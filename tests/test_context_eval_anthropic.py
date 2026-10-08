"""The Anthropic SDK agent: the same requests as mini-swe's litellm path, native replies, refusals, limits and costs.

Requests go to an in-process transport or a local stub server; nothing leaves the machine.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import test_context_eval as base
import test_context_eval_engine as engine_tests
import test_context_eval_runtime as runtime_tests
import yaml
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import Submitted
from minisweagent.models import get_model
from pydantic import ValidationError
from rich.console import Console
from test_context_eval_engine import read_log, runtime_for

from sereno.context_eval.agents import AnthropicAdapter, action_timeout, mini_swe_config
from sereno.context_eval.anthropic_model import AnthropicModel
from sereno.context_eval.config import default_registry, load_config, validate
from sereno.context_eval.engine import assistant_messages, message_text, reasoning_text
from sereno.context_eval.metrics import first_step
from sereno.context_eval.runner import run_campaign
from sereno.context_eval.schema import Check, MemoryConfig, Session
from sereno.context_eval.watch import watch
from sereno.pricing import anthropic_cost

fixture_world = base.fixture_world
template, factory = engine_tests.template, engine_tests.factory
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
KWARGS = {"max_tokens": 4096, "thinking": {"type": "adaptive", "display": "summarized"}}


def native(*content, stop_reason="tool_use", usage=None, **fields):
    return {
        "id": f"msg_{len(content)}",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 50},
        **fields,
    }


def bash(call_id, command):
    return {"type": "tool_use", "id": call_id, "name": "bash", "input": {"command": command}}


def thinking(text, signature):
    return {"type": "thinking", "thinking": text, "signature": signature}


# Thinking with text and a call, then parallel calls, a reply without a call, and the submission.
SCENARIO = [
    native(thinking("CI_THINKING_SUMMARY", "sig-1"), {"type": "text", "text": "Looking around."}, bash("t1", "ls")),
    native(thinking("", "sig-2"), bash("t2", "cat a"), bash("t3", "cat b")),
    native({"type": "text", "text": "No call here."}, stop_reason="end_turn"),
    native(bash("t4", SUBMIT)),
]


class Transport:
    """Scripted replies through the SDK's own HTTP stack; every request body is kept."""

    def __init__(self, replies, delay=0.0):
        self.replies, self.bodies, self.delay = list(replies), [], delay

    def __call__(self, request):
        self.bodies.append(json.loads(request.content))
        time.sleep(self.delay)
        reply = self.replies[len(self.bodies) - 1]
        headers = {"request-id": f"req_{len(self.bodies)}"}
        if isinstance(reply, int):
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": "CI_ERROR"}}
            return httpx2.Response(reply, json=error, headers=headers)
        return httpx2.Response(200, json=reply, headers=headers)

    def client(self):
        return anthropic.Anthropic(
            api_key="test", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(self))
        )


class Stub(BaseHTTPRequestHandler):
    bodies, replies = [], []

    def do_POST(self):
        Stub.bodies.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
        reply = json.dumps(Stub.replies[len(Stub.bodies) - 1]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class Environment:
    def execute(self, action, **kwargs):
        if action["command"] == SUBMIT:
            raise Submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted", "submission": ""}})
        return {"output": f"output of {action['command']}", "returncode": 0, "exception_info": ""}

    def get_template_vars(self):
        return {"cwd": "/app", "system": "Linux", "release": "", "version": "", "machine": "x86_64"}

    def serialize(self):
        return {}


def model_file(tmp_path, name, model):
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"model": model}))
    return path


def sdk_settings(tmp_path, kwargs=KWARGS, model_name="claude-haiku-5-5"):
    """mini.yaml merged with an Anthropic agent model config, as a session gets it."""
    return mini_swe_config(model_file(tmp_path, "sdk", {"model_name": model_name, "model_kwargs": kwargs}), "anthropic")


def run_loop(model, settings):
    agent = DefaultAgent(model, Environment(), **settings["agent"])
    return agent.run("Fix the CI_TASK_FIXTURE issue.")


