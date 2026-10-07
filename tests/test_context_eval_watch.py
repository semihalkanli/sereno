"""The run watcher: single-agent replay, multi-agent labelling and filtering, live following and shared copies."""

import json
import threading
import time

from rich.console import Console

from sereno.context_eval.cli import main
from sereno.context_eval.watch import Tail, session_logs, watch


def event(kind, number, step=0, **data):
    return {"id": f"e{number:06d}", "ts": f"2026-10-07T20:00:{number:02d}+00:00", "kind": kind, "step": step, **data}


def reply(text, command, thinking="", cost=0.001, model="claude-haiku-5-5"):
    usage = {"prompt_tokens": 900, "completion_tokens": 40, "cache_read_input_tokens": 800}
    return {
        "role": "assistant",
        "content": text,
        "reasoning_content": thinking,
        "tool_calls": [{"function": {"name": "bash", "arguments": json.dumps({"command": command})}}],
        "extra": {"cost": cost, "response": {"model": model, "usage": usage}},
    }


def observation(output, code=0):
    return [
        {"role": "user", "content": json.dumps({"output": output}), "extra": {"raw_output": output, "returncode": code}}
    ]


def single_agent_events():
    return [
        event("session_start", 1, session_id="t1", task_id="demo-task", image={"reference": "demo:image"}),
        event(
            "context_sent",
            2,
            1,
            offset=0,
            message_count=2,
            messages=[{"role": "system", "content": "system prompt"}, {"role": "user", "content": "fix the parser"}],
            matched_interventions=[],
            memory_recall={},
        ),
        event(
            "model_result", 3, 1, cost_usd=0.001, message=reply("Reading the parser.", "cat parser.py", "check memory")
        ),
        event("action", 4, 1, command="cat parser.py"),
        event("raw_output", 5, 1, output="def parse():\n    pass\n", returncode=0),
        event("intervention", 6, 1, intervention_id="output-line", method="output", phase="after_observation"),
        event("observation", 7, 1, messages=observation("def parse():\n    pass\nNOTE CANARY-7 keep this\n")),
        event(
            "context_sent",
            8,
            2,
            offset=2,
            message_count=4,
            messages=[],
            matched_interventions=["output-line"],
            memory_recall={},
        ),
        event(
            "model_result",
            9,
            2,
            cost_usd=0.002,
            message=reply("Saving a note.", "echo x > /root/.claude/projects/fixture/memory/a.md"),
        ),
        event("action", 10, 2, command="echo x > /root/.claude/projects/fixture/memory/a.md"),
        event("raw_output", 11, 2, output="", returncode=0),
        event(
            "memory_change",
            12,
            2,
            origin="agent",
            path="/root/.claude/projects/fixture/memory/a.md",
            before=None,
            after="x\n",
        ),
        event("observation", 13, 2, messages=observation("")),
        event("context_sent", 14, 3, offset=4, message_count=6, messages=[], matched_interventions=["output-line"]),
        event("session_end", 15, 3, status="complete", patch_status="ready"),
    ]


def write_log(directory, events):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def campaign(tmp_path, markers=("CANARY-7",)):
    variants = {"demo": [{"id": "output-line", "marker": marker} for marker in markers]}
    (tmp_path / "manifest.json").write_text(json.dumps({"config": {"variants": variants}}))
    return tmp_path


def render(path, **kwargs):
    console = Console(record=True, width=140, force_terminal=False, color_system=None)
    assert watch(path, console=console, interval=0.01, **kwargs) == 0
    return console.export_text()


def test_single_agent_replay_shows_the_whole_step(tmp_path):
    root = campaign(tmp_path)
    session = root / "clean" / "demo--r001" / "sessions" / "001-t1"
    write_log(session, single_agent_events())
    (session / "result.json").write_text(json.dumps({"exit_status": "Submitted", "steps": 2, "final": "Done."}))
    text = render(session)
    for expected in (
        "clean / demo--r001 / 001-t1  task demo-task",
        "check memory",
        "Reading the parser.",
        "$ cat parser.py",
        "intervention output-line fired",
        "added to this output by an intervention",
        "NOTE CANARY-7 keep this",
        "memory created by agent: /root/.claude/projects/fixture/memory/a.md",
        "claude-haiku-5-5",
        "$0.0020  total $0.0030",
        "Done.",
        "exit Submitted",
    ):
        assert expected in text
    # A command already shown from the tool call is not printed again for its action event.
    assert text.count("$ cat parser.py") == 1
    # The content stays in the context, but its exposure is reported once.
    assert text.count("exposure: the model now sees content of output-line") == 1
    assert "[main" not in text


def test_hidden_thinking_is_named(tmp_path):
    message = reply("Working.", "ls") | {"thinking_blocks": [{"type": "thinking", "thinking": "", "signature": "s"}]}
    log = write_log(tmp_path / "s", [event("model_result", 1, 1, cost_usd=0.0, message=message)])
    assert "thinking hidden by the provider (display omitted)" in render(log)


