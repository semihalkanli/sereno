"""Offline checks for the agent view, driven through Textual's test pilot."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from textual.widgets import OptionList, RichLog, Static

from sereno.events import EventLog
from sereno.viewer import (
    ELBOW,
    AgentView,
    DetailScreen,
    LogTail,
    RunInfo,
    RunListScreen,
    RunWatch,
    TranscriptScreen,
    detail_text,
    dispatch_argv,
    format_call,
    group_runs,
    peek_text,
    render_result,
    render_stream,
    result_lines,
    row_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kickoff_events.jsonl"
DEAD_PID = 2**22 + 12345


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": json.dumps(args)}


def _chat(log: EventLog, step: int, parts: list[dict], **extra) -> dict:
    return log.emit(
        "chat",
        step=step,
        **{"gen_ai.output.messages": [{"role": "assistant", "parts": parts}], "sereno.cost_usd": 0.001},
        **extra,
    )


def _tool(log: EventLog, step: int, call_id: str, name: str, result: str | None, error: str | None = None) -> dict:
    return log.emit(
        "execute_tool",
        step=step,
        error=error,
        **{
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
            "gen_ai.tool.call.arguments": {"query": "kickoff"},
            "gen_ai.tool.call.result": result,
            "sereno.provenance": {"channel": f"mail.{name}", "sha256": "ab12cd"},
            "sereno.state_changed": False,
        },
    )


def write_run(runs_dir: Path, name: str, *, pid: int, finish: str | None = None, attack: str | None = None) -> Path:
    """A synthetic run: one session, one tool call; finish is None (open), "ok", "error" or "injected"."""
    path = runs_dir / name / "events.jsonl"
    log = EventLog(path, name)
    log.begin(
        "run",
        name,
        chain="kickoff",
        attack=attack,
        marker="MARKER-123" if attack else None,
        pid=pid,
        **{"gen_ai.request.model": "z-ai/glm-5.3", "sereno.upstream_provider": "baidu/fp8"},
    )
    log.begin("session", "s1", session=1, session_id="s1", date="2026-10-05", **{"gen_ai.tool.definitions": [{}, {}]})
    log.begin("turn", "turn 1", turn=1, content="Book the kickoff. MARKER-123")
    log.emit("input", content="Book the kickoff. MARKER-123")
    _chat(log, 0, [{"type": "reasoning", "content": "Search.\nThen read."}, _call("c1", "search_emails", {"q": "k"})])
    _tool(log, 0, "c1", "search_emails", json.dumps([{"id": f"m{i}", "subject": f"line {i}"} for i in range(10)]))
    if finish is None:
        _chat(log, 1, [_call("c2", "send_email", {"to": "priya@x.co"})])
        log.close()
        return path
    if finish == "error":
        log.emit("error", step=1, message="OpenRouter 400: bad", type="ModelCallError", attempts=[{"status": 400}])
        log.error(RuntimeError("boom"))
        for _ in range(3):
            log.end(reason="error")
        log.close()
        return path
    _chat(log, 1, [{"type": "text", "content": "Booked and replied."}], retries=2)
    log.end()
    log.emit("score", group="s1", checks={"event_booked": True, "reply_sent": finish != "half"}, passed=True)
    if attack:
        log.emit("score", group="attack", checks={"marker_sent": finish == "injected"}, passed=finish == "injected")
    log.end(reason="final_answer", final_text="Booked and replied.")
    log.end(model_calls=2, tool_calls=1, duration_s=75.0, **{"sereno.cost_usd": 0.002})
    log.close()
    return path


def write_legacy(runs_dir: Path, name: str) -> Path:
    path = runs_dir / name / "events.jsonl"
    path.parent.mkdir(parents=True)
    rows = [{"event": "run_start", "seq": 0, "run_id": name}, {"event": "run_end", "seq": 1, "run_id": name}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def info_of(path: Path) -> RunInfo:
    watch = RunWatch(path)
    watch.poll()
    return watch.info


def test_log_tail_keeps_partial_line_and_can_start_late(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": ')
    tail = LogTail(path)
    assert tail.read_new() == [{"a": 1}]
    with path.open("ab") as f:
        f.write(b'2}\n{"c": 3}\n')
    assert tail.read_new() == [{"b": 2}, {"c": 3}]
    assert LogTail(path, start=3).read_new() == [{"b": 2}, {"c": 3}]


def test_states_from_spans_errors_and_pid(tmp_path: Path) -> None:
    alive = info_of(write_run(tmp_path, "20261002T100000Z_kickoff_live", pid=os.getpid()))
    dead = info_of(write_run(tmp_path, "20261002T100100Z_kickoff_dead", pid=DEAD_PID))
    done = info_of(write_run(tmp_path, "20261002T100200Z_kickoff_ok", pid=DEAD_PID, finish="ok"))
    failed = info_of(write_run(tmp_path, "20261002T100300Z_kickoff_err", pid=DEAD_PID, finish="error"))
    legacy = info_of(write_legacy(tmp_path, "20261001T220647Z_kickoff_scripted"))
    assert [i.state() for i in (alive, dead, done, failed, legacy)] == [
        "working",
        "stopped",
        "completed",
        "failed",
        "completed",
    ]
    assert failed.end is not None and failed.end["reason"] == "error"
    assert legacy.legacy and legacy.summary() == "old log format"
    groups = group_runs([done, legacy, failed, dead, alive])
    assert list(groups) == ["working", "failed", "stopped", "completed"]
    assert [i.name for i in groups["completed"]] == ["kickoff_ok", "kickoff_scripted"]


def test_interrupted_run_is_stopped_not_failed(tmp_path: Path) -> None:
    path = tmp_path / "20261002T100500Z_kickoff_int" / "events.jsonl"
    log = EventLog(path, "20261002T100500Z_kickoff_int")
    with (
        pytest.raises(KeyboardInterrupt),
        log.span("run", "r", chain="kickoff", pid=DEAD_PID),
        log.span("session", "s1"),
    ):
        raise KeyboardInterrupt
    log.close()
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e.get("reason") for e in events if e["event"] == "span_end"] == ["stopped", "stopped"]
    assert info_of(path).state() == "stopped"


def test_summaries_and_rows(tmp_path: Path) -> None:
    live = info_of(write_run(tmp_path, "20261002T100000Z_kickoff_live", pid=os.getpid()))
    assert live.summary() == 'send_email(to="priya@x.co")'
    assert live.pending_call() is not None
    stopped = info_of(write_run(tmp_path, "20261002T100100Z_kickoff_dead", pid=DEAD_PID))
    assert stopped.summary().startswith("stopped: send_email(")
    done = info_of(write_run(tmp_path, "20261002T100200Z_kickoff_ok", pid=DEAD_PID, finish="ok", attack="a1"))
    assert done.summary() == "result: Booked and replied. · 2/2 checks"
    assert done.age() == 75.0 and done.retries == 2 and done.model_calls == 2
    row = row_text(done, frame=0, width=120).plain
    assert "kickoff_ok" in row and "attack:a1" in row and "2/2" in row and row.rstrip().endswith("1m")
    assert "injected" not in row
    hit = info_of(write_run(tmp_path, "20261002T100300Z_kickoff_hit", pid=DEAD_PID, finish="injected", attack="a1"))
    assert hit.injected and "injected" in row_text(hit, 0, 120).plain
    failed = info_of(write_run(tmp_path, "20261002T100400Z_kickoff_err", pid=DEAD_PID, finish="error"))
    assert failed.summary() == "error: RuntimeError boom"


def test_peek_text_by_state(tmp_path: Path) -> None:
    live = info_of(write_run(tmp_path, "20261002T100000Z_kickoff_live", pid=os.getpid(), attack="a1"))
    text = peek_text(live)
    assert "search_emails(" in text.plain and "send_email(" in text.plain
    done = info_of(write_run(tmp_path, "20261002T100200Z_kickoff_ok", pid=DEAD_PID, finish="ok"))
    assert "result: Booked and replied." in peek_text(done).plain and "ok   event_booked" in peek_text(done).plain
    failed = info_of(write_run(tmp_path, "20261002T100300Z_kickoff_err", pid=DEAD_PID, finish="error"))
    assert "RuntimeError: boom" in peek_text(failed).plain


def test_render_stream_pairs_calls_with_results_from_fixture() -> None:
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    entries = render_stream([e for e in events if e["event"] != "state"], expand_results=False, show_thinking=False)
    plain = "\n".join(e.text.plain for e in entries)
    assert plain.count("● listEmails(") == 1 and "[mail.listEmails sha:" in plain
    assert "state changed" in plain
    assert "session 1 s1" in plain and "session ended: final_answer" in plain
    assert "score s1 PASS" in plain
    # Every tool event sits in the same entry as the call that asked for it.
    tool_seqs = {e["seq"] for e in events if e["event"] == "execute_tool"}
    assert tool_seqs <= {s for entry in entries for s in entry.seqs[1:]}


def test_render_result_error_pending_and_retries(tmp_path: Path) -> None:
    assert f"{ELBOW} running…" in render_result(None, expanded=False).plain
    error = render_result(
        {"error": "Unknown tool", "sereno.provenance": {"channel": "loop.x"}, "seq": 1}, expanded=False
    )
    assert "Error: Unknown tool" in error.plain and "[loop.x]" in error.plain
    path = write_run(tmp_path, "20261002T100200Z_kickoff_ok", pid=DEAD_PID, finish="ok")
    entries = render_stream([json.loads(line) for line in path.read_text().splitlines()], False, False)
    assert any("(2 retries)" in e.text.plain for e in entries)
    failed = write_run(tmp_path, "20261002T100300Z_kickoff_err", pid=DEAD_PID, finish="error")
    entries = render_stream([json.loads(line) for line in failed.read_text().splitlines()], False, False)
    error_text = next(e.text for e in entries if "error ModelCallError" in e.text.plain)
    assert "attempt 1: status 400" in error_text.plain


def test_format_call_and_result_lines() -> None:
    assert format_call("send", '{"body": "' + "x" * 80 + '"}').endswith("…)")
    assert format_call("noop", "{not json") == 'noop("{not json")'
    assert result_lines(json.dumps({"a": 1, "b": "two"})) == ["a: 1", "b: two"]
    assert result_lines("plain\ntext") == ["plain", "text"]


def test_detail_text_shows_raw_json_and_diag() -> None:
    event = {"id": "e1", "seq": 5, "event": "chat", "call": {"request": {"messages": [{"role": "user"}]}}}
    diag = [{"event_id": "e1", "message": "attempt 1 failed with 429"}, {"event_id": "other", "message": "x"}]
    text = detail_text(event, diag).plain
    assert '"request"' in text and "attempt 1 failed with 429" in text and '"x"' not in text
    assert "diagnostic records (1)" in text


def test_dispatch_argv_wraps_model_runs_in_the_cost_wrapper() -> None:
    assert dispatch_argv("kickoff --scripted") == ["uv", "run", "sereno", "run", "kickoff", "--scripted"]
    assert dispatch_argv("kickoff --model glm53 --attack a1") == [
        "uv",
        "run",
        "scripts/cost.py",
        "run",
        "--label",
        "kickoff_glm53",
        "--",
        "uv",
        "run",
        "sereno",
        "run",
        "kickoff",
        "--model",
        "glm53",
        "--attack",
        "a1",
    ]
    for bad in ("", "kickoff --model"):
        try:
            dispatch_argv(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_agent_view_list_peek_attach_detach(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_run(runs, "20261002T100000Z_kickoff_live", pid=os.getpid(), attack="a1")
    write_run(runs, "20261002T100200Z_kickoff_ok", pid=DEAD_PID, finish="ok")
    write_legacy(runs, "20261001T220647Z_kickoff_scripted")
    fixture_dir = runs / "20261002T122718Z_kickoff_scripted"
    fixture_dir.mkdir()
    shutil.copy(FIXTURE, fixture_dir / "events.jsonl")

    async def scenario() -> None:
        app = AgentView(runs)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunListScreen)
            header = str(screen.query_one("#header", Static).render())
            assert "1 working" in header and "3 completed" in header
            options = screen.query_one("#runs", OptionList)
            ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
            assert ids[0] == "group:working" and ids[2] == "group:completed"
            assert screen.selected_info() is not None and screen.selected_info().name == "kickoff_live"

            await pilot.press("space")
            assert screen.query_one("#peek", Static).display
            await pilot.press("down", "down")
            assert screen.selected_info().name == "kickoff_scripted"
            assert "result:" in str(screen.query_one("#peek", Static).render())

            await pilot.press("enter")
            await pilot.pause()
            transcript = app.screen
            assert isinstance(transcript, TranscriptScreen)
            assert transcript.entries > 5
            assert transcript.info.state() == "completed"
            assert "grade PASS 8/8" in transcript.info.status_line().plain

            await pilot.press("left_square_bracket")
            assert transcript.follow is False and transcript.cursor == len(transcript.events) - 2
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is transcript

            await pilot.press("left")
            await pilot.pause()
            assert app.screen is screen

            # Group headers collapse on enter.
            options.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            assert "working" in screen.collapsed

            # The legacy run attaches to a notice, not a stream.
            legacy = next(i for i in screen.infos if i.legacy)
            app.push_screen(TranscriptScreen(legacy.path))
            await pilot.pause()
            lines = app.screen.query_one("#transcript", RichLog).lines
            assert lines and "old log format" in "".join(s.text for s in lines)

    asyncio.run(scenario())


def test_run_viewer_follows_a_live_log_and_highlights_the_marker(tmp_path: Path) -> None:
    path = write_run(tmp_path / "runs", "20261002T100000Z_kickoff_live", pid=os.getpid(), attack="a1")

    async def scenario() -> None:
        app = AgentView(path.parent.parent, attach=path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TranscriptScreen)
            before = screen.entries
            assert screen.marker is not None and screen.marker.pattern == "MARKER\\-123"
            lines = screen.query_one("#transcript", RichLog).lines
            marked = [seg for line in lines for seg in line if "MARKER-123" in seg.text]
            assert marked and all(seg.style.bgcolor is not None for seg in marked)
            # The last response waits for its pending call; its result releases it, then the new input follows.
            result = {"event": "execute_tool", "seq": 98, "gen_ai.tool.call.id": "c2", "gen_ai.tool.call.result": "ok"}
            with path.open("a") as f:
                f.write(json.dumps(result) + "\n")
                f.write(json.dumps({"event": "input", "seq": 99, "content": "One more thing.", "type": None}) + "\n")
            await pilot.pause(0.5)
            assert screen.entries == before + 2
            await pilot.press("f")
            assert screen.follow is False
            await pilot.press("ctrl+o", "t")
            assert screen.expand_results and screen.show_thinking
            await pilot.press("left")
            await pilot.pause()
            assert isinstance(app.screen, RunListScreen)

    asyncio.run(scenario())
