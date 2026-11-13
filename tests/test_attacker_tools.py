"""The bounded tool catalog over one fixture request."""

import json

from attacker_world import CODE, INERT, NOTE, make_request

from sereno.attacker.config import LoopSettings
from sereno.attacker.observation import envelope, render_message
from sereno.attacker.tools import Toolbox


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
