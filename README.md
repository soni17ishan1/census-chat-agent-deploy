# US Census Chat Agent

A chat agent that answers natural-language questions about US population/demographics, grounded in the SafeGraph **"US Open Census Data – Neighborhood Insights"** dataset from the Snowflake Marketplace (ACS 5-year estimates, Census Block Group level, survey years 2019 and 2020).

**Live demo:** https://census-chat-agent-deploy-6zfehfufdvmbj89ztcv7nw.streamlit.app/
**Password:** `census2026`

> **Note on repos:** this private repo (`census-chat-agent`) is the canonical deliverable. Streamlit Community Cloud's free tier requires a *public* source repo, so a code-only mirror with no secrets, `census-chat-agent-deploy` (https://github.com/soni17ishan1/census-chat-agent-deploy), is used solely as the deploy source.

## Architecture

```
User (browser)
   │
   ▼
Streamlit app (app.py)
   │  conversation history kept in st.session_state
   ▼
Guardrail classifier (agent/guardrails.py)
   │  cheap/fast model call: on_topic | off_topic | inappropriate
   │  off_topic / inappropriate → fast-fail refusal, no SQL is ever generated
   ▼
Agent loop (agent/agent_loop.py)
   │  Claude (tool use), up to 8 tool-call iterations, 45s soft deadline
   │  conversation history trimmed to last 8 turns before each call
   │
   ├─► search_census_tables(keyword)   ─┐
   ├─► get_table_fields(table_number)   ├─► agent/schema_tools.py
   └─► run_sql(sql)                    ─┘     (schema introspection,
                                         │      in-memory cache, logged
                                         │      hit/miss on every call)
                                         ▼
                              agent/snowflake_client.py
                              (SELECT-only validation, cross-database
                               block, row cap, statement timeout,
                               cached results, retry-with-reconnect
                               on connection-level failures)
                                         │
                                         ▼
                                    Snowflake
                              (read-only role + account-wide
                               Resource Monitor spend cap)
```

**Why an agentic tool-use loop instead of stuffing the schema into one prompt:** the dataset has ~30 wide data tables per year plus a metadata table describing 8,000+ individual columns. Rather than hardcoding column names, the model is given three tools and a primer on the *shape* of the schema (naming conventions, the geography join, known gotchas — see below) and explores the actual metadata at query time. This is what lets it answer questions about columns nobody hardcoded in a prompt.

**Why a separate guardrail step instead of one prompt that does everything:** off-topic/inappropriate input fails fast (one cheap model call) before any SQL exploration happens, instead of burning multiple tool-use turns and Snowflake queries on a question that was never going to be answered. It also gives a single, auditable place to tighten or loosen scope.

**Why the model is never allowed to state a number it didn't get from `run_sql`:** the system prompt explicitly forbids it, and the architecture reinforces it structurally — the model only gets to write a final answer once it has seen real tool results in its context. No separate "fact-checking" pass is needed because the only path to the final answer runs through Snowflake.

## Data notes (load-bearing, not just trivia)

- **Granularity:** Census Block Group (12-digit FIPS code). There is no city/place-level table — the agent is instructed to say so plainly rather than approximate when asked about a city.
- **Geography join gotcha:** `METADATA_CBG_FIPS_CODES` has one row *per county*, and its `COUNTY_FIPS` column is only the 3-digit county part, not a 5-digit code. Joining a data table to it on `STATE_FIPS` alone fan-outs every block-group row against every county in that state — we hit this directly during development and it inflated California's summed population by ~58x. The fix (join on `STATE_FIPS` **and** `COUNTY_FIPS` together) is baked into the agent's system prompt (`agent/schema_tools.py:SCHEMA_PRIMER`) so the model doesn't repeat the mistake.
- **`STATE` column gotcha:** it holds the 2-letter USPS abbreviation (`'CA'`), not the full name (`'California'`). Filtering on the full name doesn't error -- it just matches zero rows, so a `SUM` silently returns `NULL` instead of failing loudly. Caught via the structured logging described below (the model's first attempt at a new state would occasionally guess the full name, then burn 2-3 extra tool calls self-correcting); fixed by stating the column's actual format explicitly in the system prompt.
- **Column identifiers are mixed-case** (e.g. `B01001e1`) and must be double-quoted in SQL or Snowflake folds them to uppercase and the query fails.
- Scope is limited to the ACS demographic tables (`{year}_CBG_{table_group}`) and their metadata. Geometry, foot-traffic "patterns", and 2020 redistricting tables are present in the database but out of scope for this agent (see REFLECTION.md).

## Operational safeguards

