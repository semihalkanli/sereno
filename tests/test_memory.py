"""Offline checks for the memory layer: the memory tool's commands and what the harness loads at session start."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from sereno.apps.memory import INDEX, INDEX_MAX_LINES, Memory, MemoryArgs, memory
from sereno.chain import load_chain
from sereno.memory import session_reminder
from sereno.model import ScriptedModel
from sereno.runner import run_chain
from sereno.tools import ToolError

NOW = datetime(2026, 11, 12, 8, 30)
NOTE = "---\nname: hotel-rule\ndescription: Sarah's hotel rule\ntype: user\n---\n\nPay at the hotel.\n"


def _world(now=NOW):
    store = Memory()
    return SimpleNamespace(now=now, app=lambda name: store), store


def run(world, **args):
    return memory(world, MemoryArgs(**args))


def test_create_stamps_modified_with_the_world_clock():
    world, store = _world()
    assert run(world, command="create", path="/memories/hotel.md", file_text=NOTE) == (
        "File created successfully at: /memories/hotel.md"
    )
    (item,) = store.files
    assert item.modified == NOW
    assert "modified: 2026-11-12T08:30:00\n---\n\nPay at the hotel." in item.content

    world.now = datetime(2026, 11, 13, 20, 10)
    result = run(world, command="str_replace", path="/memories/hotel.md", old_str="Pay at", new_str="Always pay at")
    assert result.startswith("The memory file has been edited.\n")
    assert "     8\tAlways pay at the hotel." in result
    assert item.content.count("modified:") == 1
    assert "modified: 2026-11-13T20:10:00" in item.content


def test_file_without_frontmatter_gets_no_stamp():
    world, store = _world()
    run(world, command="create", path=INDEX, file_text="- [Hotel rule](hotel.md) - pay at the hotel\n")
    assert store.files[0].modified is None
    assert "modified" not in store.files[0].content


@pytest.mark.parametrize(
    "path", ["", "notes.md", "/memories/../x.md", "/etc/passwd", "/memories//b.md", "/memories/%2e%2e/x"]
)
def test_paths_stay_under_memories(path):
    world, _ = _world()
    with pytest.raises(ToolError, match="not under /memories"):
        run(world, command="create", path=path, file_text="x")


def test_view_lists_and_shows_files():
    world, _ = _world()
    assert run(world, command="view", path="/memories").endswith("node_modules:\n0\t/memories")
    run(world, command="create", path="/memories/a.md", file_text="one\ntwo\nthree\n")
    run(world, command="create", path="/memories/trip/london/hotel.md", file_text="x" * 2048)
    listing = run(world, command="view", path="/memories/").splitlines()
    assert listing[1:] == [
        "2.0K\t/memories",
        "14\t/memories/a.md",
        "2.0K\t/memories/trip",
        "2.0K\t/memories/trip/london",
    ]
    assert run(world, command="view", path="/memories/a.md") == (
        "Here's the content of /memories/a.md with line numbers:\n     1\tone\n     2\ttwo\n     3\tthree"
    )
    assert run(world, command="view", path="/memories/a.md", view_range=[2, -1]).endswith("     2\ttwo\n     3\tthree")
    with pytest.raises(ToolError, match="does not exist. Please provide a valid path."):
        run(world, command="view", path="/memories/missing.md")


def test_str_replace_errors():
    world, _ = _world()
    run(world, command="create", path="/memories/a.md", file_text="two two\n")
    with pytest.raises(ToolError, match="Multiple occurrences of old_str `two` in lines: 1"):
        run(world, command="str_replace", path="/memories/a.md", old_str="two", new_str="2")
    with pytest.raises(ToolError, match="did not appear verbatim"):
        run(world, command="str_replace", path="/memories/a.md", old_str="three", new_str="3")


def test_insert_delete_rename():
    world, store = _world()
    run(world, command="create", path="/memories/a.md", file_text="one\nthree")
    assert run(world, command="insert", path="/memories/a.md", insert_line=1, insert_text="two\n") == (
        "The file /memories/a.md has been edited."
    )
    assert store.files[0].content == "one\ntwo\nthree"
    with pytest.raises(ToolError, match=r"within the range of lines of the file: \[0, 3\]"):
        run(world, command="insert", path="/memories/a.md", insert_line=9, insert_text="x")
    run(world, command="create", path="/memories/trip/b.md", file_text="b")
    assert run(world, command="rename", old_path="/memories/trip", new_path="/memories/london") == (
        "Successfully renamed /memories/trip to /memories/london"
    )
    assert {f.path for f in store.files} == {"/memories/a.md", "/memories/london/b.md"}
    with pytest.raises(ToolError, match="already exists"):
        run(world, command="rename", old_path="/memories/a.md", new_path="/memories/london/b.md")
    with pytest.raises(ToolError, match="cannot be deleted"):
        run(world, command="delete", path="/memories")
    assert run(world, command="delete", path="/memories/london") == "Successfully deleted /memories/london"
    assert [f.path for f in store.files] == ["/memories/a.md"]


def test_index_near_and_over_the_limit():
    world, store = _world()
    near = run(world, command="create", path=INDEX, file_text="- line\n" * 170)
    assert "near the read limit" in near
    with pytest.raises(ToolError, match="The file was saved"):
        run(world, command="create", path=INDEX, file_text="- line\n" * (INDEX_MAX_LINES + 1))
    assert len(store.files[0].content.splitlines()) == INDEX_MAX_LINES + 1


def test_reminder_loads_user_file_and_the_head_of_the_index():
    _, store = _world()
    assert "no index yet" in session_reminder("", store)
    world, store = _world()
    lines = [f"- [n{i}](n{i}.md) - hook {i}" for i in range(INDEX_MAX_LINES + 5)]
    run(world, command="create", path="/memories/notes.md", file_text="topic file body")
    with pytest.raises(ToolError):
        run(world, command="create", path=INDEX, file_text="\n".join(lines))
    text = session_reminder("Pay at the hotel.", store)
    assert text.startswith("<system-reminder>\n") and text.endswith("\n</system-reminder>")
    assert "Pay at the hotel." in text
    assert lines[INDEX_MAX_LINES - 1] in text and lines[INDEX_MAX_LINES] not in text
    assert "WARNING" in text
    assert "topic file body" not in text


def _call(call_id, args):
    return {"id": call_id, "type": "function", "function": {"name": "memory", "arguments": json.dumps(args)}}


CHAIN = {
    "id": "remember",
    "name": "Remember",
    "summary": "A fact written in s1 is loaded in s2.",
    "apps": ["contacts"],
    "system_prompt": "Assistant of {{owner_name}}.",
    "memory": True,
    "user_file": "Sign messages 'Lena'.",
    "sessions": [
        {"id": "s1", "now": "2026-03-02T09:00", "turns": ["Remember that I prefer aisle seats."]},
        {"id": "s2", "now": "2026-03-04T10:00", "turns": ["What seat do I like?"]},
    ],
}
WORLD = {"now": "2026-03-01T18:00", "owner": {"name": "Lena Brandt", "email": "lena@example.com"}}
SOLUTION = {
    "s1": [
        {
            "tool_calls": [
                _call(
                    "c1",
                    {"command": "create", "path": "/memories/seat.md", "file_text": "---\ntype: user\n---\nAisle."},
                ),
                _call("c2", {"command": "create", "path": INDEX, "file_text": "- [Seat](seat.md) - prefers aisle"}),
            ]
        },
        {"content": "Saved."},
    ],
    "s2": [{"content": "Aisle."}],
}


def test_memory_carries_over_and_loads_at_session_start(tmp_path):
    directory = tmp_path / "remember"
    directory.mkdir()
    (directory / "chain.json").write_text(json.dumps(CHAIN))
    (directory / "world.json").write_text(json.dumps(WORLD))
    loaded = load_chain("remember", root=tmp_path)
    models = {sid: ScriptedModel(steps) for sid, steps in SOLUTION.items()}
    run_chain(loaded, models.__getitem__, tmp_path / "events.jsonl", "test")

    first, second = models["s1"].requests[0], models["s2"].requests[0]
    assert first[0]["role"] == "system" and "# Memory" in first[0]["content"]
    assert first[1]["role"] == "user" and "no index yet" in first[1]["content"]
    assert "Sign messages 'Lena'." in first[1]["content"]
    assert "- [Seat](seat.md) - prefers aisle" in second[1]["content"]
    assert second[2] == {"role": "user", "content": "What seat do I like?"}
