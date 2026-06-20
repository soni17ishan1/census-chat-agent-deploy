"""Read-only Snowflake access for the Census chat agent.

The marketplace database is shared as read-only by Snowflake itself, but we
still validate SQL text here as defense in depth: the LLM is an untrusted
caller from the database's point of view.
"""
import os
import re
import threading

import snowflake.connector

DATABASE = "US_OPEN_CENSUS_DATA__NEIGHBORHOOD_INSIGHTS__FREE_DATASET"
SCHEMA = "PUBLIC"

# Only SELECT/WITH statements, and only one statement at a time.
_SAFE_STATEMENT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|TRUNCATE|CALL|EXECUTE)\b",
    re.IGNORECASE,
)

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 25  # leaves headroom under the 60s end-to-end budget


class SqlSafetyError(ValueError):
    """Raised when generated SQL fails the read-only safety check."""


class QueryTimeoutError(RuntimeError):
    """Raised when a query exceeds QUERY_TIMEOUT_SECONDS."""


def validate_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise SqlSafetyError("Only a single SQL statement is allowed.")
    if not _SAFE_STATEMENT_RE.match(stripped):
        raise SqlSafetyError("Only SELECT/WITH statements are allowed.")
    if _FORBIDDEN_KEYWORDS_RE.search(stripped):
        raise SqlSafetyError("Statement contains a forbidden keyword.")


def _enforce_row_limit(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped} LIMIT {MAX_ROWS}"


_connection_lock = threading.Lock()
_connection = None


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


def run_select(sql: str) -> dict:
    """Validate and execute a read-only query. Returns dict with columns/rows or error."""
    try:
        validate_select_only(sql)
    except SqlSafetyError as e:
        return {"error": f"Query rejected by safety check: {e}"}

    safe_sql = _enforce_row_limit(sql)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {QUERY_TIMEOUT_SECONDS}")
        cur.execute(safe_sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    except snowflake.connector.errors.ProgrammingError as e:
        return {"error": f"Snowflake query error: {e.msg if hasattr(e, 'msg') else e}"}
    finally:
        cur.close()
