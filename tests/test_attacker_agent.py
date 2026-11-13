"""The bounded tool loop: tool execution, finish handling, step limits and usage."""

import pytest
from attacker_world import INERT, NOTE, ScriptedProvider, call, completion, make_config, make_request

from sereno.attacker.agent import run_agent
from sereno.attacker.config import LoopSettings
from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ProviderFailure
from sereno.attacker.providers.base import ToolTurn, UserTurn


def test_loop_reads_code_stages_and_finishes(tmp_path):
    config = make_config(tmp_path)
    provider = ScriptedProvider(
        completion(call("read_file", path="/app/pkg/core.py")),
        completion(
            call("stage_action", op="write", path=NOTE, content=INERT),
            call("set_strategy", text="next strategy"),
            call("set_notebook", text="seen the code"),
            call("finish", note="done"),
        ),
    )
    result = run_agent(config, make_request(), provider, Deadline(120))
    assert result.finish == "finish" and result.steps == 2
    assert result.response["actions"] == [{"op": "write", "path": NOTE, "content": INERT}]
    assert result.response["strategy"] == "next strategy"
    assert result.response["notebook"] == "seen the code"
    usage = result.response["writer_usage"]
    assert usage["usage"]["total_tokens"] == 30 and usage["calls"] == 2
    assert usage["config_sha256"] == config.config_sha256
    assert usage["prompt_sha256"] == config.prompt_sha256
    tool_turns = [turn for turn in provider.turns[1] if isinstance(turn, ToolTurn)]
    assert tool_turns[0].name == "read_file" and "def alpha" in tool_turns[0].text


def test_loop_returns_queued_reads_and_strategy(tmp_path):
    provider = ScriptedProvider(
        completion(
            call("read_file", path="/app/README.md"),
            call("set_strategy", text="read the next file"),
        ),
        completion(call("finish", note="done")),
    )
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert result.response["reads"] == ["/app/README.md"]
    assert result.response["strategy"] == "read the next file"
    assert "notebook" not in result.response


def test_a_text_only_answer_is_nudged_then_finishes(tmp_path):
    provider = ScriptedProvider(
        completion(text="I will inspect first.", finish="stop"),
        completion(call("finish", note="done")),
    )
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert result.finish == "finish"
    assert any(isinstance(turn, UserTurn) and "No tool call" in turn.text for turn in provider.turns[1])


def test_max_steps_returns_the_staged_state(tmp_path):
    config = make_config(tmp_path, agent=LoopSettings(max_steps=2))
    provider = ScriptedProvider(completion(call("list_sessions")), completion(call("list_sessions")))
    result = run_agent(config, make_request(), provider, Deadline(120))
    assert result.finish == "max_steps" and result.steps == 2
    assert result.response["actions"] == []


def test_over_the_per_step_limit_is_denied_and_the_loop_continues(tmp_path):
    config = make_config(tmp_path, agent=LoopSettings(max_tool_calls_per_step=1))
    provider = ScriptedProvider(
        completion(call("list_sessions"), call("finish", note="too early")),
        completion(call("finish", note="done")),
    )
    result = run_agent(config, make_request(), provider, Deadline(120))
    assert result.finish == "finish"
    denied = [turn for turn in provider.turns[1] if isinstance(turn, ToolTurn) and turn.name == "finish"]
    assert denied and denied[0].failed and "per-step limit" in denied[0].text


def test_an_invalid_finish_does_not_end_the_call(tmp_path):
    provider = ScriptedProvider(
        completion(call("finish", note="x" * 3000)),
        completion(call("finish", note="done")),
    )
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert result.finish == "finish"
    denied = [turn for turn in provider.turns[1] if isinstance(turn, ToolTurn)]
    assert denied and denied[0].failed and "2000" in denied[0].text


def test_the_deadline_ends_before_a_model_call(tmp_path):
    provider = ScriptedProvider(completion(call("finish")))
    with pytest.raises(ProviderFailure) as caught:
        run_agent(make_config(tmp_path), make_request(), provider, Deadline(0))
    assert caught.value.code == "deadline"
    assert provider.turns == []


def test_usage_is_empty_when_a_completion_reports_none(tmp_path):
    provider = ScriptedProvider(completion(call("finish"), usage={}))
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert result.response["writer_usage"]["usage"] == {}
    assert result.response["writer_usage"]["calls"] == 1
