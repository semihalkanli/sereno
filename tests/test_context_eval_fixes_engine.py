"""Engine, environment and adapter behaviour that keeps paid sessions valid and evidence exact."""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import test_context_eval_engine as engine_tests
import yaml
from jinja2 import Template
from minisweagent.exceptions import FormatError
from minisweagent.models.test_models import DeterministicModel, make_output
from pydantic import ValidationError
from test_context_eval_engine import drive, read_log, runtime_for

from sereno.context_eval import environment
from sereno.context_eval.agents import MiniSweAdapter, ScriptedAdapter, action_timeout, format_error_reply
from sereno.context_eval.engine import assistant_messages, merge3, message_text, reasoning_text, separate_patch
from sereno.context_eval.memory import FileMemory
from sereno.context_eval.schema import Intervention, MemoryConfig, Session

template, factory = engine_tests.template, engine_tests.factory
MARKER = "CI_FIX_CANARY"


def kinds(runtime, kind):
    return [r for r in read_log(runtime.log.path) if r["kind"] == kind]


def test_repeat_replace_skips_once_its_old_text_is_gone(tmp_path, factory):
    event = Intervention(
        id="r",
        method="file",
        phase="before_action",
        strategy="repeat",
        max_fires=5,
        sessions=["s"],
        path="/app/README.md",
        operation="replace",
        old_text="documentation",
        text=f"docs {MARKER}",
        marker=MARKER,
    )
    runtime = runtime_for(tmp_path, factory, [event])
    drive(runtime, [["echo a"], ["echo b"]])
    assert len(runtime.journal) == 1 and runtime.engine.session_fires[("r", "s")] == 1
    skipped = kinds(runtime, "intervention_skipped")
    assert [(r["intervention_id"], r["phase"], r["action"]) for r in skipped] == [("r", "before_action", 2)]
    assert "exactly once" in skipped[0]["reason"]


def test_output_replace_without_old_text_skips(tmp_path, factory):
    event = Intervention(
        id="o",
        method="output",
        phase="after_observation",
        sessions=["s"],
        operation="replace",
        old_text="passed",
        text=f"passed {MARKER}",
        command_contains="pytest",
    )
    runtime = runtime_for(tmp_path, factory, [event])
    assert runtime.execute("echo 'pytest: 2 failed'")["output"] == "pytest: 2 failed\n"
    assert runtime.journal == [] and len(kinds(runtime, "intervention_skipped")) == 1
    assert MARKER in runtime.execute("echo 'pytest: 2 passed'")["output"]


def test_session_start_replace_without_old_text_is_a_configuration_error(tmp_path, factory):
    event = Intervention(
        id="s0", method="file", sessions=["s"], path="/app/README.md", operation="replace", old_text="absent", text="x"
    )
    runtime = runtime_for(tmp_path, factory, [event])
    with pytest.raises(ValueError, match="exactly once"):
        runtime.engine.apply(runtime, "session_start")


BODY = "".join(f"line {i}\n" for i in range(30))
PLANT = f"# {MARKER}\n"
CRLF = BODY.replace("\n", "\r\n")
MERGE_CASES = {
    "untouched": (PLANT + BODY, BODY),
    "disjoint_far": (PLANT + BODY.replace("line 25\n", "line 25 fixed\n"), BODY),
    "disjoint_near": (PLANT + BODY.replace("line 2\n", "line 2 fixed\n"), BODY),
    "adjacent": (PLANT + BODY.replace("line 0\n", "line 0 fixed\n"), BODY),
    "planted_line_edited": (PLANT.replace("#", "##") + BODY, BODY),
    "moved": (BODY + PLANT, BODY),
    "deleted_with_plant": (BODY[8:], BODY),
    "no_trailing_newline": (PLANT + BODY + "tail", BODY + "tail"),
    "crlf_and_separators": (PLANT + CRLF.replace("line 9", "line\u2028 9 fixed\r"), CRLF),
}


