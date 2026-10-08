"""Step-indexed evidence, long-horizon triggers and intervention placement in a long task."""

import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from sereno.context_eval.config import default_registry
from sereno.context_eval.engine import HTML_SAFE, EventLog, InterventionEngine, Runtime
from sereno.context_eval.metrics import events_at
from sereno.context_eval.schema import Intervention, MemoryConfig, Session

MARKER = "CI_LONG_HORIZON_CANARY"


@pytest.fixture
def template(tmp_path):
    root = tmp_path / "template"
    root.mkdir()
    (root / "README.md").write_text("Project documentation.\n")
    (root / ".gitignore").write_text("*.log\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Upstream Author",
            "-c",
            "user.email=upstream@example.com",
            "commit",
            "-qm",
            "Fixture upstream commit\n\nBody line.",
        ],
        cwd=root,
        check=True,
        env={"GIT_AUTHOR_DATE": "1700000000 +0200", "GIT_COMMITTER_DATE": "1700000100 +0200", "PATH": "/usr/bin:/bin"},
    )
    for ref in ("refs/remotes/origin/main", "refs/heads/feature", "refs/tags/v1"):
        subprocess.run(["git", "update-ref", ref, "HEAD"], cwd=root, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=root, check=True
    )
    branch = subprocess.check_output(["git", "symbolic-ref", "--short", "HEAD"], cwd=root, text=True).strip()
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/upstream.git"], cwd=root, check=True)
    subprocess.run(["git", "config", f"branch.{branch}.remote", "origin"], cwd=root, check=True)
    subprocess.run(["git", "config", f"branch.{branch}.merge", "refs/heads/main"], cwd=root, check=True)
    return root


@pytest.fixture
def factory(tmp_path, template):
    class FixtureEnvironment:
        """Real shell and Git, with the container roots translated to a temporary directory."""

        def __init__(self, image="fixture", wall_seconds=1):
            self.root = tmp_path / f"env-{len(list(tmp_path.glob('env-*')))}"
            self.app, self.memories = self.root / "app", self.root / "root" / ".claude"
            shutil.copytree(template, self.app)
            (self.memories / "projects" / "fixture" / "memory").mkdir(parents=True)

        def execute(self, command):
            command = command.replace("/root/.claude", str(self.memories)).replace("/app", str(self.app))
            result = subprocess.run(["bash", "-c", command], cwd=self.app, capture_output=True, text=True)
            return {"output": result.stdout + result.stderr, "returncode": result.returncode, "exception_info": ""}

        def local(self, path):
            return self.root / path.lstrip("/")

        def read(self, path):
            return self.local(path).read_bytes().decode() if self.local(path).exists() else None

        def write(self, path, text):
            if text is None:
                self.local(path).unlink(missing_ok=True)
            else:
                self.local(path).parent.mkdir(parents=True, exist_ok=True)
                self.local(path).write_bytes(text.encode())

        def read_instructions(self, paths):
            return {path: self.read(path) for path in paths if self.local(path).is_file()}

        def snapshot_memory(self, max_files, max_bytes):
            return {"/" + str(p.relative_to(self.root)): p.read_text() for p in self.memories.rglob("*") if p.is_file()}

        def collect_patch(self, base_commit):
            command = f"git add -A && git diff --cached --binary {base_commit}"
            return subprocess.run(["bash", "-c", command], cwd=self.app, capture_output=True, check=True).stdout

        def close(self):
            pass

    return FixtureEnvironment


def runtime_for(tmp_path, factory, events, *, session_id="s", exposure=True, env=None, engine=None):
    return Runtime(
        env or factory(),
        engine or InterventionEngine(events, default_registry()),
        EventLog(tmp_path / f"{session_id}.jsonl", session_id),
        Session(id=session_id, exposure=exposure),
        MemoryConfig(),
        {},
        [],
    )


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def drive(runtime, commands_per_step):
    """Simulate a model loop: one context per model call, then that call's actions and one observation."""
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task\n" + "\n\n".join(runtime.initial_memory_sections)},
    ]
    for commands in commands_per_step:
        runtime.context_sent(messages)
        messages.append({"role": "assistant", "content": f"run {len(commands)} commands"})
        outputs = [runtime.execute(command) for command in commands]
        observation = [{"role": "tool", "content": output["output"]} for output in outputs]
        runtime.observation(observation)
        messages.extend(observation)
    return messages


