"""Golden-data regression tests: verify the data layer against known-correct,
externally sourced numbers (official 2020 Decennial Census state populations),
not just internal self-consistency.

Unlike the rest of the suite, these hit live Snowflake (no mocks) -- that's
the whole point: this is exactly the kind of test that would have caught the
geography join fan-out bug found during development, which mocked-data unit
tests structurally cannot catch. Skipped automatically if live credentials
aren't configured (e.g. in CI without secrets).
"""
import os

import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("SNOWFLAKE_ACCOUNT"),
    reason="requires live Snowflake credentials",
)

from agent.snowflake_client import DATABASE, run_select  # noqa: E402

# (state abbreviation, official 2020 Decennial Census population, tolerance)
# Tolerance is generous (10%) because our data source is ACS 5-year *survey
# estimates* (2016-2020 average), not the Decennial count itself -- some
# divergence from the official decennial figure is expected and fine. The
# point of this test is to catch gross errors (wrong by 2x, 10x, 58x...),
# not to validate ACS methodology against the decennial census.
GOLDEN_STATE_POPULATIONS = [
    ("CA", 39_538_223, 0.10),
    ("TX", 29_145_505, 0.10),
    ("FL", 21_538_187, 0.10),
    ("NY", 20_201_249, 0.10),
    ("OH", 11_799_448, 0.10),
]


def _correct_join_sql(state_abbr: str) -> str:
    return f"""
        SELECT SUM(b."B01001e1") AS total_population
        FROM {DATABASE}.PUBLIC."2020_CBG_B01" b
        JOIN {DATABASE}.PUBLIC."2020_METADATA_CBG_FIPS_CODES" f
          ON LEFT(b.CENSUS_BLOCK_GROUP, 2) = f.STATE_FIPS
         AND SUBSTR(b.CENSUS_BLOCK_GROUP, 3, 3) = f.COUNTY_FIPS
        WHERE f.STATE = '{state_abbr}'
    """


def _buggy_join_sql(state_abbr: str) -> str:
    """Reproduces the original bug on purpose: joins on STATE_FIPS only,
    which fans out against every county row in that state."""
    return f"""
        SELECT SUM(b."B01001e1") AS total_population
        FROM {DATABASE}.PUBLIC."2020_CBG_B01" b
        JOIN {DATABASE}.PUBLIC."2020_METADATA_CBG_FIPS_CODES" f
          ON LEFT(b.CENSUS_BLOCK_GROUP, 2) = f.STATE_FIPS
        WHERE f.STATE = '{state_abbr}'
    """


def _run_population_query(sql: str) -> float:
    result = run_select(sql)
    assert "error" not in result, result.get("error")
    assert result["row_count"] == 1
    return float(result["rows"][0][0])


@pytest.mark.parametrize("state,expected,tolerance", GOLDEN_STATE_POPULATIONS)
def test_state_population_matches_official_census_figure(state, expected, tolerance):
    actual = _run_population_query(_correct_join_sql(state))
    lower, upper = expected * (1 - tolerance), expected * (1 + tolerance)
    assert lower <= actual <= upper, (
        f"{state}: expected ~{expected:,} (+/-{tolerance:.0%}), got {actual:,.0f}"
    )


def test_golden_check_would_have_caught_the_join_fanout_bug():
    """Proof that this golden suite has teeth: the original buggy query
    (state-only join) is wildly out of tolerance, the fixed one isn't."""
    expected = 39_538_223
    buggy = _run_population_query(_buggy_join_sql("CA"))
    correct = _run_population_query(_correct_join_sql("CA"))

    assert buggy > expected * 2, "buggy join should be comically wrong, not subtly off"
    assert abs(correct - expected) / expected < 0.10
