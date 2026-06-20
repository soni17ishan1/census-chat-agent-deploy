"""Read-only Snowflake access for the Census chat agent.

The marketplace database is shared as read-only by Snowflake itself, but we
still validate SQL text here as defense in depth: the LLM is an untrusted
caller from the database's point of view.
"""
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import snowflake.connector

logger = logging.getLogger(__name__)

DATABASE = "US_OPEN_CENSUS_DATA__NEIGHBORHOOD_INSIGHTS__FREE_DATASET"
SCHEMA = "PUBLIC"

# Only SELECT/WITH statements, and only one statement at a time.
_SAFE_STATEMENT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|TRUNCATE|CALL|EXECUTE)\b",
    re.IGNORECASE,
)

# Defense in depth: this Snowflake role can read account-level metadata
# (e.g. SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY -- verified during dev, it
# returned real login events including client IPs) that has nothing to do
# with the census dataset. A SELECT is otherwise indistinguishable from a
# legitimate query, so we explicitly block any reference to a database
# other than the target one, in addition to relying on the model's own
# instructions to stay in scope.
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED_REF_RE = re.compile(rf"({_IDENT})\s*\.\s*{_IDENT}\s*\.\s*{_IDENT}")
_KNOWN_OTHER_DATABASES_RE = re.compile(
    r"\b(SNOWFLAKE|SNOWFLAKE_SAMPLE_DATA)\b\s*\.", re.IGNORECASE
)

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 25  # leaves headroom under the 60s end-to-end budget


class SqlSafetyError(ValueError):
    """Raised when generated SQL fails the read-only safety check."""


class QueryTimeoutError(RuntimeError):
    """Raised when a query exceeds QUERY_TIMEOUT_SECONDS."""


def _strip_ident(token: str) -> str:
    return token.strip('"').upper()


def validate_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise SqlSafetyError("Only a single SQL statement is allowed.")
    if not _SAFE_STATEMENT_RE.match(stripped):
        raise SqlSafetyError("Only SELECT/WITH statements are allowed.")
    if _FORBIDDEN_KEYWORDS_RE.search(stripped):
        raise SqlSafetyError("Statement contains a forbidden keyword.")
    if _KNOWN_OTHER_DATABASES_RE.search(stripped):
        raise SqlSafetyError("Query references a database outside the census dataset.")
    for match in _QUALIFIED_REF_RE.finditer(stripped):
        if _strip_ident(match.group(1)) != DATABASE:
            raise SqlSafetyError("Query references a database outside the census dataset.")


def _enforce_row_limit(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped} LIMIT {MAX_ROWS}"


_connection_lock = threading.Lock()
_connection = None

# Repeated/identical questions (same or different users) shouldn't re-run
# the exact same query against Snowflake -- this is read-only, immutable
# historical data, so caching successful results is always safe. We
# deliberately only cache *successes*: caching an error would make a
# transient failure (e.g. the warehouse-cold-start issue observed during
# testing) permanent for the rest of the process's life, which is worse
# than no caching at all.
_QUERY_CACHE_MAX_ENTRIES = 256
_query_cache: "OrderedDict[str, dict]" = OrderedDict()
_query_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _query_cache_lock:
        if key in _query_cache:
            _query_cache.move_to_end(key)
            return _query_cache[key]
    return None


def _cache_put(key: str, value: dict) -> None:
    with _query_cache_lock:
        _query_cache[key] = value
        _query_cache.move_to_end(key)
        while len(_query_cache) > _QUERY_CACHE_MAX_ENTRIES:
            _query_cache.popitem(last=False)


def get_connection():
    global _connection
    with _connection_lock:
        if _connection is None or _connection.is_closed():
            _connection = snowflake.connector.connect(
                account=os.environ["SNOWFLAKE_ACCOUNT"],
                user=os.environ["SNOWFLAKE_USER"],
                password=os.environ["SNOWFLAKE_PASSWORD"],
                warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
                database=DATABASE,
                schema=SCHEMA,
                login_timeout=15,
            )
        return _connection


def _force_reconnect() -> None:
    """Discard the current connection so the next get_connection() call
    creates a fresh one. Used when a query fails for a reason that looks
    connection-related (not a SQL-level error) -- e.g. the Snowflake
    warehouse-cold-start failure observed live during testing, where the
    connection/session was stale rather than the query being wrong."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
        _connection = None


def _execute_query(sql: str) -> dict:
    """Runs `sql` once against the current connection. Raises on failure."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {QUERY_TIMEOUT_SECONDS}")
        cur.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    finally:
        cur.close()


def run_select(sql: str) -> dict:
    """Validate and execute a read-only query. Returns dict with columns/rows or error."""
    try:
        validate_select_only(sql)
    except SqlSafetyError as e:
        logger.warning("SQL rejected by safety check: %s | sql=%s", e, sql[:200])
        return {"error": f"Query rejected by safety check: {e}"}

    safe_sql = _enforce_row_limit(sql)

    cached = _cache_get(safe_sql)
    if cached is not None:
        logger.info("Cache hit | sql=%s", safe_sql[:200])
        return cached

    start = time.monotonic()
    try:
        result = _execute_query(safe_sql)
    except snowflake.connector.errors.ProgrammingError as e:
        # A SQL-level problem (bad column, bad syntax) -- retrying the exact
        # same query against a fresh connection would just fail the same
        # way, so don't waste time/budget on it.
        elapsed = time.monotonic() - start
        logger.error(
            "SQL error after %.1fs: %s | sql=%s", elapsed, e, safe_sql[:200]
        )
        return {"error": f"Snowflake query error: {e.msg if hasattr(e, 'msg') else e}"}
    except Exception as e:
        # Anything else (connection drop, warehouse-cold-start timeout,
        # network blip) looks like a connection/session problem rather than
        # a SQL problem -- worth one retry against a forced-fresh connection
        # before giving up, since these are exactly the transient failures
        # we've seen self-resolve on a simple retry.
        logger.warning(
            "Non-SQL error (%s), forcing reconnect and retrying once | sql=%s",
            type(e).__name__,
            safe_sql[:200],
        )
        _force_reconnect()
        try:
            result = _execute_query(safe_sql)
        except snowflake.connector.errors.ProgrammingError as e2:
            elapsed = time.monotonic() - start
            logger.error(
                "SQL error on retry after %.1fs: %s | sql=%s", elapsed, e2, safe_sql[:200]
            )
            return {"error": f"Snowflake query error: {e2.msg if hasattr(e2, 'msg') else e2}"}
        except Exception as e2:
            elapsed = time.monotonic() - start
            logger.error(
                "Retry also failed after %.1fs: %s: %s | sql=%s",
                elapsed,
                type(e2).__name__,
                e2,
                safe_sql[:200],
            )
            return {
                "error": (
                    f"Snowflake connection error after retry: {type(e2).__name__}: {e2}"
                )
            }

    elapsed = time.monotonic() - start
    logger.info(
        "Query succeeded in %.1fs, %d rows | sql=%s", elapsed, result["row_count"], safe_sql[:200]
    )
    _cache_put(safe_sql, result)
    return result