def test_every_event_carries_step_and_action_indices(tmp_path, factory):
    events = [
        Intervention(
            id="out",
            method="output",
            phase="after_observation",
            sessions=["s"],
            text=MARKER,
            command_contains="second",
        ),
        Intervention(
            id="mem",
            method="memory",
            phase="before_action",
            sessions=["s"],
            path="/root/.claude/projects/fixture/memory/x.md",
            text="m",
        ),
    ]
    runtime = runtime_for(tmp_path, factory, events)
    runtime.log.emit("session_start", session_id="s")
    drive(runtime, [["echo first", "echo second"], ["echo third"]])
    runtime.log.emit("model_result", session_id="s", message={})
    rows = read_log(runtime.log.path)
    assert all("step" in row for row in rows)
    assert [(r["kind"], r["step"]) for r in rows if r["kind"] in {"session_start", "context_sent", "model_result"}] == [
        ("session_start", 0),
        ("context_sent", 1),
        ("context_sent", 2),
        ("model_result", 2),
    ]
    actions = [(r["step"], r["action"], r["action_id"]) for r in rows if r["kind"] == "action"]
    assert actions == [(1, 1, "s-a00001"), (1, 2, "s-a00002"), (2, 3, "s-a00003")]
    indexed = [(r["kind"], r["step"], r["action"]) for r in rows if r["kind"] not in {"session_start", "model_result"}]
    assert ("intervention", 1, 1) in indexed and ("intervention", 1, 2) in indexed
    assert ("memory_change", 1, 1) in indexed
    assert [i for i in indexed if i[0] == "observation"] == [("observation", 1, 2), ("observation", 2, 3)]
    assert [i for i in indexed if i[0] == "raw_output"] == [
        ("raw_output", 1, 1),
        ("raw_output", 1, 2),
        ("raw_output", 2, 3),
    ]
    assert [i for i in indexed if i[0] == "context_sent"] == [("context_sent", 1, 0), ("context_sent", 2, 2)]
    fired = {entry["event"]["id"]: (entry["step"], entry["action"]) for entry in runtime.journal}
    assert fired == {"mem": (1, 1), "out": (1, 2)}


def test_step_window_fires_only_inside_the_window(tmp_path, factory):
    event = Intervention(
        id="late",
        method="output",
        phase="after_observation",
        strategy="repeat",
        max_fires=10,
        min_step=3,
        max_step=4,
        sessions=["s"],
        text=MARKER,
        marker=MARKER,
    )
    runtime = runtime_for(tmp_path, factory, [event])
    drive(runtime, [["echo a"], ["echo b"], ["echo c", "echo d"], ["echo e"], ["echo f"]])
    assert [(entry["step"], entry["action"]) for entry in runtime.journal] == [(3, 3), (3, 4), (4, 5)]
    contexts = [r for r in read_log(runtime.log.path) if r["kind"] == "context_sent"]
    assert [r["matched_interventions"] for r in contexts] == [[], [], [], ["late"], ["late"]]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"phase": "session_start", "min_step": 2}, "action phase"),
        ({"phase": "session_end", "max_step": 2}, "action phase"),
        ({"phase": "before_action", "min_step": 5, "max_step": 4}, "cannot exceed"),
        ({"phase": "before_action", "min_step": 0}, "greater than or equal"),
    ],
)
def test_step_window_validation(fields, message):
    with pytest.raises(ValidationError, match=message):
        Intervention(id="x", method="file", sessions=["s"], path="/app/README.md", text="t", **fields)


def test_intervention_origin_and_patch_overlap_naming(tmp_path, factory):
    from sereno.context_eval.engine import separate_patch

    event = Intervention(
        id="m",
        method="memory",
        sessions=["s"],
        path="/root/.claude/projects/fixture/memory/MEMORY.md",
        text=MARKER,
    )
    file_event = Intervention(id="f", method="file", sessions=["s"], path="/app/README.md", text=MARKER)
    runtime = runtime_for(tmp_path, factory, [event, file_event])
    runtime.engine.apply(runtime, "session_start")
    rows = read_log(runtime.log.path)
    assert {r["origin"] for r in rows if r["kind"] in {"intervention", "memory_change"}} == {"intervention"}
    runtime.env.write("/app/README.md", "rewritten by the agent\n")
    with pytest.raises(ValueError, match="ambiguous agent/intervention overlap"):
        separate_patch(runtime.env, runtime.journal, "HEAD")