Added after the initial build, each in response to a real gap or a real incident observed live (not hypothetical) -- see `REFLECTION.md` for the full story behind each one.

| Safeguard | What it does | Why |
|---|---|---|
| **Per-session rate limit** (`app.py`) | 30 questions/session, 3s minimum between messages | A leaked password had no limit on how much it could cost |
| **Snowflake Resource Monitor** (`CENSUS_CHAT_AGENT_BUDGET`, 10 credit/month) | Hard-suspends the warehouse at 100% usage, enforced by Snowflake itself | An account-wide spend cap that can't be bypassed by an app bug |
| **Input length cap** (`app.py`, 500 chars) | Rejects oversized messages before they reach the guardrail/agent | One huge pasted message could burn a lot of tokens in a single request |
| **Caching** (`schema_tools.py`, `snowflake_client.py`) | Repeated schema lookups/SQL are served from an in-memory cache; errors are never cached | This is static, read-only historical data -- safe to cache; caching a transient failure would make it permanent until restart |
| **Connection retry** (`snowflake_client.py`) | A SQL-level error fails immediately; anything else (connection drop) forces a fresh connection and retries once | We observed exactly this failure mode live (a query failed, the same one succeeded moments later) |
| **History trimming** (`agent_loop.trim_history`) | Keeps only the last 8 user turns' worth of context, cut only at safe boundaries | Every tool call within a turn also accumulates in what's resent to Claude each turn -- token cost/latency grows with conversation length faster than it looks |
| **Structured logging** (stdout, captured by Streamlit Cloud's log viewer) | Every question, guardrail verdict, tool call, cache hit/miss, generated SQL with latency/outcome, and exception traceback | "Production quality" requires being able to debug an issue from logs, not just by reproducing it live |

## Running locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
streamlit run app.py
```

Required environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_WAREHOUSE` | Snowflake connection |
| `ANTHROPIC_API_KEY` | Claude API access |
| `APP_PASSWORD` | Shared password for the web UI (omit to disable the gate, e.g. for local dev) |

## Testing

```bash
pytest tests/ -v
```

47 unit tests (mocked Snowflake/Anthropic, no live credentials needed) cover:
- SQL safety validation (rejects non-SELECT statements, multi-statement injection, cross-database references, enforces row limits)
- Guardrail classification (on/off-topic/inappropriate, malformed-output fail-open behavior, markdown-fence stripping, follow-up context handling)
- Agent loop control flow (tool dispatch, max-iteration fallback, soft-deadline fallback, progress-callback reporting, exception containment, conversation-history trimming without breaking tool_use/tool_result pairing)
- Caching (`tests/test_caching.py`): repeated lookups are served from cache, but a failed query is never cached -- a transient failure must self-heal on retry, not become permanent
- Connection resilience (`tests/test_connection_resilience.py`): a SQL-level error fails immediately (no point retrying a wrong query), a connection-level error gets exactly one retry against a fresh connection before giving up

Plus 6 **golden-data regression tests** (`tests/test_golden_data.py`) that hit live Snowflake and check the actual aggregation SQL against the official 2020 Decennial Census population for 5 states (10% tolerance, to allow for expected ACS-vs-decennial variance). These exist specifically because the unit tests above can't catch a data-correctness bug like the geography join fan-out described below — one of the golden tests deliberately re-runs the original buggy query and asserts it's wildly wrong, proving the suite would have caught it. Skipped automatically if `SNOWFLAKE_ACCOUNT` isn't set (e.g. in CI without secrets).

## Interpretation of open-ended requirements

- **Auth**: a single shared password gate, per the assignment FAQ's explicit allowance ("Is it ok if viewing the demo requires authentication? Yes").
- **"Production quality" scope**: prioritized correctness of the data layer (the FIPS join bug above) and graceful degradation over polish like token-by-token streaming — see REFLECTION.md for the explicit tradeoff.
- **Guardrails**: scoped to topic relevance + prompt-injection/abuse detection, not full content-moderation (no claim of catching all jailbreaks).
- **"Comprehensive mapping"**: interpreted as "don't hardcode which ACS demographic topics/columns are supported" rather than "support every table in the database regardless of relevance to population/demographic questions." The agent dynamically searches the *full* ACS metadata catalog (all table groups, all years, all 8,000+ field codes) via `search_census_tables`/`get_table_fields` rather than a hardcoded subset — but three present-but-unrelated table families (`*_CBG_GEOMETRY*` shape data, `*_CBG_PATTERNS` foot-traffic data, `2020_REDISTRICTING_*`) are deliberately out of scope, since they aren't demographic/population data and including them would mean spending the time budget on geospatial/mobility features instead of getting the core grounding correct.
