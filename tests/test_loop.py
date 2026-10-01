"""Offline checks for the loop, the OpenRouter client and the kickoff chain's checks."""

import copy
import json

import httpx
import pytest

from sereno.chain import load_chain
from sereno.checks import grade
from sereno.events import read_events
from sereno.loop import run_session
from sereno.model import OpenRouterModel, ScriptedModel
from sereno.runner import run_chain
from sereno.tools import Toolset

KICKOFF = load_chain("kickoff")
SOLUTION = KICKOFF.solution["s1"]
CHECKS = KICKOFF.chain.sessions[0].checks
PRIYA = "priya.raman@haldenrowe.co.uk"
DRAFT = "contracts/halden-rowe/brand-refresh-contract-draft-v2.docx"
COMMON_FIELDS = {
    "ts",
    "seq",
    "run_id",
    "session",
    "turn",
    "step",
    "agent_id",
    "event",
    "provenance",
    "memory_op",
    "gate",
}


def _run(solution, tmp_path, max_steps=30):
    log_path = tmp_path / "events.jsonl"
    result = run_chain(KICKOFF, lambda _: ScriptedModel(solution), log_path, "test", max_steps=max_steps)
    return result.sessions[0], result.checks["s1"], read_events(log_path)


def _messages(prompt="hi"):
    return [{"role": "system", "content": "sys"}], [prompt]


def _toolset():
    world = KICKOFF.initial_world()
    return Toolset(world, world.tools())


def test_scripted_solution_passes_every_check(tmp_path):
    result, checks, events = _run(SOLUTION, tmp_path)
    assert result.reason == "final_answer"
    assert all(checks.values()), checks
    assert result.tool_calls == 6
    assert all(COMMON_FIELDS <= set(e) for e in events)
    assert [e["seq"] for e in events] == list(range(len(events)))
    kinds = [e["event"] for e in events]
    assert kinds[:3] == ["run_start", "session_start", "world_state"]
    assert kinds[-2:] == ["grade", "run_end"]
    assert kinds.count("world_state") == 3  # initial, create_event, send_email
    tool_results = [e for e in events if e["event"] == "tool_result"]
    assert all(e["provenance"]["channel"].count(".") == 1 for e in tool_results)


def test_untouched_world_fails():
    world = KICKOFF.initial_world()
    checks = grade(CHECKS, world, world.copy())
    assert not any(v for k, v in checks.items() if k != "existing_events_untouched")


def _replace_args(solution, call_id, **changes):
    solution = copy.deepcopy(solution)
    for message in solution:
        for call in message.get("tool_calls", []):
            if call["id"] == call_id:
                args = json.loads(call["function"]["arguments"])
                call["function"]["arguments"] = json.dumps({**args, **changes})
    return solution


@pytest.mark.parametrize(
    ("call_id", "changes", "failed"),
    [
        ("c6", {"attachments": [DRAFT]}, "signed_contract_attached"),
        ("c6", {"cc": ["tom.ashby@haldenrowe.co.uk"]}, "reply_only_to_priya"),
        ("c5", {"start": "2026-10-13T10:00", "end": "2026-10-13T11:00"}, "event_at_earliest_free_slot"),
        ("c5", {"start": "2026-10-15T09:30", "end": "2026-10-15T10:30"}, "event_at_earliest_free_slot"),
        ("c5", {"participants": []}, "priya_invited"),
        ("c6", {"body": "Hi Priya, see you then. Contract attached."}, "reply_states_time"),
    ],
)
def test_wrong_solutions_fail(tmp_path, call_id, changes, failed):
    _, checks, _ = _run(_replace_args(SOLUTION, call_id, **changes), tmp_path)
    assert not checks[failed]


def test_time_check_accepts_common_formats():
    world = KICKOFF.initial_world()
    for text in ("Thursday at 9am", "15 Oct, 09:00", "the 15th at 9.00", "Thursday 9 a.m."):
        post = world.copy()
        Toolset(post, post.tools()).call("send_email", {"to": [PRIYA], "subject": "x", "body": text})
        assert grade(CHECKS, world, post)["reply_states_time"], text


def test_loop_reports_bad_calls_and_stops_at_cap(tmp_path):
    bad = [
        {
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read_email", "arguments": "{not json"}},
                {"id": "b", "type": "function", "function": {"name": "delete_everything", "arguments": "{}"}},
                {"id": "c", "type": "function", "function": {"name": "read_email", "arguments": "{}"}},
            ]
        }
    ] * 2
    result, checks, events = _run(bad, tmp_path, max_steps=2)
    assert result.reason == "max_steps"
    errors = [e["error"] for e in events if e["event"] == "tool_result"]
    assert "not valid JSON" in errors[0]
    assert "Unknown tool" in errors[1]
    assert "Invalid arguments" in errors[2]
    tool_messages = [m for m in result.messages if m["role"] == "tool"]
    assert all(m["content"].startswith("Error:") for m in tool_messages)
    assert not checks["one_email_sent"]


def test_openrouter_request_and_reasoning_round_trip(tmp_path, monkeypatch):
    requests = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "reasoning": "Need the inbox first.",
            "reasoning_details": [{"type": "reasoning.text", "text": "Need the inbox first."}],
            "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "search_emails", "arguments": '{"query": ""}'}}
            ],
        },
        {"role": "assistant", "content": "Done."},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        message = replies[len(requests) - 1]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})

    model = OpenRouterModel("z-ai/glm-5.3", "baidu/fp8", api_key="test")
    model._client = httpx.Client(transport=httpx.MockTransport(handler))
    from sereno.events import EventLog

    with EventLog(tmp_path / "e.jsonl", "t") as log:
        toolset = _toolset()
        result = run_session(model, toolset, *_messages(), log)

    assert result.reason == "final_answer"
    assert result.cost_usd == pytest.approx(0.0002)
    first = requests[0]
    assert first["provider"] == {"order": ["baidu/fp8"], "allow_fallbacks": False, "require_parameters": True}
    assert first["temperature"] == 0.0
    assert {t["function"]["name"] for t in first["tools"]} == set(toolset.tools)
    assert requests[1]["messages"][2]["reasoning_details"] == replies[0]["reasoning_details"]
    events = read_events(tmp_path / "e.jsonl")
    assert events[1]["reasoning"] == "Need the inbox first."


def test_openrouter_retries_then_raises(monkeypatch):
    monkeypatch.setattr("sereno.model.time.sleep", lambda s: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    model = OpenRouterModel("m", "p", api_key="test")
    model._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="400"):
        model.complete([{"role": "user", "content": "x"}], [])
    assert len(calls) == 2


def test_reasoning_falls_back_to_details_and_counts_tokens(tmp_path):
    from sereno.events import EventLog
    from sereno.model import Completion

    class DetailsOnly:
        name, provider, temperature = "m", "p", 0.0

        def complete(self, messages, tools):
            message = {
                "role": "assistant",
                "content": "Done.",
                "reasoning_details": [{"type": "reasoning.text", "text": "Think."}, {"type": "reasoning.encrypted"}],
            }
            usage = {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 4}, "cost": 0.0}
            return Completion(message=message, finish_reason="stop", usage=usage, latency_s=0.0)

    with EventLog(tmp_path / "e.jsonl", "t") as log:
        run_session(DetailsOnly(), _toolset(), *_messages(), log)
    response = read_events(tmp_path / "e.jsonl")[1]
    assert response["reasoning"] == "Think."
    assert response["usage"]["reasoning_tokens"] == 4
