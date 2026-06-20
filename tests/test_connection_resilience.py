"""Connection resilience: a SQL-level error (bad column, bad syntax) should
fail immediately with no retry, since retrying the same query against a
fresh connection would just fail the same way. A connection-level error
(the kind we saw live during testing -- looked like a stale/cold-started
warehouse) should get exactly one retry against a freshly created
connection before giving up.
"""
from unittest.mock import MagicMock, patch

import snowflake.connector.errors

from agent.snowflake_client import _query_cache, run_select


def setup_function():
    _query_cache.clear()


def _mock_conn(rows=None, columns=("X",)):
    conn = MagicMock()
    cur = MagicMock()
    if rows is not None:
        cur.description = [(c,) for c in columns]
        cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn, cur


@patch("agent.snowflake_client.get_connection")
def test_programming_error_fails_immediately_without_retry(mock_get_conn):
    conn, cur = _mock_conn()
    cur.execute.side_effect = snowflake.connector.errors.ProgrammingError(msg="bad column")
    mock_get_conn.return_value = conn

    result = run_select("SELECT bad_column FROM foo")

    assert "error" in result
    assert mock_get_conn.call_count == 1  # no retry attempted


@patch("agent.snowflake_client._force_reconnect")
@patch("agent.snowflake_client.get_connection")
def test_connection_error_retries_once_against_fresh_connection(mock_get_conn, mock_reconnect):
    # First call's cursor raises a generic (non-SQL) error; second call's
    # cursor (after the forced reconnect) succeeds.
    bad_conn, bad_cur = _mock_conn()
    bad_cur.execute.side_effect = ConnectionError("connection reset")
    good_conn, good_cur = _mock_conn(rows=[(42,)])
    mock_get_conn.side_effect = [bad_conn, good_conn]

    result = run_select("SELECT 42 AS X")

    assert "error" not in result
    assert result["rows"] == [(42,)]
    mock_reconnect.assert_called_once()
    assert mock_get_conn.call_count == 2


@patch("agent.snowflake_client._force_reconnect")
@patch("agent.snowflake_client.get_connection")
def test_connection_error_gives_up_after_one_failed_retry(mock_get_conn, mock_reconnect):
    conn, cur = _mock_conn()
    cur.execute.side_effect = ConnectionError("connection reset")
    mock_get_conn.return_value = conn  # fails the same way both times

    result = run_select("SELECT 42 AS X")

    assert "error" in result
    assert "after retry" in result["error"]
    mock_reconnect.assert_called_once()
    assert mock_get_conn.call_count == 2  # original attempt + one retry, no more