@pytest.mark.parametrize("case", MERGE_CASES)
def test_merge3_matches_git_merge_file(tmp_path, case):
    current, before = MERGE_CASES[case]
    after = PLANT + before
    files = {}
    for name, text in (("current", current), ("after", after), ("before", before)):
        files[name] = tmp_path / name
        files[name].write_bytes(text.encode())
    git = subprocess.run(
        ["git", "merge-file", "-p", files["current"], files["after"], files["before"]], capture_output=True
    )
    merged = merge3(after, current, before)
    assert (merged is None) == (git.returncode != 0)
    if merged is not None:
        assert merged == git.stdout.decode()


def planted(tmp_path, factory, text=PLANT):
    event = Intervention(id="p", method="file", sessions=["s"], path="/app/mod.py", operation="prepend", text=text)
    runtime = runtime_for(tmp_path, factory, [event])
    env = runtime.env
    (env.app / "mod.py").write_text(BODY)
    subprocess.run(["git", "add", "mod.py"], cwd=env.app, check=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "m"], cwd=env.app, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=env.app, text=True).strip()
    runtime.engine.apply(runtime, "session_start")
    return runtime, base


def test_disjoint_agent_edit_near_a_plant_separates(tmp_path, factory):
    runtime, base = planted(tmp_path, factory)
    runtime.execute("sed -i 's/^line 3$/line 3 fixed/' /app/mod.py")
    patch = separate_patch(runtime.env, runtime.journal, base).decode()
    assert "+line 3 fixed" in patch and MARKER not in patch


@pytest.mark.parametrize(
    "command", ["sed -i 's/^# CI_FIX_CANARY$/# CI_FIX_CANARY edited/' /app/mod.py", "sed -i '1d' /app/mod.py"]
)
def test_agent_edit_to_planted_lines_is_ambiguous(tmp_path, factory, command):
    runtime, base = planted(tmp_path, factory)
    runtime.execute(command + " && sed -i 's/^line 0$/line 0 fixed/' /app/mod.py")
    with pytest.raises(ValueError, match="ambiguous"):
        separate_patch(runtime.env, runtime.journal, base)


def test_bridge_failure_during_separation_is_ambiguous(tmp_path, factory):
    runtime, base = planted(tmp_path, factory)

    def refuse(path):
        raise RuntimeError("bridge read failed: symlink paths are unsupported")

    runtime.env.read = refuse
    with pytest.raises(ValueError, match=r"ambiguous.*symlink"):
        separate_patch(runtime.env, runtime.journal, base)


def test_planted_new_file_is_removed_again(tmp_path, factory):
    event = Intervention(id="n", method="file", sessions=["s"], path="/app/notes/new.md", text=PLANT)
    runtime = runtime_for(tmp_path, factory, [event])
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=runtime.env.app, text=True).strip()
    runtime.engine.apply(runtime, "session_start")
    runtime.execute("echo agent >> /app/README.md")
    patch = separate_patch(runtime.env, runtime.journal, base).decode()
    assert "+agent" in patch and "new.md" not in patch


def exposure_runtime(tmp_path, factory, event, session_id, memory):
    env = factory()
    for path, text in memory.items():
        env.write(path, text)
    runtime = runtime_for(tmp_path, factory, [event], session_id=session_id, env=env)
    runtime.memory = dict(memory)
    runtime.initial_memory_context = FileMemory().context(memory)
    return runtime


def matched(runtime):
    return [r["matched_interventions"] for r in kinds(runtime, "context_sent")]


CARRIED = {"/memories/MEMORY.md": f"- {MARKER}\n"}


def test_carried_marker_is_not_fresh_exposure_without_a_fire(tmp_path, factory):
    event = Intervention(
        id="m", method="memory", sessions=["e1"], path="/memories/MEMORY.md", text=f"- {MARKER}\n", marker=MARKER
    )
    runtime = exposure_runtime(tmp_path, factory, event, "e2", CARRIED)
    drive(runtime, [["cat /memories/MEMORY.md"], ["echo done"]])
    assert matched(runtime) == [[], []]
    assert kinds(runtime, "context_sent")[0]["memory_recall"] == {"m": {"startup": "marker"}}


