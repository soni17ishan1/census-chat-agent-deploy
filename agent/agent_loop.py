"""Claude tool-use agent loop: explores schema, runs SQL, answers grounded
in the actual returned rows. No answer is generated except as the final
turn after tool results are visible to the model, so it can't hallucinate
numbers that didn't come back from Snowflake.
"""
import json
import time
from typing import Callable, Optional

import anthropic

from agent import schema_tools
from agent.snowflake_client import run_select

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 8
SOFT_DEADLINE_SECONDS = 45  # leaves headroom under the 60s end-to-end budget

# Every tool call within a turn (search/lookup/SQL results) also gets added
# to the message history that's resent to Claude on every subsequent turn --
# so token usage (cost + latency) grows with conversation length much faster
# than the number of user questions alone suggests. This bounds it.
MAX_TURNS_IN_CONTEXT = 8

TOOLS = [
    {
        "name": "search_census_tables",
        "description": (
            "Search the ACS table catalog by keyword (e.g. 'income', 'age', "
            "'commute', 'race') to find candidate table_number values and "
            "what each table covers. Always call this before guessing a "
            "table/column."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
    },
    {
        "name": "get_table_fields",
        "description": (
            "List every column code and human-readable description for a "
            "given table_number (e.g. 'B01001'), so you can pick the exact "
            "column for the concept being asked about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table_number": {"type": "string"}},
            "required": ["table_number"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Execute a single read-only SELECT against the census database. "
            "Only SELECT/WITH statements are permitted. Results are capped "
            "at 200 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]

SYSTEM_PROMPT = f"""You are a careful data analyst answering questions about \
US Census demographic data. {schema_tools.SCHEMA_PRIMER}

Rules:
- NEVER state a number that didn't come from a run_sql result. If a tool \
call errors or returns no rows, say so plainly and explain what you tried, \
rather than guessing or making up a plausible-sounding figure.
- If the question is ambiguous (e.g. could mean state OR county level, or \
multiple census years could apply), pick a reasonable default, state which \
interpretation you used, and invite the user to clarify if they meant \
something else.
- If the question partially matches available data (e.g. asks for a metric \
that doesn't exist but a related one does), say what's not available and \
offer the closest available alternative instead of refusing outright.
- If the question is reasonable but genuinely unanswerable from this \
dataset (e.g. city/place-level data, years outside 2019-2020, topics not \
covered by any ACS table), say so clearly and explain why.
- Keep final answers concise and concrete: lead with the number/fact, then \
brief supporting detail. Mention the source year and geography level.
"""


class AgentTimeoutError(RuntimeError):
    pass


def _progress_message(name: str, tool_input: dict) -> str:
    if name == "search_census_tables":
        return f"Searching Census tables for \"{tool_input.get('keyword', '')}\"..."
    if name == "get_table_fields":
        return f"Looking up columns in table {tool_input.get('table_number', '')}..."
    if name == "run_sql":
        return "Querying Snowflake..."
    return f"Running {name}..."


def _execute_tool(name: str, tool_input: dict) -> dict:
    try:
        if name == "search_census_tables":
            return {"results": schema_tools.search_census_tables(tool_input["keyword"])}
        if name == "get_table_fields":
            return {"results": schema_tools.get_table_fields(tool_input["table_number"])}
        if name == "run_sql":
            return run_select(tool_input["sql"])
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:  # tool execution must never crash the agent loop
        return {"error": f"{type(e).__name__}: {e}"}


def trim_history(messages: list[dict], max_turns: int = MAX_TURNS_IN_CONTEXT) -> tuple[list[dict], bool]:
    """Keep only the most recent `max_turns` user *questions*, along with
    each one's full tool-use trace, dropping older turns wholesale.

    A "turn boundary" is a real user question -- a message with plain
    string content, as opposed to a list of tool_result blocks. We only
    ever cut at a boundary: cutting in the middle of a turn would split a
    tool_use block from its required tool_result, which the Anthropic API
    rejects as an invalid request.

    Returns (trimmed_messages, did_trim) so callers can tell the user when
    older context was actually dropped.
    """
    boundaries = [
        i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)
    ]
    if len(boundaries) <= max_turns:
        return messages, False
    cutoff = boundaries[-max_turns]
    return messages[cutoff:], True


def run_agent_turn(
    messages: list[dict], on_progress: Optional[Callable[[str], None]] = None
) -> str:
    """Runs one user turn to completion, mutating `messages` in place with
    the full tool-use trace so the next turn has full context.

    on_progress, if given, is called with a short human-readable status
    string before each tool call -- lets the UI show real step-by-step
    progress instead of one static "please wait" message for the whole
    (up to 45s) loop.
    """
    client = anthropic.Anthropic()
    deadline = time.monotonic() + SOFT_DEADLINE_SECONDS

    for _ in range(MAX_ITERATIONS):
        if time.monotonic() > deadline:
            messages.append(
                {
                    "role": "assistant",
                    "content": "This is taking longer than expected to look up. "
                    "Could you narrow your question (e.g. a specific state or topic)?",
                }
            )
            return messages[-1]["content"]

        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            return text or "I couldn't generate a response. Please try rephrasing your question."

        tool_result_blocks = []
        for block in response.content:
            if block.type == "tool_use":
                if on_progress:
                    on_progress(_progress_message(block.name, block.input))
                result = _execute_tool(block.name, block.input)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        messages.append({"role": "user", "content": tool_result_blocks})

    fallback = (
        "I wasn't able to settle on an answer within a reasonable number of steps. "
        "Could you rephrase or narrow your question (e.g. specify a state, topic, or year)?"
    )
    messages.append({"role": "assistant", "content": fallback})
    return fallback
