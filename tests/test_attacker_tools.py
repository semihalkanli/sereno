"""The bounded tool catalog over one fixture request."""

import json

from attacker_world import CODE, INERT, NOTE, make_config, make_request, write_request

from sereno.attacker.config import LoopSettings
from sereno.attacker.observation import Transcript, envelope, render_message
from sereno.attacker.protocol import envelope_text, read_request
from sereno.attacker.tools import Toolbox

NOTICE_TEXT = "GUARD_NOTICE_FIXTURE_TEXT"


def toolbox(**contract) -> Toolbox:
    return Toolbox(make_request(**contract), LoopSettings())


def test_read_file_serves_views_and_queues_index_paths():
    tools = toolbox()
    text, failed = tools.execute("read_file", json.dumps({"path": "/app/pkg/core.py"}))
    result = json.loads(text)
    assert not failed and result["status"] == "ok" and result["text"] == CODE
    text, failed = tools.execute("read_file", json.dumps({"path": "/app/README.md"}))
    result = json.loads(text)
    assert not failed and result["status"] == "queued"
    assert tools.queued == ["/app/README.md"]
    text, failed = tools.execute("read_file", json.dumps({"path": "/etc/passwd"}))
    assert failed and json.loads(text)["status"] == "denied"


def test_read_file_is_bounded_by_the_task_start_index():
    tools = toolbox()
    text, failed = tools.execute("read_file", json.dumps({"path": "/app/src/created_later.py"}))
    result = json.loads(text)
    assert failed and result["status"] == "denied"
    assert tools.queued == []


def test_reads_are_bounded_by_the_contract():
    tools = toolbox(max_reads_per_call=1)
    tools.execute("read_file", json.dumps({"path": "/app/README.md"}))
    text, failed = tools.execute("read_file", json.dumps({"path": "/app/tests/test_core.py"}))
    assert failed and json.loads(text)["status"] == "denied"
    assert tools.queued == ["/app/README.md"]


def test_transcript_pages_and_searches():
    tools = toolbox()
    text, _ = tools.execute("read_transcript", json.dumps({"session": "current", "offset": 0, "limit": 1}))
    result = json.loads(text)
    assert result["returned"] == 1 and result["next"] == 1 and result["total"] == 3
    text, _ = tools.execute("search_transcript", json.dumps({"query": "fixture task", "session": "all"}))
    result = json.loads(text)
    assert result["matches"] and result["matches"][0]["session"] == "current"
    text, _ = tools.execute("read_transcript", json.dumps({"session": "earlier", "limit": 5}))
    assert json.loads(text)["total"] == 2


def test_earlier_sessions_are_named_by_the_key_the_harness_writes():
    request = make_request()
    tools = Toolbox(request, LoopSettings())
    listed = json.loads(tools.execute("list_sessions", "{}")[0])
    assert [session["id"] for session in listed["sessions"]] == ["earlier"]
    cards = envelope(request, LoopSettings())["observation"]["completed_sessions"]["sessions"]
    assert [card["id"] for card in cards] == ["earlier"]


def test_render_message_shows_thinking_summary():
    line = render_message(0, {"role": "assistant", "content": "visible", "thinking": "summary text"})
    assert "[thinking] summary text" in line and "visible" in line


def test_transcript_tools_expose_thinking_summary():
    request = make_request()
    request["observation"]["transcript"].append({"role": "assistant", "content": "visible", "thinking": "summary text"})
    tools = Toolbox(request, LoopSettings())
    last = len(request["observation"]["transcript"]) - 1
    text, failed = tools.execute("read_transcript", json.dumps({"session": "current", "offset": last, "limit": 1}))
    assert not failed and "[thinking] summary text" in json.loads(text)["messages"][0]
    text, failed = tools.execute("search_transcript", json.dumps({"query": "summary text", "session": "current"}))
    assert not failed and json.loads(text)["matches"]


def test_envelope_shows_thinking_summary():
    request = make_request()
    request["observation"]["transcript"].append({"role": "assistant", "content": "visible", "thinking": "summary text"})
    tail = envelope(request, LoopSettings())["observation"]["transcript"]
    assert any("[thinking] summary text" in message for message in tail["messages"])


