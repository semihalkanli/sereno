"""Offline checks for the memory layer: the memory app's tools and what the harness loads at session start."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from sereno.apps.memory import (
    INDEX_MAX_LINES,
    DeleteArgs,
    EditArgs,
    Memory,
    ViewArgs,
    WriteArgs,
    memory_delete,
    memory_edit,
    memory_view,
    memory_write,
)
from sereno.chain import load_chain
from sereno.memory import session_reminder
from sereno.model import ScriptedModel
from sereno.runner import run_chain
from sereno.tools import ToolError

NOW = datetime(2026, 11, 12, 8, 30)
NOTE = "---\nname: hotel-rule\ndescription: Sarah's hotel rule\ntype: user\n---\n\nPay at the hotel.\n"


def _world(now=NOW):
    memory = Memory()
    return SimpleNamespace(now=now, app=lambda name: memory), memory


def test_write_stamps_modified_with_the_world_clock():
    world, memory = _world()
    assert memory_write(world, WriteArgs(path="hotel_rule.md", content=NOTE))["status"] == "created"
    (item,) = memory.files
    assert item.modified == NOW
    assert "modified: 2026-11-12T08:30:00\n---\n\nPay at the hotel." in item.content

    world.now = datetime(2026, 11, 13, 20, 10)
    memory_edit(world, EditArgs(path="hotel_rule.md", old_string="Pay at", new_string="Always pay at"))
    assert item.content.count("modified:") == 1
    assert "modified: 2026-11-13T20:10:00" in item.content


def test_file_without_frontmatter_gets_no_stamp():
    world, memory = _world()
    memory_write(world, WriteArgs(path="MEMORY.md", content="- [Hotel rule](hotel_rule.md) - pay at the hotel\n"))
    assert memory.files[0].modified is None
    assert "modified" not in memory.files[0].content


@pytest.mark.parametrize("path", ["", "../x.md", "/etc/passwd", "a//b.md", "./a.md"])
def test_paths_stay_inside_the_directory(path):
    world, _ = _world()
    with pytest.raises(ToolError):
        memory_write(world, WriteArgs(path=path, content="x"))


def test_view_edit_delete():
    world, memory = _world()
    assert memory_view(world, ViewArgs()) == "The memory directory is empty."
    memory_write(world, WriteArgs(path="memory/a.md", content="one\ntwo two\n"))
    assert memory_view(world, ViewArgs()).startswith("a.md")
    assert memory_view(world, ViewArgs(path="a.md")) == "     1\tone\n     2\ttwo two"
    with pytest.raises(ToolError, match="2 times"):
        memory_edit(world, EditArgs(path="a.md", old_string="two", new_string="2"))
    memory_edit(world, EditArgs(path="a.md", old_string="two", new_string="2", replace_all=True))
    assert memory.files[0].content == "one\n2 2\n"
    memory_delete(world, DeleteArgs(path="a.md"))
    assert memory.files == []


def test_index_near_and_over_the_limit():
    world, memory = _world()
    near = memory_write(world, WriteArgs(path="MEMORY.md", content="- line\n" * 170))
    assert "near the read limit" in near["reminder"]
    with pytest.raises(ToolError, match="The file was saved"):
        memory_write(world, WriteArgs(path="MEMORY.md", content="- line\n" * (INDEX_MAX_LINES + 1)))
    assert len(memory.files[0].content.splitlines()) == INDEX_MAX_LINES + 1


def test_reminder_loads_user_file_and_the_head_of_the_index():
    _, memory = _world()
    assert "no index yet" in session_reminder("", memory)
    world, memory = _world()
    lines = [f"- [n{i}](n{i}.md) - hook {i}" for i in range(INDEX_MAX_LINES + 5)]
    memory_write(world, WriteArgs(path="notes.md", content="topic file body"))
    try:
        memory_write(world, WriteArgs(path="MEMORY.md", content="\n".join(lines)))
    except ToolError:
        pass
    text = session_reminder("Pay at the hotel.", memory)
    assert text.startswith("<system-reminder>\n") and text.endswith("\n</system-reminder>")
    assert "Pay at the hotel." in text
    assert lines[INDEX_MAX_LINES - 1] in text and lines[INDEX_MAX_LINES] not in text
    assert "WARNING" in text
    assert "topic file body" not in text


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


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
                _call("c1", "memory_write", {"path": "seat.md", "content": "---\nname: seat\ntype: user\n---\nAisle."}),
                _call("c2", "memory_write", {"path": "MEMORY.md", "content": "- [Seat](seat.md) - prefers aisle"}),
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
