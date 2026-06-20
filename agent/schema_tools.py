"""Schema-introspection helpers backing the agent's tools.

Column codes (e.g. B01001e1) are consistent across the 2019 and 2020 ACS
table variants in this dataset, so we look them up against the 2020
metadata tables regardless of which year's data table is ultimately queried.
"""
from functools import lru_cache

from agent.snowflake_client import DATABASE, SCHEMA, get_connection

METADATA_YEAR = "2020"

AVAILABLE_YEARS = ("2019", "2020")


# This metadata is static for the lifetime of the process (it's a fixed,
# already-published dataset), and these lookups repeat constantly across
# questions/users (e.g. almost every income question searches "income").
# lru_cache only memoizes successful returns -- if get_connection() or the
# query raises, nothing is cached and the next call retries fresh.
@lru_cache(maxsize=128)
def search_census_tables(keyword: str, limit: int = 20) -> list[dict]:
    """Find ACS table codes whose title/topic/universe mentions `keyword`."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT DISTINCT TABLE_NUMBER, TABLE_TITLE, TABLE_TOPICS, TABLE_UNIVERSE
            FROM {DATABASE}.{SCHEMA}."{METADATA_YEAR}_METADATA_CBG_FIELD_DESCRIPTIONS"
            WHERE TABLE_TITLE ILIKE %s OR TABLE_TOPICS ILIKE %s OR TABLE_UNIVERSE ILIKE %s
            ORDER BY TABLE_NUMBER
            LIMIT %s
            """,
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


@lru_cache(maxsize=128)
def get_table_fields(table_number: str) -> list[dict]:
    """Return every column code + human-readable description for a table number."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT TABLE_ID, FIELD_LEVEL_1, FIELD_LEVEL_2, FIELD_LEVEL_3, FIELD_LEVEL_4,
                   FIELD_LEVEL_5, FIELD_LEVEL_6, FIELD_LEVEL_7, FIELD_LEVEL_8
            FROM {DATABASE}.{SCHEMA}."{METADATA_YEAR}_METADATA_CBG_FIELD_DESCRIPTIONS"
            WHERE TABLE_NUMBER = %s
            ORDER BY TABLE_ID
            """,
            (table_number,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            parts = [str(r[c]) for c in cols[1:] if r[c] is not None]
            r["description"] = " > ".join(parts)
        return rows
    finally:
        cur.close()


SCHEMA_PRIMER = f"""
Dataset: SafeGraph "US Open Census Data" (ACS estimates), database
{DATABASE}, schema {SCHEMA}. Available years: {", ".join(AVAILABLE_YEARS)}.

Data tables are named "{{year}}_CBG_{{table_group}}", where table_group is
the leading letters+2digits of the table number (e.g. table B01001 lives in
table "2020_CBG_B01"). The full table number (e.g. B01001) only appears as
a column prefix within that table -- columns are named like B01001e1.
Always double-quote table names, since they start with a digit.

Every numeric column has two variants: an estimate (suffix "e", e.g.
B01001e1) and a margin of error (suffix "m", e.g. B01001m1). Use the "e"
columns to answer questions; only mention margin of error if the user asks
about confidence/uncertainty.

Geography: each row is keyed by CENSUS_BLOCK_GROUP, a 12-digit FIPS code.
  - State FIPS = LEFT(CENSUS_BLOCK_GROUP, 2)
  - County FIPS, in "{{year}}_METADATA_CBG_FIPS_CODES", is only the 3-digit
    county part, NOT the full 5-digit code: it equals
    SUBSTR(CENSUS_BLOCK_GROUP, 3, 3) -- NOT LEFT(CENSUS_BLOCK_GROUP, 5).
  - The FIPS_CODES table has one row per COUNTY (one state has many
    counties). When joining a data table to FIPS_CODES, you MUST match on
    BOTH STATE_FIPS and COUNTY_FIPS together:
      ON LEFT(b.CENSUS_BLOCK_GROUP, 2) = f.STATE_FIPS
     AND SUBSTR(b.CENSUS_BLOCK_GROUP, 3, 3) = f.COUNTY_FIPS
    Joining on STATE_FIPS alone causes a fan-out (every CBG row matches
    every county row in that state) and silently inflates SUM()s by ~the
    number of counties in the state -- this is a real bug we hit and
    verified: it inflated California's population by ~58x. ALWAYS join on
    the full state+county pair, even when only state-level output is
    wanted (then GROUP BY STATE on top of the correctly-joined rows).
  - There is NO city/place-level table -- city/ZIP-level questions
    generally cannot be answered precisely from this dataset.
  - To aggregate to state or county level, SUM the relevant estimate
    column across all matching CENSUS_BLOCK_GROUP rows (after the correct
    join above). Margins of error do NOT sum directly (note this if asked,
    don't silently add them).
  - All column names (e.g. "B01001e1") are mixed-case and MUST be
    double-quoted in SQL, or Snowflake folds them to uppercase and the
    query fails with "invalid identifier".

Workflow:
  1. Call search_census_tables with a keyword to find candidate table(s).
  2. Call get_table_fields with the table_number to find the exact column
     code for the concept asked about.
  3. Write a single read-only SELECT (joining to FIPS_CODES for geography
     filters/labels as needed) and call run_sql.
If no table/column plausibly covers the question, or the requested
geography can't be resolved (e.g. a city name), say so plainly instead of
guessing.
"""