def test_requests_equal_the_ones_litellm_sends(tmp_path):
    """mini-swe's DefaultAgent with each model, the same replies: the SDK sends litellm's request bodies."""
    kwargs = KWARGS | {"output_config": {"effort": "xhigh"}}
    settings = sdk_settings(tmp_path, kwargs)
    transport = Transport(SCENARIO)
    model = AnthropicModel(client=transport.client(), **settings["model"])
    assert run_loop(model, settings)["exit_status"] == "Submitted"

    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Stub.bodies, Stub.replies = [], SCENARIO
    try:
        stub = {"api_base": f"http://127.0.0.1:{server.server_port}", "api_key": "test"}
        litellm_file = model_file(
            tmp_path,
            "litellm",
            {
                "model_name": "anthropic/claude-haiku-5-5",
                "cost_tracking": "ignore_errors",
                "model_kwargs": kwargs | stub,
            },
        )
        litellm_settings = mini_swe_config(litellm_file)
        assert run_loop(get_model(config=litellm_settings["model"]), litellm_settings)["exit_status"] == "Submitted"
    finally:
        server.shutdown()

    assert len(transport.bodies) == len(Stub.bodies) == 4
    assert transport.bodies == Stub.bodies
    first, last = transport.bodies[0], transport.bodies[-1]
    assert first["tools"] == [
        {
            "name": "bash",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The bash command to execute"}},
                "required": ["command"],
            },
            "type": "custom",
            "description": "Execute a bash command",
        }
    ]
    assert first["system"] == [
        {"type": "text", "text": "You are a helpful assistant that can interact with a computer."}
    ]
    assert (first["thinking"], first["output_config"], first["max_tokens"]) == (
        KWARGS["thinking"],
        {"effort": "xhigh"},
        4096,
    )
    # One breakpoint, on the last block: the format error after the two tool results, all in one user turn.
    turn = last["messages"][-1]
    assert [b["type"] for b in turn["content"]] == ["tool_result", "tool_result", "text"]
    assert turn["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "No tool calls found" in turn["content"][-1]["text"]
    assert json.dumps(last).count("cache_control") == 1
    # The reply without a call stays out of the history, as mini-swe keeps it out.
    assert "No call here." not in json.dumps(last)


def test_thinking_blocks_return_unchanged_in_their_order(tmp_path):
    """Returned blocks are replayed as they came, redacted thinking and thinking after text included."""
    blocks = [
        thinking("CI_THINKING_SUMMARY", "sig-1"),
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": "Next."},
        thinking("CI_LATE", "sig-2"),
        bash("t1", "ls"),
    ]
    transport = Transport([native(*blocks), native(bash("t2", SUBMIT))])
    settings = sdk_settings(tmp_path)
    run_loop(AnthropicModel(client=transport.client(), **settings["model"]), settings)
    assert transport.bodies[1]["messages"][1] == {"role": "assistant", "content": blocks}


def adapter_config(tmp_path, kwargs=KWARGS, cost_limit=1.0, wall=60):
    path = model_file(tmp_path, "model", {"model_name": "claude-haiku-5-5", "model_kwargs": kwargs})
    return SimpleNamespace(
        model_config_file=path, memory=MemoryConfig(), cost_limit_usd=cost_limit, wall_time_limit_seconds=wall
    )


def run_adapter(tmp_path, factory, replies, session=None, **options):
    tmp_path.mkdir(exist_ok=True)
    runtime = runtime_for(tmp_path, factory, [])
    transport = Transport(replies, delay=options.pop("delay", 0.0))
    outcome = AnthropicAdapter(client=transport.client()).run(
        runtime, "task", "", adapter_config(tmp_path, **options), session or Session(id="s")
    )
    return outcome, read_log(runtime.log.path), transport


def test_a_session_logs_replies_the_readers_understand(tmp_path, factory):
    outcome, events, _ = run_adapter(tmp_path, factory, SCENARIO[:1] + SCENARIO[2:])
    assert (outcome["exit_status"], outcome["steps"]) == ("Submitted", 3)
    results = [e for e in events if e["kind"] == "model_result"]
    assert [r.get("format_error") for r in results] == [None, True, None]
    first = results[0]["message"]
    assert reasoning_text(first) == "CI_THINKING_SUMMARY"
    assert message_text([first]) == "Looking around."
    assert first["extra"]["request_id"] == "req_1" and first["extra"]["response"]["stop_reason"] == "tool_use"
    # A reply without a call keeps its text in its event though it stays out of the history.
    assert message_text([results[1]["message"]]) == "No call here."
    assert "No call here." in message_text(assistant_messages(events))
    check = Check(id="thought", source="reasoning", contains="CI_THINKING_SUMMARY").model_dump()
    assert first_step(check, events) == 1
    # The observation is plain text in the logged context, so exposure matching reads it.
    observation = [e for e in events if e["kind"] == "context_sent"][1]["messages"][-1]
    assert (observation["role"], observation["tool_call_id"]) == ("tool", "t1")
    assert message_text([observation]) == observation["content"] and '"returncode": 0' in observation["content"]
    # The cost of every reply, the format error's included, is the session's cost.
    usage = SCENARIO[0]["usage"]
    assert outcome["cost_usd"] == pytest.approx(3 * anthropic_cost("claude-haiku-5-5", usage))
    assert sum(r["cost_usd"] for r in results) == pytest.approx(outcome["cost_usd"])
    # The last reply with visible text, a format error included, as in the litellm path.
    assert outcome["final"] == "No call here."
    assert "stop_details" not in outcome


def test_watch_shows_native_usage_thinking_and_a_refusal(tmp_path, factory):
    refusal = native(
        {"type": "text", "text": "Partial."},
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "CI_EXPLANATION"},
    )
    _, events, _ = run_adapter(tmp_path, factory, [SCENARIO[0], refusal])
    log = tmp_path / "watched" / "events.jsonl"
    log.parent.mkdir()
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    console = Console(record=True, width=200, force_terminal=False, color_system=None)
    assert watch(log, console=console, interval=0.01) == 0
    text = console.export_text()
    for expected in (
        "in 150 (cache read 0, write 50) out 20",
        "CI_THINKING_SUMMARY",
        "Looking around.",
        "$ ls",
        "refused by the provider (category cyber): the session ends",
    ):
        assert expected in text