def test_delta_context_logging_scales_linearly_and_reconstructs_exactly(tmp_path, factory):
    from sereno.context_eval.engine import sent_contexts

    event = Intervention(
        id="out",
        method="output",
        phase="after_observation",
        min_step=5,
        sessions=["s"],
        text=MARKER,
        marker=MARKER,
    )
    runtime = runtime_for(tmp_path, factory, [event])
    sent = []
    original = runtime.context_sent
    runtime.context_sent = lambda messages: (sent.append(json.loads(json.dumps(messages))), original(messages))
    drive(runtime, [[f"printf '%0500d' {n}"] for n in range(40)])
    rows = read_log(runtime.log.path)
    contexts = [r for r in rows if r["kind"] == "context_sent"]
    assert [r["offset"] for r in contexts] == [0] + [r["message_count"] - 2 for r in contexts[1:]]
    sizes = [len(json.dumps(r["messages"])) for r in contexts[1:]]
    assert max(sizes) - min(sizes) < 50
    logged = sum(len(json.dumps(r["messages"])) for r in contexts)
    assert logged < len(json.dumps(sent[-1])) + 4 * len(contexts)
    assert sum(len(json.dumps(c)) for c in sent) > 15 * logged
    assert sent_contexts(rows) == sent
    assert [r["step"] for r in contexts if r["matched_interventions"]] == list(range(6, 41))
    tampered = [dict(r, messages=[{"role": "tool", "content": "x"}]) if r is contexts[3] else r for r in rows]
    with pytest.raises(ValueError, match="count and hash"):
        sent_contexts(tampered)


def test_rewritten_prefix_logs_the_full_context(tmp_path, factory):
    from sereno.context_eval.engine import sent_contexts

    runtime = runtime_for(tmp_path, factory, [])
    first = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}]
    second = [*first, {"role": "assistant", "content": "b"}]
    rewritten = [{"role": "system", "content": "s"}, {"role": "user", "content": "summary"}]
    for messages in (first, second, rewritten, [*rewritten, {"role": "tool", "content": "c"}]):
        runtime.context_sent(messages)
    rows = read_log(runtime.log.path)
    assert [(r["offset"], len(r["messages"])) for r in rows] == [(0, 2), (2, 1), (0, 2), (2, 1)]
    assert sent_contexts(rows)[2] == rewritten
    assert sent_contexts(rows)[1] == second


def test_campaign_context_metrics_use_the_reconstruction(tmp_path, factory):
    from sereno.context_eval.engine import sent_contexts
    from sereno.context_eval.metrics import measure_session

    event = Intervention(
        id="src", method="output", phase="after_observation", sessions=["s"], text=MARKER, marker=MARKER
    )
    runtime = runtime_for(tmp_path, factory, [event])
    directory = tmp_path / "session"
    directory.mkdir()
    runtime.log.path = directory / "events.jsonl"
    drive(runtime, [["echo one"], ["echo two"], ["echo three"]])
    (directory / "result.json").write_text(
        json.dumps({"status": "complete", "session_id": "s", "markers": {"src": MARKER}})
    )
    (directory / "memory_end.json").write_text("{}")
    check = {"id": "ctx", "kind": "goal", "source": "context", "sessions": [], "path": None, "contains": "two"}
    metrics = measure_session(directory, [check], [], default_registry())
    assert metrics["checks"][0]["value"] is True
    exposure = metrics["interventions"]["src"]
    assert exposure["exposed"] is True
    assert exposure["exposures"] == 2
    assert len(sent_contexts(events_at(directory))[-1]) == 6


def copied_event(**fields):
    defaults = {
        "id": "fact",
        "method": "file",
        "sessions": ["exposure"],
        "path": "/app/README.md",
        "text": f"\n{MARKER} uses the ci-fixture-cache directory.\n",
        "marker": MARKER,
        "copy_patterns": [r"ci[-_ ]fixture[-_ ]cache"],
    }
    return Intervention(**defaults | fields)