def test_render_message_names_redacted_thinking():
    line = render_message(0, {"role": "assistant", "content": "visible", "thinking_redacted": True})
    assert "[redacted thinking]" in line


def test_render_message_has_no_notice_marker():
    """Notices never reach the request, so the renderer names none; a stray mark would announce one."""
    line = render_message(0, {"role": "system", "content": "text", "notice_id": "guard-fixture"})
    assert "notice" not in line.lower() and line == "[0000] system: text"


def test_a_hand_built_request_with_a_marked_notice_is_cleaned(tmp_path):
    """The runtime's second layer: a notice that reaches a request in either recorded form is dropped, silently."""
    request = make_request()
    transcript = request["observation"]["transcript"]
    transcript.insert(1, {"role": "system", "content": NOTICE_TEXT, "extra": {"notice_id": "guard-a"}})
    transcript.insert(2, {"role": "system", "content": NOTICE_TEXT, "notice_id": "guard-b"})
    request["observation"]["completed_sessions"][0]["transcript"].insert(
        1, {"role": "system", "content": NOTICE_TEXT, "extra": {"notice_id": "guard-c"}}
    )
    # The envelope applies the same predicate even before validation, on the transcript and the session cards.
    raw = envelope(request, LoopSettings())
    assert raw["observation"]["transcript"]["total"] == 3
    assert NOTICE_TEXT not in json.dumps(raw) and "notice_id" not in json.dumps(raw)
    assert raw["observation"]["completed_sessions"]["sessions"][0]["messages"] == 2
    cleaned = read_request(write_request(tmp_path, request))
    assert [message["content"] for message in cleaned["observation"]["transcript"]] == [
        "fixture task",
        "fixture answer",
        "fixture tool result",
    ]
    assert [message["content"] for message in cleaned["observation"]["completed_sessions"][0]["transcript"]] == [
        "earlier task",
        "earlier answer",
    ]
    # Nothing the model sees or the tools return names a removed message.
    text = envelope_text(make_config(tmp_path), cleaned)
    assert NOTICE_TEXT not in text and "notice_id" not in text and "[notice" not in text
    tools = Toolbox(cleaned, LoopSettings())
    page = json.loads(tools.execute("read_transcript", json.dumps({"session": "current", "limit": 40}))[0])
    assert page["total"] == 3
    assert [line.split(" ", 1)[0] for line in page["messages"]] == ["[0000]", "[0001]", "[0002]"]
    listed = json.loads(tools.execute("list_sessions", "{}")[0])
    assert listed["current"]["messages"] == 3 and listed["sessions"][0]["messages"] == 2


def test_transcript_drops_a_marked_notice_without_a_validated_request():
    """A Transcript built straight from an uncleaned observation applies the same predicate and counts the kept."""
    request = make_request()
    request["observation"]["transcript"].insert(1, {"role": "system", "content": NOTICE_TEXT, "notice_id": ""})
    request["observation"]["completed_sessions"][0]["transcript"].insert(
        1, {"role": "system", "content": NOTICE_TEXT, "extra": {"notice_id": ""}}
    )
    transcript = Transcript(request["observation"])
    assert len(transcript.current) == 3
    described = transcript.describe()
    assert described["current"]["messages"] == 3 and described["sessions"][0]["messages"] == 2
    assert transcript.search(NOTICE_TEXT, "all", 10)["matches"] == []


def test_memory_lists_and_reads():
    tools = toolbox()
    text, _ = tools.execute("read_memory", "{}")
    listing = json.loads(text)
    assert listing["files"][0]["path"] == NOTE and listing["files"][0]["owned"] is False
    text, _ = tools.execute("read_memory", json.dumps({"path": NOTE, "start_line": 1, "end_line": 1}))
    result = json.loads(text)
    assert result["status"] == "ok" and result["text"] == "carried fixture line\n"


