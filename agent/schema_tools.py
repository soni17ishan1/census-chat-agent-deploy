"""Schema-introspection helpers backing the agent's tools.

Column codes (e.g. B01001e1) are consistent across the 2019 and 2020 ACS
table variants in this dataset, so we look them up against the 2020
metadata tables regardless of which year's data table is ultimately queried.
"""
import logging

from agent.snowflake_client import DATABASE, SCHEMA, get_connection

logger = logging.getLogger(__name__)

METADATA_YEAR = "2020"

AVAILABLE_YEARS = ("2019", "2020")

# This metadata is static for the lifetime of the process (it's a fixed,
# already-published dataset), and these lookups repeat constantly across
# questions/users (e.g. almost every income question searches "income").
# Plain dict caches (not lru_cache) so we can log explicitly whether a given
# call was a cache hit or a fresh Snowflake round-trip -- important for
# debugging "why was this answer instant vs. slow". A raised exception is
# never cached, so a transient failure always retries fresh next time.
_table_search_cache: dict[tuple[str, int], list[dict]] = {}
_table_fields_cache: dict[str, list[dict]] = {}


def search_census_tables(keyword: str, limit: int = 20) -> list[dict]:
    """Find ACS table codes whose title/topic/universe mentions `keyword`."""
    cache_key = (keyword, limit)
    if cache_key in _table_search_cache:
        logger.info("Cache hit: search_census_tables(%r)", keyword)
        return _table_search_cache[cache_key]

    logger.info("Cache miss: search_census_tables(%r), querying Snowflake", keyword)
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
        result = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()
    _table_search_cache[cache_key] = result
    return result


def get_table_fields(table_number: str) -> list[dict]:
    """Return every column code + human-readable description for a table number."""
    if table_number in _table_fields_cache:
        logger.info("Cache hit: get_table_fields(%r)", table_number)
        return _table_fields_cache[table_number]

    logger.info("Cache miss: get_table_fields(%r), querying Snowflake", table_number)
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
    finally:
        cur.close()
    _table_fields_cache[table_number] = rows
    return rows


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
  - In "{{year}}_METADATA_CBG_FIPS_CODES", the STATE column holds the
    2-letter USPS abbreviation (e.g. 'CO', 'CA'), NOT the full state name
    ('Colorado' will match zero rows and silently return NULL from a SUM
    with no error -- this is a real mistake we caught via logging during
    testing, costing several wasted exploration steps before
    self-correcting). If you only have a full state name, either map it to
    its abbreviation yourself or filter by STATE_FIPS instead.
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