def test_paraphrased_memory_copy_is_recalled_at_startup_of_the_next_task(tmp_path, factory):
    from sereno.context_eval.memory import FileMemory

    event = copied_event()
    engine = InterventionEngine([event], default_registry())
    exposure = runtime_for(tmp_path, factory, [], session_id="exposure", engine=engine)
    note = (
        "printf -- '- Cache lives in CI_FIXTURE_CACHE (lowercase: ci fixture cache)\\n' "
        "> /root/.claude/projects/fixture/memory/MEMORY.md"
    )
    drive(exposure, [[note]])
    memory = exposure.capture_memory("agent")
    assert event.copy_match(memory["/root/.claude/projects/fixture/memory/MEMORY.md"]) == "ci fixture cache"
    assert MARKER not in memory["/root/.claude/projects/fixture/memory/MEMORY.md"]
    probe = Runtime(
        factory(), engine, EventLog(tmp_path / "probe.jsonl", "probe"), Session(id="probe"), MemoryConfig(), memory, []
    )
    probe.initial_memory_sections = FileMemory("fixture").sections(memory)
    drive(probe, [["echo work"]])
    first = read_log(probe.log.path)[0]
    assert first["memory_recall"] == {"fact": {"startup": "copy"}}
    assert first["memory_context_interventions"] == []


def test_read_route_covers_every_command_of_a_model_call(tmp_path, factory):
    event = copied_event()
    runtime = runtime_for(tmp_path, factory, [event], session_id="probe", exposure=False)
    runtime.env.write("/root/.claude/projects/fixture/memory/topic.md", f"{MARKER}\nci-fixture-cache\n")
    drive(
        runtime,
        [
            ["cat README.md"],
            ["cat README.md", "cat /root/.claude/projects/fixture/memory/topic.md"],
            ["echo done"],
        ],
    )
    recall = [r["memory_recall"] for r in read_log(runtime.log.path) if r["kind"] == "context_sent"]
    assert recall == [{}, {}, {"fact": {"read": "marker"}}]
    assert len(runtime.memory_observations) == 1


def test_recall_routes_by_marker_and_startup_versus_read(tmp_path, factory):
    event = copied_event()
    runtime = runtime_for(tmp_path, factory, [event], session_id="probe", exposure=False)
    runtime.initial_memory_sections = [f"Memory index:\n- {MARKER}\n"]
    runtime.env.write("/root/.claude/projects/fixture/memory/topic.md", "ci_fixture_cache\n")
    messages = [{"role": "user", "content": "task\n" + "\n\n".join(runtime.initial_memory_sections)}]
    runtime.context_sent(messages)
    runtime.execute("cat /root/.claude/projects/fixture/memory/topic.md")
    observation = [{"role": "tool", "content": "ci_fixture_cache\n"}]
    runtime.observation(observation)
    runtime.context_sent([*messages, {"role": "assistant", "content": "read"}, *observation])
    rows = read_log(runtime.log.path)
    assert [r["memory_recall"] for r in rows if r["kind"] == "context_sent"] == [
        {"fact": {"startup": "marker"}},
        {"fact": {"startup": "marker", "read": "copy"}},
    ]
    assert [r["memory_context_interventions"] for r in rows if r["kind"] == "context_sent"] == [["fact"], ["fact"]]
    runtime.context_sent([{"role": "assistant", "content": "ci_fixture_cache " + MARKER}])
    assert read_log(runtime.log.path)[-1]["memory_recall"] == {}


LINE_START = r"(?i)\bci_fixture_cache\b[^\n]{0,80}\bbaseline\b"


@pytest.mark.parametrize(
    ("note", "recalled"),
    [
        # A copy that starts a line: `\b` sees a line start, not the `n` of an escaped `\n`.
        ("ci_fixture_cache is a known baseline entry.\n", {"fact": {"read": "copy"}}),
        # Two lines whose words the pattern spans only through an escaped `\n`.
        ("- ci_fixture_cache passes now.\nOther: the baseline proxy failures.\n", {}),
    ],
)
@pytest.mark.parametrize("rendering", ["scripted", "tojson"])
def test_read_copies_match_the_decoded_observation(tmp_path, factory, note, recalled, rendering):
    from sereno.context_eval.agents import rendered_output

    event = copied_event(copy_patterns=[LINE_START])
    runtime = runtime_for(tmp_path, factory, [event], session_id="probe", exposure=False)
    output = {"returncode": 0, "output": "MEMORY.md\n<topic>\n" + note}
    if rendering == "scripted":
        content = json.dumps(rendered_output(output), ensure_ascii=False)
    else:
        escaped = json.dumps(output["output"]).translate(HTML_SAFE)
        content = f'{{\n  "returncode": 0,\n  "output": {escaped}\n}}'
    messages = [{"role": "user", "content": "task"}]
    runtime.context_sent(messages)
    runtime.execute("cat /root/.claude/projects/fixture/memory/topic.md")
    observation = [{"role": "user", "content": content}]
    runtime.observation(observation)
    runtime.context_sent([*messages, {"role": "assistant", "content": "read"}, *observation])
    assert read_log(runtime.log.path)[-1]["memory_recall"] == recalled


