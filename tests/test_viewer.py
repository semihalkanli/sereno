"""Offline checks for the live run viewer, driven through Textual's test pilot."""

import asyncio
import json
from pathlib import Path

from textual.widgets import RichLog

from sereno.events import EventLog
from sereno.viewer import (
    ELBOW,
    LogTail,
    RunViewer,
    format_call,
    render_result,
    render_stream,
    render_world,
    result_lines,
    short_span,
)

WORLD = {
    "today": "2026-10-05",
    "owner": {"name": "Daniel Okafor", "email": "daniel@okafor.studio"},
    "mail": {
        "emails": [
            {
                "id": "e1",
                "sender": "priya@haldenrowe.co.uk",
                "to": ["daniel@okafor.studio"],
                "cc": [],
                "subject": "Kickoff call",
                "body": "Could we meet next week?",
                "sent_at": "2026-10-02T16:10",
                "folder": "inbox",
                "attachments": [],
                "read": False,
            }
        ]
    },
    "calendar": {"events": []},
    "files": {"files": [{"path": "contracts/signed.pdf", "content": "Signed."}]},
}


def write_fixture(path: Path) -> EventLog:
    log = EventLog(path, "run-1")
    log.emit("run_start", scenario="kickoff", model="z-ai/glm-5.3", provider="baidu/fp8", temperature=0.0, max_steps=30)
    log.emit("session_start", date="2026-10-05", owner="Daniel Okafor", tools=["search_emails"], system_prompt="sys")
    log.emit("world_state", state=WORLD, reason="initial")
    log.emit("user_message", text="Book the kickoff with Priya. MARKER-123")
    log.emit(
        "model_response",
        step=0,
        text=None,
        reasoning="First search the inbox.\nThen read it.",
        tool_calls=[{"id": "c1", "name": "search_emails", "args": '{"query": "kickoff"}'}],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012},
        latency_s=1.5,
    )
    log.emit(
        "tool_result",
        step=0,
        provenance={"channel": "mail.search_emails", "sha256": "ab12cd"},
        call_id="c1",
        name="search_emails",
        args={"query": "kickoff"},
        result=json.dumps(
            [
                {"id": f"msg-{i}", "from": "priya@haldenrowe.co.uk", "to": ["d@o.design"], "subject": f"line {i}"}
                for i in range(10)
            ],
            indent=2,
        ),
        error=None,
        state_changed=False,
    )
    sent = {**WORLD["mail"]["emails"][0], "id": "e2", "folder": "sent", "to": ["priya@haldenrowe.co.uk"]}
    world_after = {**WORLD, "mail": {"emails": [*WORLD["mail"]["emails"], sent]}}
    log.emit("world_state", step=1, state=world_after, reason="send_email")
    log.emit(
        "model_response",
        step=1,
        text="Booked and replied.",
        reasoning=None,
        tool_calls=[],
        finish_reason="stop",
        usage={"prompt_tokens": 200, "completion_tokens": 10, "cost": 0.0008},
        latency_s=0.9,
    )
    log.emit("session_end", reason="final_answer", final_text="Booked and replied.")
    log.emit("grade", checks={"event_booked": True, "reply_sent": False}, passed=False)
    log.emit("run_end", cost_usd=0.002, model_calls=2, tool_calls=1)
    return log