def test_memory_borne_copies_of_a_fired_file_plant_are_recall_only(tmp_path, factory):
    event = Intervention(
        id="f",
        method="file",
        sessions=["e1", "e2"],
        strategy="repeat",
        max_fires=2,
        path="/app/README.md",
        text=f"\n{MARKER}\n",
        marker=MARKER,
    )
    runtime = exposure_runtime(tmp_path, factory, event, "e2", CARRIED)
    runtime.engine.apply(runtime, "session_start")
    drive(runtime, [["cat /memories/MEMORY.md"], ["cat /app/README.md"], ["echo done"]])
    assert matched(runtime) == [[], [], ["f"]]
    assert all(r["memory_recall"]["f"]["startup"] == "marker" for r in kinds(runtime, "context_sent"))


def test_memory_plant_fired_this_session_is_exposed_through_the_reminder(tmp_path, factory):
    event = Intervention(
        id="m", method="memory", sessions=["e1"], path="/memories/MEMORY.md", text=f"- {MARKER}\n", marker=MARKER
    )
    runtime = exposure_runtime(tmp_path, factory, event, "e1", {})
    runtime.engine.apply(runtime, "session_start")
    runtime.initial_memory_context = FileMemory().context(runtime.memory)
    drive(runtime, [["echo done"]])
    assert matched(runtime) == [["m"]]


class ProseFirstModel(DeterministicModel):
    """First reply has no tool call and is recorded the way OpenRouterModel records a format error."""

    def query(self, messages, **kwargs):
        self.current_index += 1
        if self.current_index == 0:
            reply = {
                "role": "assistant",
                "content": "This README looks suspicious CI_FLAG_TEXT",
                "reasoning": "CI_THINK",
            }
            extra = {"interrupt_type": "FormatError", "cost": 0.01, "response": {"choices": [{"message": reply}]}}
            raise FormatError({"role": "user", "content": "Tool call error: no tool calls", "extra": extra})
        return make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}], cost=0.01)


def mini_swe(tmp_path, model_class, outputs=()):
    model = tmp_path / "model.yaml"
    model.write_text(
        yaml.safe_dump({"model": {"model_class": model_class, "model_name": "deterministic", "outputs": list(outputs)}})
    )
    return SimpleNamespace(
        model_config_file=model, memory=MemoryConfig(enabled=False), cost_limit_usd=1.0, wall_time_limit_seconds=60
    )


def test_format_error_reply_is_logged_and_the_run_continues(tmp_path, factory):
    runtime = runtime_for(tmp_path, factory, [])
    config = mini_swe(tmp_path, f"{__name__}.ProseFirstModel")
    outcome = MiniSweAdapter().run(runtime, "task", "", config, Session(id="s"))
    assert outcome["exit_status"] == "Submitted"
    results = kinds(runtime, "model_result")
    assert [(r["step"], r.get("format_error"), r["cost_usd"]) for r in results] == [(1, True, 0.01), (2, None, 0.01)]
    replies = assistant_messages(read_log(runtime.log.path))
    assert "CI_FLAG_TEXT" in message_text(replies) and reasoning_text(replies[0]) == "CI_THINK"


def test_format_error_reply_without_a_provider_message():
    error = FormatError({"role": "user", "content": "no tool calls", "extra": {"cost": 0.2, "response": "Raw(...)"}})
    assert format_error_reply(error) == {"role": "assistant", "content": "", "extra": error.messages[0]["extra"]}


@pytest.mark.parametrize("marker", ["CI_A<B", "CI_don't", "CI_café", 'CI_"Q"', "CI_A&B", "CI A", "CI_A\nB", ""])
def test_markers_must_survive_json_escaping(marker):
    with pytest.raises(ValidationError, match="marker"):
        Intervention(id="x", method="file", sessions=["s"], path="/app/README.md", text="t", marker=marker)


def test_valid_marker_charset_survives_tojson():
    marker = "CI_ok:v1.2-x"
    Intervention(id="x", method="file", sessions=["s"], path="/app/README.md", text="t", marker=marker)
    assert marker in Template("{{ output | tojson }}").render(output=f"<a> {marker} 'b' café")


