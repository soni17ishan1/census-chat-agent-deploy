"""Caching behavior: repeated/identical lookups shouldn't re-hit Snowflake,
but a failed query must never be cached -- a transient failure (e.g. the
warehouse-cold-start issue observed during dev) would otherwise become
permanent for the rest of the process's life.
"""
from unittest.mock import MagicMock, patch

import pytest
import snowflake.connector.errors

from agent import schema_tools
from agent.snowflake_client import _query_cache, run_select


@pytest.fixture(autouse=True)
def _clear_caches():
    schema_tools._table_search_cache.clear()
    schema_tools._table_fields_cache.clear()
    _query_cache.clear()
    yield
    schema_tools._table_search_cache.clear()
    schema_tools._table_fields_cache.clear()
    _query_cache.clear()


def _mock_select_conn(rows, columns=("X",)):
    conn = MagicMock()
    cur = MagicMock()
    cur.description = [(c,) for c in columns]
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn, cur


@patch("agent.schema_tools.get_connection")
def test_search_census_tables_is_cached_across_calls(mock_get_conn):
    conn, cur = _mock_select_conn(
        [("B19013", "Median Household Income", "Income", "Households")],
        columns=("TABLE_NUMBER", "TABLE_TITLE", "TABLE_TOPICS", "TABLE_UNIVERSE"),
    )
    mock_get_conn.return_value = conn

    first = schema_tools.search_census_tables("income")
    second = schema_tools.search_census_tables("income")

    assert first == second
    assert cur.execute.call_count == 1  # second call was a cache hit
    mock_get_conn.assert_called_once()


@patch("agent.schema_tools.get_connection")
def test_search_census_tables_different_keywords_each_hit_snowflake(mock_get_conn):
    conn, cur = _mock_select_conn(
        [("B19013", "Median Household Income", "Income", "Households")],
        columns=("TABLE_NUMBER", "TABLE_TITLE", "TABLE_TOPICS", "TABLE_UNIVERSE"),
    )
    mock_get_conn.return_value = conn

    schema_tools.search_census_tables("income")
    schema_tools.search_census_tables("age")

    assert cur.execute.call_count == 2


@patch("agent.schema_tools.get_connection")
def test_get_table_fields_is_cached_across_calls(mock_get_conn):
    conn, cur = _mock_select_conn(
        [("B19013e1", "Estimate", "Median income", None, None, None, None, None, None)],
        columns=(
            "TABLE_ID", "FIELD_LEVEL_1", "FIELD_LEVEL_2", "FIELD_LEVEL_3", "FIELD_LEVEL_4",
            "FIELD_LEVEL_5", "FIELD_LEVEL_6", "FIELD_LEVEL_7", "FIELD_LEVEL_8",
        ),
    )
    mock_get_conn.return_value = conn

    first = schema_tools.get_table_fields("B19013")
    second = schema_tools.get_table_fields("B19013")

    assert first == second
    assert cur.execute.call_count == 1
    mock_get_conn.assert_called_once()


@patch("agent.snowflake_client.get_connection")
def test_run_select_caches_successful_results(mock_get_conn):
    conn, cur = _mock_select_conn([(1,)])
    mock_get_conn.return_value = conn

    first = run_select("SELECT 1 AS X")
    second = run_select("SELECT 1 AS X")

    assert first == second
    assert "error" not in first
    # 2 execute calls total (ALTER SESSION + query) from the *first* run only
    assert cur.execute.call_count == 2
    mock_get_conn.assert_called_once()


@patch("agent.snowflake_client.get_connection")
def test_run_select_does_not_cache_errors(mock_get_conn):
    conn = MagicMock()
    cur = MagicMock()
    cur.execute.side_effect = snowflake.connector.errors.ProgrammingError(msg="boom")
    conn.cursor.return_value = cur
    mock_get_conn.return_value = conn

    first = run_select("SELECT bad_column FROM foo")
    second = run_select("SELECT bad_column FROM foo")

    assert "error" in first
    assert "error" in second
    # Neither call was served from cache -- a transient failure must never
    # become permanent for the rest of the process's life.
    assert mock_get_conn.call_count == 2


@patch("agent.snowflake_client.get_connection")
def test_run_select_cache_key_normalizes_implicit_limit(mock_get_conn):
    """SELECT ... and SELECT ... LIMIT 200 (the auto-appended default)
    should be treated as the same cached query."""
    conn, cur = _mock_select_conn([(1,)])
    mock_get_conn.return_value = conn

    run_select("SELECT 1 AS X")
    run_select("SELECT 1 AS X LIMIT 200")

    assert cur.execute.call_count == 2  # only the first call actually ran