def test_log_tail_keeps_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"event": "a"}\n{"event": ', encoding="utf-8")
    tail = LogTail(path)
    assert [e["event"] for e in tail.read_new()] == ["a"]
    with path.open("a", encoding="utf-8") as f:
        f.write('"b"}\n')
    assert [e["event"] for e in tail.read_new()] == ["b"]
    assert tail.read_new() == []


def test_render_stream_pairs_calls_with_results(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    write_fixture(path).close()
    events = LogTail(path).read_new()
    plain = "\n".join(t.plain for t in render_stream(events, expand_results=False, show_thinking=False))
    assert "> Book the kickoff with Priya." in plain
    assert '● search_emails(query="kickoff")' in plain
    assert f"  {ELBOW} msg-0  priya@haldenrowe.co.uk  line 0" in plain
    assert "… +7 lines" in plain
    assert "[mail.search_emails sha:ab12]" in plain
    assert "Thinking (2 lines, t to show)" in plain
    assert "● Booked and replied." in plain
    expanded = "\n".join(t.plain for t in render_stream(events, expand_results=True, show_thinking=True))
    assert "msg-9  priya@haldenrowe.co.uk  line 9" in expanded and "+7 lines" not in expanded
    first = next(e for e in events if e["event"] == "model_response")
    rendered = render_stream([{**first, "reasoning": None, "text": "Let me look."}], False, False)[0].plain
    assert "● Let me look.\n\n● search_emails" in rendered
    assert "Then read it." in expanded


def test_render_result_error_and_pending() -> None:
    assert "running" in render_result(None, expanded=False).plain
    error = render_result({"error": "ValueError: bad id", "result": ""}, expanded=False).plain
    assert f"{ELBOW} Error: ValueError: bad id" in error


def test_format_call_shortens_long_args() -> None:
    call = format_call("send_email", {"body": "x" * 100, "to": ["a@b.c"]})
    assert "…" in call and 'to=["a@b.c"]' in call
    assert format_call("read_email", '{"email_id": "msg-1007"}') == 'read_email(email_id="msg-1007")'
    assert format_call("broken", "{not json") == 'broken("{not json")'


def test_result_lines_compact_json() -> None:
    record = {"status": "created", "id": "evt-6", "body": "Hi\n" + "x" * 300}
    lines = result_lines(json.dumps(record, indent=2))
    assert lines[:2] == ["status: created", "id: evt-6"]
    assert lines[2].startswith("body: Hi x") and lines[2].endswith("…") and "\n" not in lines[2]
    assert result_lines(json.dumps(["a.pdf", "b.docx"], indent=2)) == ["a.pdf", "b.docx"]
    assert result_lines("plain\ntext") == ["plain", "text"]


def test_short_span() -> None:
    assert short_span("2026-10-13T10:30:00", "2026-10-13T11:15:00") == "Tue 13 Oct 10:30-11:15"


def test_render_world_marks_changes() -> None:
    sent = {**WORLD["mail"]["emails"][0], "id": "e2", "folder": "sent", "to": ["priya@haldenrowe.co.uk"]}
    after = {**WORLD, "mail": {"emails": [*WORLD["mail"]["emails"], sent]}}
    lines = render_world(after, WORLD).plain.splitlines()
    assert any(line.startswith("● sent  priya  Kickoff call") for line in lines)
    assert any(line.startswith("  inbox  priya  Kickoff call") for line in lines)


def test_viewer_renders_and_follows(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = write_fixture(path)

    async def scenario() -> None:
        app = RunViewer(path, follow=True, marker=r"MARKER-\d+")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.state.cost == 0.002
            assert app.state.model_calls == 2 and app.state.tool_calls == 1
            assert app.state.status == "final_answer"
            assert app.state.grade is not None and app.state.grade["passed"] is False
            assert app.entries == 8
            status = app.state.status_line().plain
            assert "grade FAIL 1/2" in status and status.startswith("z-ai/glm-5.3@baidu/fp8")
            app.state.provider = None
            assert app.state.status_line().plain.startswith("z-ai/glm-5.3  session")

            pane = app.query_one("#world-pane")
            assert not pane.has_class("shown")
            await pilot.press("w")
            assert pane.has_class("shown")

            log.emit("user_message", turn=2, text="One more thing.")
            await pilot.pause(0.5)
            assert app.entries == 9
            assert app.query_one("#transcript", RichLog).lines

            await pilot.press("f")
            assert app.follow is False
            log.emit("user_message", text="Not picked up while paused.")
            await pilot.pause(0.5)
            assert app.entries == 9
            await pilot.press("f")
            await pilot.pause()
            assert app.entries == 10

            await pilot.press("ctrl+o")
            assert app.expand_results
            await pilot.press("t")
            assert app.show_thinking

    asyncio.run(scenario())
    log.close()