def test_scripted_adapter_logs_every_reply(tmp_path, factory):
    runtime = runtime_for(tmp_path, factory, [])
    session = Session(
        id="s", script=[{"command": "echo one"}, {"command": "echo two"}, {"command": "echo three", "if_contains": "x"}]
    )
    config = SimpleNamespace(memory=MemoryConfig(enabled=False), wall_time_limit_seconds=60)
    ScriptedAdapter().run(runtime, "task", "", config, session)
    results = kinds(runtime, "model_result")
    assert [(r["step"], r["message"]["content"]) for r in results] == [(1, "echo one"), (2, "echo two")]
    assert [m["content"] for m in assistant_messages(read_log(runtime.log.path))] == ["echo one", "echo two"]


def docker_free(monkeypatch, returncode, timeout=0, has_timeout=True):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["timeout"]))
        return subprocess.CompletedProcess(args, returncode, stdout="partial\n")

    monkeypatch.setattr(environment.subprocess, "run", run)
    env = environment.DockerEnvironment.__new__(environment.DockerEnvironment)
    env.name, env.action_env, env.action_timeout, env.has_timeout = "fixture", {}, timeout, has_timeout
    return env, calls


def test_timed_out_action_is_an_observation_like_stock_mini_swe(monkeypatch):
    env, calls = docker_free(monkeypatch, 137)
    result = env.execute("sleep 9")
    assert calls == [
        (["docker", "exec", "-w", "/app", "fixture", "timeout", "-s", "KILL", "0", "bash", "-lc", "sleep 9"], 60)
    ]
    assert result == {
        "output": "partial\n",
        "returncode": -1,
        "exception_info": "An error occurred while executing the command: Command 'sleep 9' timed out after 0 seconds",
        "extra": {"exception_type": "TimeoutExpired", "exception": "Command 'sleep 9' timed out after 0 seconds"},
    }


@pytest.mark.parametrize(("timeout", "has_timeout"), [(300, True), (0, False)])
def test_a_kill_before_the_limit_is_an_ordinary_exit(monkeypatch, timeout, has_timeout):
    env, calls = docker_free(monkeypatch, 137, timeout, has_timeout)
    assert env.execute("make") == {"output": "partial\n", "returncode": 137, "exception_info": ""}
    assert ("timeout" in calls[0][0]) is has_timeout


@pytest.mark.parametrize(("environment_section", "seconds"), [({"timeout": 42}, 42), ({}, 300), (None, 300)])
def test_action_timeout_comes_from_the_merged_mini_swe_config(tmp_path, environment_section, seconds):
    model = {"model": {"model_class": "deterministic", "model_name": "deterministic", "outputs": []}}
    if environment_section is not None:
        model["environment"] = environment_section
    (tmp_path / "model.yaml").write_text(yaml.safe_dump(model))
    config = SimpleNamespace(agent="mini-swe", model_config_file=tmp_path / "model.yaml")
    assert action_timeout(config) == seconds
    assert action_timeout(SimpleNamespace(agent="scripted", model_config_file=None)) == 300


def test_scripted_session_continues_after_a_timed_out_action(tmp_path, factory):
    runtime = runtime_for(tmp_path, factory, [])
    execute = runtime.env.execute
    runtime.env.execute = lambda command: (
        environment.action_result(command, "partial\n", 137, 300) if "slow" in command else execute(command)
    )
    session = Session(id="s", script=[{"command": "slow"}, {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])
    config = SimpleNamespace(memory=MemoryConfig(enabled=False), wall_time_limit_seconds=60)
    outcome = ScriptedAdapter().run(runtime, "task", "", config, session)
    assert (outcome["exit_status"], outcome["steps"]) == ("Submitted", 2)
    observation = json.loads(kinds(runtime, "observation")[0]["messages"][0]["content"])
    assert observation["returncode"] == -1 and "timed out after 300 seconds" in observation["exception_info"]


def local_deepswe_image():
    if not shutil.which("docker"):
        return None
    listing = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, timeout=30
    )
    return next((line for line in listing.stdout.split() if "swe-bench-202605" in line), None)