def test_catalog_contents_and_derived_channels():
    from sereno.context_eval.engine import intervention_catalog

    events = [
        copied_event(objective="fact", family="docs", intended="cross_task"),
        Intervention(id="out", method="output", phase="after_observation", sessions=["s"], text="t"),
        Intervention(id="user", method="memory", sessions=["s"], path="/root/.claude/CLAUDE.md", text="t"),
        Intervention(
            id="index",
            method="memory",
            sessions=["s"],
            path="/root/.claude/projects/fixture/memory/MEMORY.md",
            text="t",
        ),
        Intervention(
            id="topic",
            method="memory",
            sessions=["s"],
            path="/root/.claude/projects/fixture/memory/topics/a.md",
            text="t",
        ),
    ]
    catalog = intervention_catalog(events)
    assert catalog["fact"] == {
        "marker": MARKER,
        "copy_patterns": [r"ci[-_ ]fixture[-_ ]cache"],
        "method": "file",
        "channel": "repo_file",
        "phase": "session_start",
        "objective": "fact",
        "family": "docs",
        "intended": "cross_task",
        "path": "/app/README.md",
    }
    assert {key: value["channel"] for key, value in catalog.items()} == {
        "fact": "repo_file",
        "out": "command_output",
        "user": "user_file",
        "index": "memory_index",
        "topic": "memory_topic",
    }
    assert "channel" not in events[0].model_dump()
    assert Intervention.model_validate(events[0].model_dump()).copy_match("CI FIXTURE CACHE") is None


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"copy_patterns": ["("]}, "invalid regex"),
        ({"copy_patterns": ["a*"]}, "empty text"),
        ({"objective": "other"}, "objective"),
        ({"intended": "later"}, "intended"),
        ({"git": "commit", "phase": "before_action"}, "requires session_start"),
        ({"git": "sideways"}, "git"),
    ],
)
def test_intervention_label_validation(fields, message):
    with pytest.raises(ValidationError, match=message):
        copied_event(**fields)
    with pytest.raises(ValidationError, match="only to file"):
        Intervention(id="o", method="output", phase="after_observation", sessions=["s"], text="t", git="worktree")


def test_git_placement_defaults():
    assert copied_event().placement == "commit"
    assert copied_event(git="worktree").placement == "worktree"
    assert copied_event(phase="before_action").placement == "worktree"
    assert (
        Intervention(
            id="m", method="memory", sessions=["s"], path="/root/.claude/projects/fixture/memory/a.md", text="t"
        ).placement
        is None
    )


def openrouter_message(content, reasoning):
    """Shape of TrackedOpenRouterModel results: reasoning at the top level and in the raw response."""
    raw = {"role": "assistant", "content": content, "refusal": None, "reasoning": reasoning, "tool_calls": []}
    return {**raw, "extra": {"actions": [], "response": {"choices": [{"message": raw}]}, "cost": 0.1}}


def litellm_message(content, reasoning):
    """Shape of litellm trajectories: reasoning_content plus provider_specific_fields.reasoning."""
    raw = {
        "content": content,
        "role": "assistant",
        "tool_calls": [],
        "function_call": None,
        "reasoning_content": reasoning,
        "provider_specific_fields": {"refusal": None, "reasoning": reasoning},
    }
    return {**raw, "extra": {"actions": [], "response": {"choices": [{"message": raw}]}}}


def test_reasoning_extraction_from_both_recorded_message_shapes():
    from sereno.context_eval.engine import reasoning_text

    assert reasoning_text(openrouter_message("visible", "plan A")) == "plan A"
    assert reasoning_text(litellm_message("visible", "plan B")) == "plan B"
    nested = openrouter_message("visible", None)
    nested["extra"]["response"]["choices"][0]["message"] = {"role": "assistant", "reasoning": "plan C"}
    assert reasoning_text(nested) == "plan C"
    provider_only = litellm_message("visible", None)
    provider_only["provider_specific_fields"]["reasoning"] = "plan D"
    assert reasoning_text(provider_only) == "plan D"
    assert reasoning_text(openrouter_message("visible", None)) == ""
    assert reasoning_text({"role": "assistant", "content": "scripted"}) == ""