def test_a_refusal_ends_the_session_with_its_own_status(tmp_path, factory):
    details = {"type": "refusal", "category": None, "explanation": "CI_EXPLANATION"}
    refusal = native(thinking("", "sig"), bash("t9", "ls"), stop_reason="refusal", stop_details=details)
    outcome, events, _ = run_adapter(tmp_path, factory, [SCENARIO[0], refusal])
    assert (outcome["exit_status"], outcome["steps"], outcome["stop_details"]) == ("Refused", 2, details)
    # Billed like any reply; its tool call never runs.
    assert outcome["cost_usd"] == pytest.approx(2 * anthropic_cost("claude-haiku-5-5", SCENARIO[0]["usage"]))
    assert [e["command"] for e in events if e["kind"] == "action"] == ["ls"]
    last = [e for e in events if e["kind"] == "model_result"][-1]
    assert (last["refusal"], last["stop_details"], last.get("format_error")) == (True, details, None)
    assert outcome["messages"][-1] == {
        "role": "exit",
        "content": "Refused",
        "extra": {"exit_status": "Refused", "submission": "", "stop_details": details},
    }


def test_a_cut_off_reply_gets_mini_swe_length_message(tmp_path, factory):
    cut = native(thinking("long", "sig"), stop_reason="max_tokens")
    outcome, _, transport = run_adapter(tmp_path, factory, [cut, SCENARIO[3]])
    assert outcome["exit_status"] == "Submitted"
    error = transport.bodies[1]["messages"][-1]["content"][-1]["text"]
    assert error.startswith("Your previous response reached the output token limit (finish_reason=length)")


def test_repeated_format_errors_end_the_session_as_in_mini_swe(tmp_path, factory):
    prose = native({"type": "text", "text": "prose"}, stop_reason="end_turn")
    outcome, events, _ = run_adapter(tmp_path, factory, [prose] * 3)
    assert (outcome["exit_status"], outcome["steps"]) == ("RepeatedFormatError", 3)
    assert [e.get("format_error") for e in events if e["kind"] == "model_result"] == [True] * 3


def test_step_cost_and_time_limits(tmp_path, factory):
    calls = [native(bash(f"t{i}", "true")) for i in range(3)]
    outcome, _, _ = run_adapter(tmp_path / "steps", factory, calls, Session(id="s", max_steps=2))
    assert (outcome["exit_status"], outcome["steps"]) == ("LimitsExceeded", 2)
    outcome, _, _ = run_adapter(tmp_path / "cost", factory, calls, cost_limit=1e-6)
    assert (outcome["exit_status"], outcome["steps"]) == ("LimitsExceeded", 1)
    outcome, _, _ = run_adapter(tmp_path / "time", factory, calls, wall=1, delay=1.05)
    assert (outcome["exit_status"], outcome["steps"]) == ("TimeExceeded", 1)


def test_a_failed_request_logs_its_request_id(tmp_path, factory):
    with pytest.raises(anthropic.BadRequestError):
        run_adapter(tmp_path, factory, [400])
    events = read_log(tmp_path / "s.jsonl")
    errors = [e for e in events if e["kind"] == "model_error"]
    assert [(e["error_type"], e["status_code"], e["request_id"]) for e in errors] == [("BadRequestError", 400, "req_1")]