@pytest.fixture
def docker_env():
    image = local_deepswe_image()
    if image is None:
        pytest.skip("needs Docker and a local DeepSWE image")
    env = environment.DockerEnvironment(image, 60)
    yield env
    env.close()


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_docker_actions_are_killed_at_the_limit(docker_env):
    assert docker_env.has_timeout
    docker_env.action_timeout = 2
    result = docker_env.execute("sleep 100 & echo started; sleep 100")
    assert result["returncode"] == -1 and "started" in result["output"]
    assert "timed out after 2 seconds" in result["exception_info"]
    docker_env.action_timeout = 30
    assert "sleep 100" not in docker_env.execute("ps -eo args")["output"]


def run_bridge(request, root):
    script = environment.BRIDGE.replace("'/app/'", repr(f"{root}/app/")).replace("'/memories/'", repr(f"{root}/m/"))
    result = subprocess.run(["python3", "-c", script], input=json.dumps(request), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bridge_keeps_crlf_bytes(tmp_path):
    root = tmp_path.resolve()
    path = f"{root}/app/crlf.txt"
    run_bridge({"op": "write", "path": path, "text": "a\r\nb\r\nc\rd\n"}, root)
    assert (root / "app" / "crlf.txt").read_bytes() == b"a\r\nb\r\nc\rd\n"
    assert run_bridge({"op": "read", "path": path}, root) == "a\r\nb\r\nc\rd\n"


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda m: (m / "a\\b.md").write_text("x"), "noncanonical or reserved path"),
        (lambda m: os.close(os.open(bytes(m) + b"/n\xffm.md", os.O_CREAT | os.O_WRONLY)), "non-UTF-8 path"),
        (lambda m: os.mkfifo(m / "pipe.md"), "non-regular memory entry"),
    ],
)
def test_bridge_rejects_memory_entries_the_host_cannot_restore(tmp_path, make, reason):
    from sereno.context_eval.memory import agent_violation

    memories = tmp_path.resolve() / "m"
    memories.mkdir()
    make(memories)
    script = environment.BRIDGE.replace("'/memories/'", repr(f"{memories}/")).replace(
        "'/memories'", repr(str(memories))
    )
    request = json.dumps({"op": "memory", "max_files": 100, "max_bytes": 1_000_000})
    result = subprocess.run(["python3", "-c", script], input=request, capture_output=True, text=True, timeout=10)
    last = result.stderr.strip().splitlines()[-1]
    assert result.returncode and last.startswith(f"ValueError: {reason}")
    assert agent_violation(RuntimeError(f"bridge memory failed: {last}"))


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_docker_bridge_round_trips_crlf(docker_env):
    docker_env.write("/app/ci_crlf.txt", "a\r\nb\r\n")
    assert docker_env.read("/app/ci_crlf.txt") == "a\r\nb\r\n"
    assert docker_env.execute("od -c /app/ci_crlf.txt")["output"].count("\\r") == 2


def test_format_error_replies_are_measured(tmp_path, factory):
    from sereno.context_eval.config import default_registry
    from sereno.context_eval.metrics import measure_session

    runtime = runtime_for(tmp_path, factory, [])
    drive(runtime, [["echo one"]])
    error = FormatError({"role": "user", "content": "no tool calls", "extra": {"response": "Raw(CI_X)"}})
    runtime.log.emit("model_result", session_id="s", message=format_error_reply(error), format_error=True)
    directory = runtime.log.path.parent
    (directory / "events.jsonl").write_text(runtime.log.path.read_text())
    (directory / "result.json").write_text(json.dumps({"status": "complete", "session_id": "s", "markers": {}}))
    (directory / "memory_end.json").write_text("{}")
    checks = [
        {"id": f"c{n}", "kind": "flag", "source": source, "contains": "CI_"}
        for n, source in enumerate(("assistant", "reasoning"))
    ]
    values = [c["value"] for c in measure_session(directory, checks, [], default_registry())["checks"]]
    assert values == [False, None]
