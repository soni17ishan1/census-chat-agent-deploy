"""Full-agent golden tests: ask the real agent a question in plain English
and check the number in its written answer against the same official
ground truth used in test_golden_data.py.

This is that test's counterpart one level up the stack: test_golden_data.py
proves the SQL itself is correct, but can't catch a regression in how the
model translates a question into a query in the first place. Closes the
gap explicitly noted in REFLECTION.md.

Slower and costs real Anthropic + Snowflake calls on every run, so kept
small (two cases) and -- like test_golden_data.py -- run manually, not
wired into CI (see README's "Testing" section for why).
"""
import os
import re

import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("SNOWFLAKE_ACCOUNT") or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="requires live Snowflake and Anthropic credentials",
)

from agent.agent_loop import run_agent_turn  # noqa: E402

# Same official 2020 Decennial Census ground truth as test_golden_data.py,
# exercised through the full agent (real question -> real model -> real
# tool calls -> real Snowflake) instead of hand-written SQL.
GOLDEN_QUESTIONS = [
    ("What is the population of California?", 39_538_223, 0.10),
    ("What is the population of Texas?", 29_145_505, 0.10),
]


def _extract_population_figure(answer: str) -> float:
    """Pulls the first number-with-commas out of the answer text, e.g.
    "California's population is approximately 39,346,023." -> 39346023.0
    """
    match = re.search(r"[\d,]{4,}", answer)
    assert match, f"No number found in answer: {answer!r}"
    return float(match.group(0).replace(",", ""))


@pytest.mark.parametrize("question,expected,tolerance", GOLDEN_QUESTIONS)
def test_agent_answers_population_question_correctly(question, expected, tolerance):
    answer = run_agent_turn([{"role": "user", "content": question}])
    actual = _extract_population_figure(answer)
    lower, upper = expected * (1 - tolerance), expected * (1 + tolerance)
    assert lower <= actual <= upper, (
        f"{question!r}: expected ~{expected:,} (+/-{tolerance:.0%}), got {actual:,.0f} in: {answer}"
    )