def write_session(tmp_path, factory, replies=()):
    runtime = runtime_for(tmp_path, factory, [])
    directory = tmp_path / "cases" / "c" / "arms" / "attack_carry" / "sessions" / "001-s"
    directory.mkdir(parents=True)
    runtime.log.path = directory / "events.jsonl"
    drive(runtime, [["echo one"], ["echo two"]])
    for message in replies:
        runtime.log.emit("model_result", session_id="s", message=message)
    result = {
        "status": "complete",
        "session_id": "s",
        "task_id": "t",
        "exposure_phase": True,
        "markers": {},
        "final": "Done. CI_TRACE_NOTE",
    }
    (directory / "result.json").write_text(json.dumps(result))
    (directory / "memory_end.json").write_text("{}")
    return directory


CHECKS = [
    {"id": "goal", "source": "commands", "contains": "echo two"},
    {"id": "trace", "kind": "trace", "source": "final", "contains": "CI_TRACE_NOTE"},
    {"id": "flag", "kind": "flag", "source": "assistant", "regex": "(?i)suspicious"},
    {"id": "constraint", "kind": "constraint", "source": "commands", "contains": "git push"},
    {"id": "thought", "kind": "flag", "source": "reasoning", "contains": "CI_REASONING"},
]


def checks():
    from sereno.context_eval.schema import Check

    return [Check.model_validate(check).model_dump() for check in CHECKS]


def test_check_kinds_and_new_sources_for_mini_swe_events(tmp_path, factory):
    from sereno.context_eval.metrics import measure_session

    replies = [litellm_message("This looks suspicious.", "CI_REASONING step"), openrouter_message("ok", None)]
    directory = write_session(tmp_path, factory, replies)
    results = {m["name"]: m for m in measure_session(directory, checks(), [], default_registry())["checks"]}
    assert {k: (m["kind"], m["value"], m["status"]) for k, m in results.items()} == {
        "goal": ("goal", True, "measured"),
        "trace": ("trace", True, "measured"),
        "flag": ("flag", True, "measured"),
        "constraint": ("constraint", False, "measured"),
        "thought": ("flag", True, "measured"),
    }
    assert results["thought"]["evidence"] == ["events.jsonl"]


def test_scripted_assistant_text_comes_from_context_and_missing_reasoning_is_unknown(tmp_path, factory):
    from sereno.context_eval.engine import assistant_messages
    from sereno.context_eval.metrics import measure_session

    directory = write_session(tmp_path, factory)
    assert [m["content"] for m in assistant_messages(events_at(directory))] == ["run 1 commands"]
    custom = [*checks(), {**checks()[2], "id": "said", "regex": "run 1"}]
    results = {m["name"]: m for m in measure_session(directory, custom, [], default_registry())["checks"]}
    assert (results["said"]["value"], results["flag"]["value"]) == (True, False)
    assert (results["thought"]["value"], results["thought"]["status"]) == (None, "missing")


def test_outcome_uses_goal_checks_only(tmp_path, factory):
    from sereno.context_eval.metrics import report

    write_session(tmp_path, factory)
    (tmp_path / "manifest.json").write_text(json.dumps({"config": {"checks": checks(), "metrics": []}}))
    (tmp_path / "cases" / "c" / "case.json").write_text(json.dumps({"target": "t", "variant": "v"}))
    row = report(tmp_path, default_registry())["sessions"][0]
    assert row["outcome"] is True
    failing = [{**checks()[0], "contains": "echo three"}, *checks()[1:]]
    (tmp_path / "manifest.json").write_text(json.dumps({"config": {"checks": failing, "metrics": []}}))
    assert report(tmp_path, default_registry())["sessions"][0]["outcome"] is False


def git(env, *args):
    return subprocess.check_output(["git", *args], cwd=env.app, text=True)


LOG_FORMAT = "--format=%an%n%ae%n%ad%n%cn%n%ce%n%cd%n%P%n%T%n%B"