def multi_agent_events():
    return [
        event("session_start", 1, session_id="t1", task_id="demo-task"),
        event("agent_start", 2, agent_id="lead", role="orchestrator", model="claude-haiku-5-5", task="split the work"),
        event("model_result", 3, 1, agent_id="lead", cost_usd=0.003, message=reply("Delegating.", "true")),
        event("agent_message", 4, 1, agent_id="lead", to="worker-1", content="write the parser"),
        event("agent_start", 5, 1, agent_id="worker-1", parent_id="lead", role="worker", task="write the parser"),
        event("agent_start", 6, 1, agent_id="worker-2", parent_id="lead", role="worker", task="write the tests"),
        event("model_result", 7, 2, agent_id="worker-1", cost_usd=0.001, message=reply("Parser.", "vi parser.py")),
        event("model_result", 8, 3, agent_id="worker-2", cost_usd=0.002, message=reply("Tests.", "vi test.py")),
        event("agent_end", 9, 3, agent_id="worker-1", status="done", final="parser written"),
        event("agent_message", 10, 3, agent_id="worker-1", to="lead", content="parser written"),
        event("session_end", 11, 3, status="complete", patch_status="ready"),
    ]


def test_multi_agent_lines_are_labelled_nested_and_costed(tmp_path):
    text = render(write_log(tmp_path / "s", multi_agent_events()))
    assert "[lead orchestrator] agent lead started (orchestrator, claude-haiku-5-5)" in text
    assert "  [worker-1 worker] agent worker-1 started by lead (worker)" in text
    assert "  [worker-2 worker] ● step 3 (call 1)" in text
    assert "[worker-1 worker] to lead" in text
    assert "[worker-1 worker] agent worker-1 ended: done  calls 1  $0.0010" in text
    assert "total $0.0060" in text
    assert "[lead orchestrator] calls 1  $0.0030" in text


def test_agent_filter_keeps_the_named_agents_and_session_events(tmp_path):
    text = render(write_log(tmp_path / "s", multi_agent_events()), agents=["worker-2"])
    assert "Tests." in text and "session end" in text
    assert "Parser." not in text and "Delegating." not in text
    # The filtered-out parent still places worker-2 under it.
    assert "  [worker-2 worker] ● step 3" in text


def test_tail_waits_for_a_complete_line(tmp_path):
    path = tmp_path / "events.jsonl"
    record = json.dumps(event("session_start", 1))
    path.write_text(record[:10])
    tail = Tail(path)
    assert tail.read() == []
    with path.open("a") as stream:
        stream.write(record[10:] + "\n")
    assert [e["kind"] for e in tail.read()] == ["session_start"]
    assert tail.read() == []


def test_tail_waits_inside_a_multi_byte_character(tmp_path):
    path = tmp_path / "events.jsonl"
    record = (json.dumps(event("raw_output", 1, output="state → done"), ensure_ascii=False) + "\n").encode()
    split = record.index("→".encode()) + 1
    path.write_bytes(record[:split])
    tail = Tail(path)
    assert tail.read() == []
    with path.open("ab") as stream:
        stream.write(record[split:])
    assert [e["output"] for e in tail.read()] == ["state → done"]


def test_a_command_that_did_not_finish_is_named(tmp_path):
    events = [
        event("action", 1, 1, command="sleep 999"),
        event("raw_output", 2, 1, output="", returncode=-1, exception_info="timed out after 300 s"),
    ]
    assert "command did not finish normally: timed out after 300 s" in render(write_log(tmp_path / "s", events))


def test_follow_prints_events_as_they_are_written_and_stops_at_session_end(tmp_path):
    session = tmp_path / "s"
    session.mkdir()
    events = single_agent_events()

    def write_slowly():
        for record in events:
            with (session / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            time.sleep(0.005)

    writer = threading.Thread(target=write_slowly)
    writer.start()
    text = render(session, follow=True)
    writer.join()
    assert "Reading the parser." in text and "Saving a note." in text and "session end" in text


def test_campaign_lists_a_shared_copy_once_in_start_order(tmp_path):
    root = campaign(tmp_path)
    later = single_agent_events()
    earlier = [e | {"ts": e["ts"].replace("20:00", "19:00")} for e in single_agent_events()]
    write_log(root / "clean" / "demo--r001" / "sessions" / "002-t2", later)
    copy = write_log(root / "cases" / "demo--canary--r001" / "arms" / "clean" / "sessions" / "002-t2", later)
    first = write_log(root / "clean" / "demo--r001" / "sessions" / "001-t1", earlier)
    # Identical copies keep the first path in sorted order, so the listing does not depend on directory order.
    assert session_logs(root) == [first, copy]
    assert render(root).count("session end") == 2


def test_cli_watch_replays_a_session(tmp_path, capsys):
    session = tmp_path / "s"
    write_log(session, single_agent_events())
    assert main(["context-eval", "watch", str(session)]) == 0
    assert "Reading the parser." in capsys.readouterr().out
    assert main(["context-eval", "watch", str(tmp_path / "missing")]) == 2