def test_cost_counts_cache_tiers_and_long_prompts(tmp_path):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 4000,
        "cache_creation_input_tokens": 3000,
        "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000},
    }
    transport = Transport(
        [
            native(bash("t1", "ls"), usage=usage),
            native(bash("t2", "ls"), usage={"input_tokens": 100001, "output_tokens": 10}),
        ]
    )
    model = AnthropicModel(client=transport.client(), **sdk_settings(tmp_path)["model"])
    first = model.query([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert first["extra"]["cost"] == pytest.approx((1000 + 2500 + 2000 + 400) * 0.10e-6 + 200 * 0.50e-6)
    second = model.query([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert second["extra"]["cost"] == pytest.approx(100001 * 0.50e-6 + 10 * 2.50e-6)


def test_an_unpriced_model_fails_rather_than_counting_free(tmp_path):
    reply = native(bash("t1", "ls")) | {"model": "claude-unknown"}
    model = AnthropicModel(client=Transport([reply]).client(), **sdk_settings(tmp_path)["model"])
    with pytest.raises(RuntimeError, match="no price"):
        model.query([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])


@pytest.mark.parametrize(
    ("model", "error"),
    [
        ({"model_name": "claude-haiku-5-5", "model_kwargs": {"max_tokens": 10, "drop_params": True}}, "drop_params"),
        ({"model_name": "claude-haiku-5-5", "model_kwargs": {"thinking": {"type": "adaptive"}}}, "max_tokens"),
        ({"model_name": "claude-haiku-5-5", "model_kwargs": {"max_tokens": 10, "messages": []}}, "messages"),
        (
            {"model_class": "litellm", "model_name": "claude-haiku-5-5", "model_kwargs": {"max_tokens": 10}},
            "model_class",
        ),
        (
            {"model_name": "claude-haiku-5-5", "cost_tracking": "default", "model_kwargs": {"max_tokens": 10}},
            "cost_tracking",
        ),
    ],
)
def test_model_config_errors(tmp_path, model, error):
    with pytest.raises(ValidationError, match=error):
        AnthropicModel(
            client=Transport([]).client(), **mini_swe_config(model_file(tmp_path, "m", model), "anthropic")["model"]
        )


def test_the_agent_takes_mini_yaml_templates_and_settings_but_not_its_litellm_kwargs(tmp_path):
    path = model_file(tmp_path, "m", {"model_name": "claude-haiku-5-5", "model_kwargs": {"max_tokens": 10}})
    merged = mini_swe_config(path, "anthropic")
    assert merged["model"]["model_kwargs"] == {"max_tokens": 10}
    assert merged["model"]["format_error_template"] == mini_swe_config(path)["model"]["format_error_template"]
    assert mini_swe_config(path)["model"]["model_kwargs"]["drop_params"] is True
    merged["environment"]["timeout"] = 45
    (tmp_path / "frozen.yaml").write_text(yaml.safe_dump(merged))
    config = SimpleNamespace(agent="anthropic", model_config_file=path, _agent_config_file=tmp_path / "frozen.yaml")
    assert action_timeout(config) == 45


def test_validate_accepts_the_example_and_rejects_an_unpriced_model(tmp_path):
    examples = Path(__file__).parents[1] / "examples" / "context-eval"
    config = load_config(examples / "clean-chain.yaml")
    if not config.dataset_root.is_dir():
        pytest.skip("needs the DeepSWE checkout")
    sdk = config.model_copy(
        update={"agent": "anthropic", "model_config_file": examples / "model-anthropic-sdk-haiku.yaml"}
    )
    assert validate(sdk, default_registry())["agent"] == "anthropic"
    unpriced = model_file(tmp_path, "m", {"model_name": "claude-unknown", "model_kwargs": {"max_tokens": 10}})
    with pytest.raises(ValueError, match="no price"):
        validate(config.model_copy(update={"agent": "anthropic", "model_config_file": unpriced}), default_registry())


def test_a_campaign_freezes_the_agent_config_and_keeps_a_refusal_as_an_outcome(tmp_path, fixture_world):
    dataset, environments, _, _ = fixture_world
    config = runtime_tests.mini_swe_config(tmp_path, dataset, [])
    config.agent = "anthropic"
    config.model_config_file = model_file(tmp_path, "sdk", {"model_name": "claude-haiku-5-5", "model_kwargs": KWARGS})
    details = {"type": "refusal", "category": "cyber", "explanation": "CI_EXPLANATION"}
    registry = default_registry()
    refusal = native(stop_reason="refusal", stop_details=details)
    registry.agents["anthropic"] = AnthropicAdapter(client=Transport([SCENARIO[0], refusal]).client())
    root = tmp_path / "campaign"
    run_campaign(config, root, registry, env_factory=environments, identities=runtime_tests.IDENTITIES)
    manifest = runtime_tests.read(root / "manifest.json")
    assert manifest["versions"]["anthropic"] == version("anthropic")
    assert manifest["versions"]["mini-swe-agent"] == version("mini-swe-agent")
    assert yaml.safe_load((root / "mini-swe-config.yaml").read_text())["model"]["model_kwargs"] == KWARGS
    [result] = [runtime_tests.read(path) for path in (root / "clean").glob("*/sessions/*/result.json")]
    assert (result["status"], result["exit_status"], result["stop_details"]) == ("complete", "Refused", details)
    assert (result["steps"], result["cost_status"], result["limit"], result["patch_status"]) == (
        2,
        "reported",
        None,
        "ready",
    )
