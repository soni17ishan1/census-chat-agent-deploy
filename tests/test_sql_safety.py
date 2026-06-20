import pytest

from agent.snowflake_client import (
    MAX_ROWS,
    SqlSafetyError,
    _enforce_row_limit,
    validate_select_only,
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select * from foo",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_validate_select_only_allows_select_and_with(sql):
    validate_select_only(sql)  # should not raise


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE foo",
        "DELETE FROM foo",
        "INSERT INTO foo VALUES (1)",
        "SELECT 1; DROP TABLE foo",
        "UPDATE foo SET x = 1",
        "CREATE TABLE foo (x INT)",
        "GRANT SELECT ON foo TO PUBLIC",
        "SELECT * FROM foo WHERE bar = (CALL my_proc())",
    ],
)
def test_validate_select_only_rejects_unsafe_statements(sql):
    with pytest.raises(SqlSafetyError):
        validate_select_only(sql)


def test_enforce_row_limit_appends_when_missing():
    result = _enforce_row_limit("SELECT * FROM foo")
    assert result == f"SELECT * FROM foo LIMIT {MAX_ROWS}"


def test_enforce_row_limit_leaves_existing_limit():
    result = _enforce_row_limit("SELECT * FROM foo LIMIT 5")
    assert result == "SELECT * FROM foo LIMIT 5"


def test_enforce_row_limit_strips_trailing_semicolon():
    result = _enforce_row_limit("SELECT * FROM foo;")
    assert result == f"SELECT * FROM foo LIMIT {MAX_ROWS}"
