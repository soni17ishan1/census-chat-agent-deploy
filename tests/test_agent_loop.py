from unittest.mock import MagicMock, patch

from agent import agent_loop


def _text_block(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(name, tool_input, tool_id="tool_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = tool_input
    block.id = tool_id
    return block


def _response(content_blocks, stop_reason):
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    return resp


@patch("agent.agent_loop._execute_tool")
@patch("agent.agent_loop.anthropic.Anthropic")
def test_run_agent_turn_executes_tool_then_returns_final_text(mock_anthropic_cls, mock_execute_tool):
    mock_execute_tool.return_value = {"results": [{"TABLE_NUMBER": "B01001"}]}
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _response(
            [_tool_use_block("search_census_tables", {"keyword": "population"})],
            stop_reason="tool_use",
        ),
        _response([_text_block("California's population is ~39.3 million.")], stop_reason="end_turn"),
    ]
    mock_anthropic_cls.return_value = mock_client

    messages = [{"role": "user", "content": "What is the population of California?"}]
    answer = agent_loop.run_agent_turn(messages)

    assert "39.3 million" in answer
    mock_execute_tool.assert_called_once_with("search_census_tables", {"keyword": "population"})
    # the tool result must have been fed back to the model as a user turn
    tool_result_turn = messages[-2]
    assert tool_result_turn["role"] == "user"
    assert tool_result_turn["content"][0]["type"] == "tool_result"


@patch("agent.agent_loop._execute_tool")
@patch("agent.agent_loop.anthropic.Anthropic")
def test_run_agent_turn_gives_up_after_max_iterations(mock_anthropic_cls, mock_execute_tool):
    mock_execute_tool.return_value = {"results": []}
    mock_client = MagicMock()
    # Model never stops calling tools.
    mock_client.messages.create.return_value = _response(
        [_tool_use_block("run_sql", {"sql": "SELECT 1"})], stop_reason="tool_use"
    )
    mock_anthropic_cls.return_value = mock_client

    messages = [{"role": "user", "content": "some unanswerable question"}]
    answer = agent_loop.run_agent_turn(messages)

    assert "rephrase" in answer.lower() or "narrow" in answer.lower()
    assert mock_client.messages.create.call_count == agent_loop.MAX_ITERATIONS


@patch("agent.agent_loop.anthropic.Anthropic")
def test_run_agent_turn_respects_soft_deadline(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client

    with patch("agent.agent_loop.time.monotonic", side_effect=[0, 1000, 1000]):
        messages = [{"role": "user", "content": "anything"}]
        answer = agent_loop.run_agent_turn(messages)

    # Deadline already exceeded before the first model call.
    mock_client.messages.create.assert_not_called()
    assert "longer than expected" in answer


def test_execute_tool_dispatches_search_census_tables():
    with patch("agent.agent_loop.schema_tools.search_census_tables") as mock_search:
        mock_search.return_value = [{"TABLE_NUMBER": "B19001"}]
        result = agent_loop._execute_tool("search_census_tables", {"keyword": "income"})
    assert result == {"results": [{"TABLE_NUMBER": "B19001"}]}
    mock_search.assert_called_once_with("income")


def test_execute_tool_dispatches_run_sql():
    with patch("agent.agent_loop.run_select") as mock_run_select:
        mock_run_select.return_value = {"columns": ["X"], "rows": [(1,)], "row_count": 1}
        result = agent_loop._execute_tool("run_sql", {"sql": "SELECT 1 AS X"})
    assert result["row_count"] == 1
    mock_run_select.assert_called_once_with("SELECT 1 AS X")


def test_execute_tool_catches_exceptions_instead_of_crashing():
    with patch("agent.agent_loop.schema_tools.get_table_fields", side_effect=RuntimeError("boom")):
        result = agent_loop._execute_tool("get_table_fields", {"table_number": "B01001"})
    assert "error" in result
    assert "boom" in result["error"]


def _user_turn(text):
    return {"role": "user", "content": text}


def _assistant_tool_use_turn(tool_id="t1"):
    return {"role": "assistant", "content": [_tool_use_block("run_sql", {"sql": "SELECT 1"}, tool_id)]}


def _tool_result_turn(tool_id="t1"):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "{}"}]}


def _full_turn(question, tool_id):
    """A realistic turn: user question, assistant tool call, tool result, final answer."""
    return [
        _user_turn(question),
        _assistant_tool_use_turn(tool_id),
        _tool_result_turn(tool_id),
        {"role": "assistant", "content": [_text_block("answer")]},
    ]


def test_trim_history_noop_when_under_limit():
    messages = _full_turn("q1", "t1") + _full_turn("q2", "t2")
    trimmed, did_trim = agent_loop.trim_history(messages, max_turns=8)
    assert trimmed == messages
    assert did_trim is False


def test_trim_history_drops_oldest_turns_when_over_limit():
    turns = [_full_turn(f"q{i}", f"t{i}") for i in range(10)]
    messages = [msg for turn in turns for msg in turn]

    trimmed, did_trim = agent_loop.trim_history(messages, max_turns=3)

    assert did_trim is True
    # only the last 3 user questions should remain
    user_questions = [m["content"] for m in trimmed if m["role"] == "user" and isinstance(m["content"], str)]
    assert user_questions == ["q7", "q8", "q9"]


def test_trim_history_never_splits_a_tool_use_pair():
    turns = [_full_turn(f"q{i}", f"t{i}") for i in range(5)]
    messages = [msg for turn in turns for msg in turn]

    trimmed, _ = agent_loop.trim_history(messages, max_turns=2)

    # every tool_use block must have its matching tool_result still present
    tool_use_ids = {
        block.id
        for m in trimmed
        if m["role"] == "assistant"
        for block in (m["content"] if isinstance(m["content"], list) else [])
        if getattr(block, "type", None) == "tool_use"
    }
    tool_result_ids = {
        block["tool_use_id"]
        for m in trimmed
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_result"
    }
    assert tool_use_ids == tool_result_ids


def test_execute_tool_unknown_tool_name():
    result = agent_loop._execute_tool("not_a_real_tool", {})
    assert "error" in result


@patch("agent.agent_loop._execute_tool")
@patch("agent.agent_loop.anthropic.Anthropic")
def test_run_agent_turn_reports_progress_per_tool_call(mock_anthropic_cls, mock_execute_tool):
    mock_execute_tool.return_value = {"results": []}
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _response(
            [_tool_use_block("search_census_tables", {"keyword": "income"})],
            stop_reason="tool_use",
        ),
        _response([_text_block("done")], stop_reason="end_turn"),
    ]
    mock_anthropic_cls.return_value = mock_client

    progress_messages = []
    messages = [{"role": "user", "content": "What is median income?"}]
    agent_loop.run_agent_turn(messages, on_progress=progress_messages.append)

    assert len(progress_messages) == 1
    assert "income" in progress_messages[0]
