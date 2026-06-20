# US Census Chat Agent

[![Tests](https://github.com/soni17ishan1/census-chat-agent/actions/workflows/test.yml/badge.svg)](https://github.com/soni17ishan1/census-chat-agent/actions/workflows/test.yml)

A chat agent that answers natural-language questions about US population/demographics, grounded in the SafeGraph **"US Open Census Data – Neighborhood Insights"** dataset from the Snowflake Marketplace (ACS 5-year estimates, Census Block Group level, survey years 2019 and 2020).

**Live demo:** https://census-chat-agent-deploy-6zfehfufdvmbj89ztcv7nw.streamlit.app/
**Password:** sent separately via email (not committed here, since this repo's visibility could change).

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
   ├─► search_census_tables(keyword)   ─┐  cache #1 (BoundedCache,
   ├─► get_table_fields(table_number)   ├─ 256 entries, FIFO)   agent/schema_tools.py
   └─► run_sql(sql)                    ─┘  cache #2 (same)      (schema introspection,
                                         │                        logged hit/miss every call)
                                         ▼
                              agent/snowflake_client.py
                              (SELECT-only validation, cross-database
                               block, row cap, statement timeout,
                               cache #3 -- BoundedCache, 256 entries,
                               FIFO -- retry-with-reconnect on
                               connection-level failures)
                                         │
                                         ▼
                                    Snowflake
                              (read-only role + account-wide
                               Resource Monitor spend cap)
```

**Why an agentic tool-use loop instead of stuffing the schema into one prompt:** the dataset has ~30 wide data tables per year plus a metadata table describing 8,000+ individual columns. Rather than hardcoding column names, the model is given three tools and a primer on the *shape* of the schema (naming conventions, the geography join, known gotchas — see below) and explores the actual metadata at query time. This is what lets it answer questions about columns nobody hardcoded in a prompt.

**Why a separate guardrail step instead of one prompt that does everything:** off-topic/inappropriate input fails fast (one cheap model call) before any SQL exploration happens, instead of burning multiple tool-use turns and Snowflake queries on a question that was never going to be answered. It also gives a single, auditable place to tighten or loosen scope.

**Why the model is never allowed to state a number it didn't get from `run_sql`:** the system prompt explicitly forbids it, and the architecture reinforces it structurally — the model only gets to write a final answer once it has seen real tool results in its context. No separate "fact-checking" pass is needed because the only path to the final answer runs through Snowflake.

### Request lifecycle: what actually happens for one submitted question

The diagram above shows the static structure; this is the order of operations for a single question, end to end (see `app.py` and `agent/agent_loop.py:run_agent_turn`).

1. **Gatekeeping, before any LLM or Snowflake call:** reject if the session has already asked 30 questions, if the message is over 500 characters, or if it's been under 3 seconds since the last question.
2. **Guardrail (one cheap Claude Haiku call):** classifies the question (using recent conversation for context) as `on_topic`, `off_topic`, or `inappropriate`. If not `on_topic`, a canned refusal is returned immediately — no Snowflake, no main agent call, nothing else runs. This is the fast-fail path.
3. **Main agent loop (Claude Sonnet, up to 8 rounds / 45s soft deadline):** each round, the model either (a) returns a final text answer directly — this happens when the answer is already visible earlier in the same conversation, costing zero tool calls — or (b) calls one of the three tools (`search_census_tables`, `get_table_fields`, `run_sql`).
4. **Each tool call checks an in-memory cache first.** A cache hit returns instantly with no Snowflake round-trip. A cache miss actually queries Snowflake, then stores the result for next time (errors are never cached — see Operational safeguards).
5. **The loop repeats** with the tool result fed back into the conversation, until the model produces a final answer or the iteration/time budget runs out (in which case a fallback message asks the user to narrow the question).
6. **The answer is rendered and appended to the session's chat history.**

Every step above logs explicitly (see Operational safeguards), so the path a given answer took — guardrail refusal, answered from memory, served from cache, or required a fresh Snowflake query — is visible after the fact, not just inferable.

## Data notes (load-bearing, not just trivia)

- **Granularity:** Census Block Group (12-digit FIPS code). There is no city/place-level table — the agent is instructed to say so plainly rather than approximate when asked about a city.
- **Geography join:** `METADATA_CBG_FIPS_CODES` has one row *per county*; its `COUNTY_FIPS` column is only the 3-digit county part, not a 5-digit code. A data table must join to it on `STATE_FIPS` **and** `COUNTY_FIPS` together, never `STATE_FIPS` alone (see `agent/schema_tools.py:SCHEMA_PRIMER` for the exact join, baked into the agent's system prompt so the model doesn't get this wrong — the story behind why this matters is in `REFLECTION.md`).
- **`STATE` column format:** holds the 2-letter USPS abbreviation (`'CA'`), not the full name (`'California'`); filtering on the full name doesn't error, it just silently matches zero rows.
- **Column identifiers are mixed-case** (e.g. `B01001e1`) and must be double-quoted in SQL or Snowflake folds them to uppercase and the query fails.
- Scope is limited to the ACS demographic tables (`{year}_CBG_{table_group}`) and their metadata. Geometry, foot-traffic "patterns", and 2020 redistricting tables are present in the database but out of scope for this agent (see REFLECTION.md).

## Operational safeguards

Current behavior, for a quick reference. The reasoning, evidence, and tradeoffs behind each one are in `REFLECTION.md`, not duplicated here.

| Safeguard | What it does now |
|---|---|
| **Per-session rate limit** (`app.py`) | 30 questions/session, 3s minimum between messages |
| **Snowflake Resource Monitor** (`CENSUS_CHAT_AGENT_BUDGET`) | 10 credit/month quota, auto-suspends the warehouse at 100% usage, enforced by Snowflake itself (not app code) |
| **Input length cap** (`app.py`) | Rejects messages over 500 characters before they reach the guardrail/agent |
| **Caching** (`agent/cache_utils.py:BoundedCache`) | Three separate caches -- `search_census_tables`, `get_table_fields`, and SQL results -- each bounded at 256 entries with FIFO eviction, so none can grow without limit over a long-running process. Errors are never cached |
| **Connection retry** (`snowflake_client.py`) | A SQL-level error fails immediately; a connection-level error forces a fresh connection and retries once |
| **History trimming** (`agent_loop.trim_history`) | Keeps only the last 8 user turns' worth of context sent to Claude, dropping older turns at safe boundaries |
| **Structured logging** (stdout, captured by Streamlit Cloud's log viewer) | Every question, guardrail verdict, tool call with cache hit/miss, generated SQL with latency/outcome, and exception traceback |

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

Mocked unit tests (Snowflake/Anthropic mocked, no live credentials needed) cover:
- SQL safety validation (rejects non-SELECT statements, multi-statement injection, cross-database references, enforces row limits)
- Guardrail classification (on/off-topic/inappropriate, malformed-output fail-open behavior, markdown-fence stripping, follow-up context handling)
- Agent loop control flow (tool dispatch, max-iteration fallback, soft-deadline fallback, progress-callback reporting, exception containment, conversation-history trimming without breaking tool_use/tool_result pairing)
- Caching (`tests/test_caching.py`): repeated lookups are served from cache, but a failed query is never cached -- a transient failure must self-heal on retry, not become permanent
- Cache bounding (`tests/test_cache_utils.py`): the shared `BoundedCache` never exceeds its size limit (verified by inserting 100 entries into a 5-entry cache), evicts the oldest entry first, and `get()` refreshes an entry's recency so it survives eviction
- Connection resilience (`tests/test_connection_resilience.py`): a SQL-level error fails immediately (no point retrying a wrong query), a connection-level error gets exactly one retry against a fresh connection before giving up

Plus **golden-data regression tests** (`tests/test_golden_data.py`) that hit live Snowflake and check the actual aggregation SQL against the official 2020 Decennial Census population for several states (10% tolerance, to allow for expected ACS-vs-decennial variance). These exist specifically because the mocked tests above can't catch a data-correctness bug like the geography join fan-out described above (in Data notes) — one of them deliberately re-runs the original buggy query and asserts it's wildly wrong, proving the suite would have caught it. Skipped automatically (not failed) if `SNOWFLAKE_ACCOUNT` isn't set.

**CI** (`.github/workflows/test.yml`) runs the mocked suite automatically on every push/PR to `main`. The golden-data tests are deliberately **not** wired into CI -- no Snowflake credentials are configured as GitHub secrets, so they show as skipped there, by design, for three reasons: (1) **cost** -- CI runs on every push, and burning real Snowflake compute against the account's spend cap on every single push (rather than real usage) is wasteful; (2) **security surface** -- it would mean another place the Snowflake password lives, for no real benefit; (3) **reliability** -- live-infrastructure tests are exposed to the same transient failures (e.g. warehouse cold-start) documented in `REFLECTION.md`, which would make CI flaky/untrustworthy rather than a fast, dependable signal. So the split is intentional: CI checks control-flow correctness on every push for free; the golden-data tests check actual data correctness, run manually, when it matters (e.g. before a release).

## Interpretation of open-ended requirements

- **Auth**: a single shared password gate, per the assignment FAQ's explicit allowance ("Is it ok if viewing the demo requires authentication? Yes").
- **"Production quality" scope**: prioritized correctness of the data layer (the FIPS join bug above) and graceful degradation over polish like token-by-token streaming — see REFLECTION.md for the explicit tradeoff.
- **Guardrails**: scoped to topic relevance + prompt-injection/abuse detection, not full content-moderation (no claim of catching all jailbreaks).
- **"Comprehensive mapping"**: interpreted as "don't hardcode which ACS demographic topics/columns are supported" rather than "support every table in the database regardless of relevance to population/demographic questions." The agent dynamically searches the *full* ACS metadata catalog (all table groups, all years, all 8,000+ field codes) via `search_census_tables`/`get_table_fields` rather than a hardcoded subset — but three present-but-unrelated table families (`*_CBG_GEOMETRY*` shape data, `*_CBG_PATTERNS` foot-traffic data, `2020_REDISTRICTING_*`) are deliberately out of scope, since they aren't demographic/population data and including them would mean spending the time budget on geospatial/mobility features instead of getting the core grounding correct.
- **Data source**: the assignment's required path -- the Snowflake Marketplace -- was accessible, so the SafeGraph CSV fallback was never needed. The specific Marketplace listing used is the free **"SafeGraph: US Open Census Data – Neighborhood Insights"** dataset (the assignment doesn't name an exact listing, just "the US Open Census dataset," and more than one Marketplace listing could plausibly match that description).
- **"Preserve context across multiple turns"**: interpreted as within a single browser session, not persisted across visits. Conversation history lives in Streamlit's `session_state`; refreshing the page or returning later starts a new, empty conversation. This satisfies the letter of the requirement (multi-turn follow-ups within a conversation work correctly) but is worth being explicit about, since a reviewer could reasonably read "preserve context" as implying durable, cross-visit memory -- which would require a real persistence layer (a database, a session ID) that wasn't built given the scope/time.