def test_stage_actions_validate_and_round_trip():
    tools = toolbox()
    text, failed = tools.execute("stage_action", json.dumps({"op": "write", "path": NOTE, "content": INERT}))
    assert not failed and json.loads(text)["status"] == "staged"
    text, _ = tools.execute("list_staged_actions", "{}")
    assert json.loads(text)["staged_actions"] == [{"op": "write", "path": NOTE, "bytes": len(INERT)}]
    text, failed = tools.execute("stage_action", json.dumps({"op": "write", "path": "relative.md", "content": "x"}))
    assert failed and json.loads(text)["status"] == "denied"
    text, failed = tools.execute("stage_action", json.dumps({"op": "delete", "path": NOTE, "content": "x"}))
    assert failed and json.loads(text)["status"] == "denied"
    text, failed = tools.execute("drop_action", json.dumps({"path": NOTE}))
    assert not failed and json.loads(text)["status"] == "removed"
    assert tools.staged == {}


def test_strategy_and_notebook_limits():
    tools = toolbox(strategy_max_bytes=5, notebook_max_bytes=5)
    text, failed = tools.execute("set_strategy", json.dumps({"text": "123456"}))
    assert failed and json.loads(text)["status"] == "denied"
    text, failed = tools.execute("set_strategy", json.dumps({"text": "12345"}))
    assert not failed and tools.strategy_dirty and tools.strategy == "12345"
    text, failed = tools.execute("set_notebook", json.dumps({"text": "123456"}))
    assert failed and json.loads(text)["status"] == "denied"
    text, failed = tools.execute("set_notebook", json.dumps({"text": "12345"}))
    assert not failed and tools.notebook_dirty and tools.notebook == "12345"


# The model's JSON escape for a lone surrogate: `json.loads` decodes it to a character no UTF-8 encoder accepts.
SURROGATE = '"\\ud800"'


def test_a_lone_surrogate_argument_is_a_denied_tool_call():
    """A JSON escape for a lone surrogate decodes to a character no UTF-8 file or transport body can hold. Every text
    argument refuses it as a denied tool result, so the loop continues and the response stays encodable."""
    tools = toolbox()
    for name, arguments in (
        ("stage_action", '{"op": "write", "path": ' + SURROGATE + ', "content": "x"}'),
        ("stage_action", '{"op": "write", "path": "' + NOTE + '", "content": ' + SURROGATE + "}"),
        ("drop_action", '{"path": ' + SURROGATE + "}"),
        ("set_strategy", '{"text": ' + SURROGATE + "}"),
        ("set_notebook", '{"text": ' + SURROGATE + "}"),
        ("read_memory", '{"path": ' + SURROGATE + "}"),
        ("search_transcript", '{"query": ' + SURROGATE + "}"),
        ("find_files", '{"pattern": ' + SURROGATE + "}"),
        ("finish", '{"note": ' + SURROGATE + "}"),
    ):
        text, failed = tools.execute(name, arguments)
        assert failed and json.loads(text)["status"] == "denied", name
        assert "UTF-8" in json.loads(text)["reason"], name
    assert tools.staged == {} and not tools.finished


def test_a_surrogate_result_is_escaped_rather_than_raising():
    """`clamp` is the last encode before the transport: a result built from anything unencodable is escaped instead of
    failing the call."""
    tools = toolbox()

    class Echo(dict):
        pass

    text = tools.clamp({"status": "ok", "value": "\ud800"})
    assert json.loads(text)["value"] == "\ud800"


def test_find_files_and_search_files():
    tools = toolbox()
    text, _ = tools.execute("find_files", json.dumps({"pattern": "*.py"}))
    files = json.loads(text)["files"]
    assert "/app/pkg/core.py" in files and "/app/tests/test_core.py" in files
    text, _ = tools.execute("search_files", json.dumps({"pattern": "return 1"}))
    matches = json.loads(text)["matches"]
    assert matches and matches[0]["path"] == "/app/pkg/core.py" and matches[0]["line"] == 2


def test_unknown_tool_bad_json_and_finish():
    tools = toolbox()
    text, failed = tools.execute("nope", "{}")
    assert failed and "unknown tool" in text
    text, failed = tools.execute("read_file", "not json")
    assert failed and json.loads(text)["status"] == "error"
    text, failed = tools.execute("finish", json.dumps({"note": "done"}))
    assert not failed and tools.finished and tools.finish_note == "done"
