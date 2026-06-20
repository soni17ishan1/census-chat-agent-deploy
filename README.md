# US Census Chat Agent

A chat agent that answers natural-language questions about US population/demographics, grounded in the SafeGraph **"US Open Census Data – Neighborhood Insights"** dataset from the Snowflake Marketplace (ACS 5-year estimates, Census Block Group level, survey years 2019 and 2020).

**Live demo:** _<filled in after deployment>_
**Password:** _<filled in after deployment>_

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
   │
   ├─► search_census_tables(keyword)   ─┐
   ├─► get_table_fields(table_number)   ├─► agent/schema_tools.py
   └─► run_sql(sql)                    ─┘     (schema introspection)
                                         │
                                         ▼
                              agent/snowflake_client.py
                              (SELECT-only validation, row cap,
                               statement timeout, read-only role)
                                         │
                                         ▼
                                    Snowflake
```

**Why an agentic tool-use loop instead of stuffing the schema into one prompt:** the dataset has ~30 wide data tables per year plus a metadata table describing 8,000+ individual columns. Rather than hardcoding column names, the model is given three tools and a primer on the *shape* of the schema (naming conventions, the geography join, known gotchas — see below) and explores the actual metadata at query time. This is what lets it answer questions about columns nobody hardcoded in a prompt.

**Why a separate guardrail step instead of one prompt that does everything:** off-topic/inappropriate input fails fast (one cheap model call) before any SQL exploration happens, instead of burning multiple tool-use turns and Snowflake queries on a question that was never going to be answered. It also gives a single, auditable place to tighten or loosen scope.

**Why the model is never allowed to state a number it didn't get from `run_sql`:** the system prompt explicitly forbids it, and the architecture reinforces it structurally — the model only gets to write a final answer once it has seen real tool results in its context. No separate "fact-checking" pass is needed because the only path to the final answer runs through Snowflake.

## Data notes (load-bearing, not just trivia)

- **Granularity:** Census Block Group (12-digit FIPS code). There is no city/place-level table — the agent is instructed to say so plainly rather than approximate when asked about a city.
- **Geography join gotcha:** `METADATA_CBG_FIPS_CODES` has one row *per county*, and its `COUNTY_FIPS` column is only the 3-digit county part, not a 5-digit code. Joining a data table to it on `STATE_FIPS` alone fan-outs every block-group row against every county in that state — we hit this directly during development and it inflated California's summed population by ~58x. The fix (join on `STATE_FIPS` **and** `COUNTY_FIPS` together) is baked into the agent's system prompt (`agent/schema_tools.py:SCHEMA_PRIMER`) so the model doesn't repeat the mistake.
- **Column identifiers are mixed-case** (e.g. `B01001e1`) and must be double-quoted in SQL or Snowflake folds them to uppercase and the query fails.
- Scope is limited to the ACS demographic tables (`{year}_CBG_{table_group}`) and their metadata. Geometry, foot-traffic "patterns", and 2020 redistricting tables are present in the database but out of scope for this agent (see REFLECTION.md).

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

27 unit tests cover:
- SQL safety validation (rejects non-SELECT statements, multi-statement injection, enforces row limits)
- Guardrail classification (on/off-topic/inappropriate, malformed-output fail-open behavior, markdown-fence stripping, follow-up context handling)
- Agent loop control flow (tool dispatch, max-iteration fallback, soft-deadline fallback, exception containment)

Snowflake and Anthropic calls are mocked in tests so the suite runs without live credentials. See `REFLECTION.md` for what's deliberately *not* covered (e.g. no live-Snowflake integration test asserting a specific numeric answer) and why.

## Interpretation of open-ended requirements

- **Auth**: a single shared password gate, per the assignment FAQ's explicit allowance ("Is it ok if viewing the demo requires authentication? Yes").
- **"Production quality" scope**: prioritized correctness of the data layer (the FIPS join bug above) and graceful degradation over polish like token-by-token streaming — see REFLECTION.md for the explicit tradeoff.
- **Guardrails**: scoped to topic relevance + prompt-injection/abuse detection, not full content-moderation (no claim of catching all jailbreaks).