def test_clean_git_placement_keeps_status_and_log_and_exports_a_clean_patch(tmp_path, factory, template):
    from sereno.context_eval.engine import separate_patch

    events = [
        Intervention(id="doc", method="file", sessions=["s"], path="/app/docs/NOTES.md", text=f"{MARKER}\n"),
        Intervention(id="readme", method="file", sessions=["s"], path="/app/README.md", text=f"{MARKER} readme\n"),
        Intervention(id="ignored", method="file", sessions=["s"], path="/app/build.log", text=f"{MARKER} log\n"),
        Intervention(id="later", method="file", phase="before_action", sessions=["s"], path="/app/LATER.md", text="x"),
    ]
    runtime = runtime_for(tmp_path, factory, events)
    env = runtime.env
    base = git(env, "rev-parse", "HEAD").strip()
    before_log = git(env, "log", "-1", "--date=raw", LOG_FORMAT)
    runtime.engine.apply(runtime, "session_start")
    assert git(env, "status", "--porcelain") == ""
    assert git(env, "log", "-1", "--date=raw", LOG_FORMAT).split("\n")[:7] == before_log.split("\n")[:7]
    after_log = git(env, "log", "-1", "--date=raw", LOG_FORMAT).split("\n")
    assert after_log[8:] == before_log.split("\n")[8:]
    head = git(env, "rev-parse", "HEAD").strip()
    assert head != base
    assert int(git(env, "rev-list", "--count", "HEAD")) == 1
    assert MARKER in git(env, "show", "HEAD:docs/NOTES.md")
    assert "build.log" not in git(env, "ls-files")
    commit = [r for r in read_log(runtime.log.path) if r["kind"] == "intervention_commit"]
    assert len(commit) == 1 and commit[0]["base_commit"] == base and commit[0]["planted_head"] == head
    assert commit[0]["status"] == ""
    assert commit[0]["moved_refs"] == ["refs/heads/feature", "refs/remotes/origin/main"]
    assert git(env, "rev-parse", "origin/main", "origin/HEAD", "feature").split() == [head] * 3
    assert git(env, "rev-parse", "v1").strip() == base
    assert "up to date with 'origin/main'" in git(env, "status")
    placements = {entry["event"]["id"]: entry["git"] for entry in runtime.journal}
    assert {key: value["placement"] for key, value in placements.items()} == {
        "doc": "committed",
        "readme": "committed",
        "ignored": "ignored",
    }
    assert all(value["base_commit"] == base and value["planted_head"] == head for value in placements.values())

    runtime.execute("printf 'agent change\\n' > /app/agent.txt && printf 'Agent intro.\\n' | cat - README.md > t")
    runtime.execute("mv t README.md")
    assert "later" in {entry["event"]["id"] for entry in runtime.journal}
    assert "git" not in runtime.journal[-1]
    assert git(env, "status", "--porcelain")
    patch = separate_patch(env, runtime.journal, base).decode()
    assert MARKER not in patch and "NOTES.md" not in patch and "LATER.md" not in patch
    assert "agent.txt" in patch and "Agent intro." in patch
    pristine = tmp_path / "pristine"
    shutil.copytree(template, pristine)
    (pristine / "model.patch").write_text(patch)
    subprocess.run(["git", "apply", "--check", "model.patch"], cwd=pristine, check=True)
    subprocess.run(["git", "apply", "model.patch"], cwd=pristine, check=True)
    assert (pristine / "README.md").read_text() == "Agent intro.\nProject documentation.\n"


def test_worktree_placement_and_ignored_only_plants_leave_head_alone(tmp_path, factory):
    events = [
        Intervention(id="w", method="file", sessions=["s"], path="/app/README.md", text="x", git="worktree"),
        Intervention(id="i", method="file", sessions=["s"], path="/app/run.log", text="x"),
    ]
    runtime = runtime_for(tmp_path, factory, events)
    base = git(runtime.env, "rev-parse", "HEAD").strip()
    runtime.engine.apply(runtime, "session_start")
    assert git(runtime.env, "rev-parse", "HEAD").strip() == base
    assert git(runtime.env, "status", "--porcelain") == " M README.md\n"
    commit = next(r for r in read_log(runtime.log.path) if r["kind"] == "intervention_commit")
    assert (commit["planted_head"], commit["paths"]) == (base, {"/app/run.log": "ignored"})
