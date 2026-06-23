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

import snowflake.connector

from agent.cache_utils import BoundedCache

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
#
# A fully-qualified Snowflake table name has 3 dot-separated parts:
# DATABASE.SCHEMA.TABLE, e.g. SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY or
# US_OPEN_CENSUS_DATA....PUBLIC."2020_CBG_B01". _QUALIFIED_REF_RE finds any
# such 3-part reference in the query text; validate_select_only() below then
# checks that the first part is always our own DATABASE, never anything else.
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED_REF_RE = re.compile(rf"({_IDENT})\s*\.\s*{_IDENT}\s*\.\s*{_IDENT}")
_KNOWN_OTHER_DATABASES_RE = re.compile(
    r"\b(SNOWFLAKE|SNOWFLAKE_SAMPLE_DATA)\b\s*\.", re.IGNORECASE
)

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 25  # leaves headroom under the 60s end-to-end budget

# Snowflake surfaces master/session-token expiration as a ProgrammingError --
# the same exception class raised for a bad-SQL error -- so errno is the
# only way to tell "the connection is dead, re-authenticate" apart from
# "the query is wrong, don't bother retrying". GS codes per
# snowflake.connector.network: 390110 id token expired, 390112 session
# expired, 390113/390114/390115 master token not found/expired/invalid.
_AUTH_TOKEN_EXPIRED_ERRNOS = {390110, 390112, 390113, 390114, 390115}


class SqlSafetyError(ValueError):
    """Raised when generated SQL fails the read-only safety check."""


class QueryTimeoutError(RuntimeError):
    """Raised when a query exceeds QUERY_TIMEOUT_SECONDS."""


def _strip_ident(token: str) -> str:
    return token.strip('"').upper()


def validate_select_only(sql: str) -> None:
    """Raises SqlSafetyError on the first violation found. Examples:
        validate_select_only("SELECT * FROM foo")            -> OK
        validate_select_only("DROP TABLE foo")                -> raises (not SELECT/WITH)
        validate_select_only("SELECT 1; DROP TABLE foo")      -> raises (two statements)
        validate_select_only('SELECT * FROM SNOWFLAKE.X.Y')   -> raises (wrong database)
    """
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
# than no caching at all. Bounded (see agent/cache_utils.py) so this can't
# grow without limit over a long-running process.
_query_cache: BoundedCache[str, dict] = BoundedCache()


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
                # This connection is held open and reused for the life of
                # the process (see module-level _connection), so without a
                # heartbeat the master token hits its ~4h validity window
                # and expires from pure age, not actual inactivity. This
                # makes the connector ping /session/heartbeat in the
                # background to renew it before that happens.
                client_session_keep_alive=True,
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


def _retry_after_reconnect(safe_sql: str, start: float) -> dict:
    """Discards the (dead) connection and retries `safe_sql` exactly once
    against a fresh one. Returns the query result dict, or {"error": ...}
    if the retry also fails."""
    _force_reconnect()
    try:
        return _execute_query(safe_sql)
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


def run_select(sql: str) -> dict:
    """Validate and execute a read-only query. Returns dict with columns/rows or error."""
    try:
        validate_select_only(sql)
    except SqlSafetyError as e:
        logger.warning("SQL rejected by safety check: %s | sql=%s", e, sql[:200])
        return {"error": f"Query rejected by safety check: {e}"}

    safe_sql = _enforce_row_limit(sql)

    # Cache key is the exact final SQL string, e.g.:
    #   'SELECT SUM(b."B01003e1") AS total_population FROM ... WHERE f.STATE = \'CA\' LIMIT 200'
    # Cache value is the result dict, e.g. {"columns": [...], "rows": [(39346023.0,)], "row_count": 1}.
    # Same question asked again (by anyone, this cache is shared across all
    # users) -> instant return below, no Snowflake round-trip.
    cached = _query_cache.get(safe_sql)
    if cached is not None:
        logger.info("Cache hit | sql=%s", safe_sql[:200])
        return cached

    start = time.monotonic()
    try:
        result = _execute_query(safe_sql)
    except snowflake.connector.errors.ProgrammingError as e:
        if e.errno not in _AUTH_TOKEN_EXPIRED_ERRNOS:
            # A genuine SQL-level problem (bad column, bad syntax) --
            # retrying the exact same query against a fresh connection
            # would just fail the same way, so don't waste time/budget on it.
            elapsed = time.monotonic() - start
            logger.error(
                "SQL error after %.1fs: %s | sql=%s", elapsed, e, safe_sql[:200]
            )
            return {"error": f"Snowflake query error: {e.msg if hasattr(e, 'msg') else e}"}
        logger.warning(
            "Auth token expired (errno %s), forcing reconnect and retrying once | sql=%s",
            e.errno,
            safe_sql[:200],
        )
        retried = _retry_after_reconnect(safe_sql, start)
        if "error" in retried:
            return retried
        result = retried
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
        retried = _retry_after_reconnect(safe_sql, start)
        if "error" in retried:
            return retried
        result = retried

    elapsed = time.monotonic() - start
    logger.info(
        "Query succeeded in %.1fs, %d rows | sql=%s", elapsed, result["row_count"], safe_sql[:200]
    )
    _query_cache.put(safe_sql, result)
    return result
