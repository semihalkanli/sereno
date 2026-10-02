"""The agent's own memory directory: Claude's memory tool, kept as Claude Code keeps auto memory.

One tool, `memory`, with the commands, arguments and result texts of the
memory tool in Anthropic's API (`memory_20250818`): view, create, str_replace,
insert, delete and rename on files under `/memories`. How the directory is
kept, one file per fact plus the index `MEMORY.md`, follows Claude Code's auto
memory: the harness loads the index at every session start (see
`sereno.memory`), stamps `modified` with the world clock on every file with a
frontmatter block, and reminds the agent when the index nears or passes its
read limit. A chain links this app by setting `memory: true`, not through
`apps`.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

ROOT = "/memories"
INDEX = f"{ROOT}/MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000
NEAR = 0.8
"""The share of either limit at which a write to the index gets a reminder to shorten it."""
VIEW_MAX_CHARS = 16_000
SNIPPET_LINES = 4
FRONTMATTER = re.compile(r"\A---\n(.*?\n)?---(\n|\Z)", re.DOTALL)


class MemoryFile(BaseModel):
    path: str
    content: str
    modified: datetime | None = None


class Memory(BaseModel):
    files: list[MemoryFile] = []


class MemoryArgs(BaseModel):
    command: Literal["view", "create", "str_replace", "insert", "delete", "rename"]
    path: str | None = Field(None, description="Path under /memories; every command but rename.")
    view_range: list[int] | None = Field(
        None, description="view of a file: [start_line, end_line], or [start_line, -1] to the end."
    )
    file_text: str | None = Field(None, description="create: the whole content of the file.")
    old_str: str | None = Field(None, description="str_replace: exact text to replace; must occur once.")
    new_str: str | None = Field(None, description="str_replace: the replacement; omitted deletes old_str.")
    insert_line: int | None = Field(None, description="insert: the line to insert after; 0 is the beginning.")
    insert_text: str | None = Field(None, description="insert: the text to insert.")
    old_path: str | None = Field(None, description="rename: the file or directory to move.")
    new_path: str | None = Field(None, description="rename: where to move it.")


def _clean(raw: str | None) -> str:
    """A path under /memories in canonical form, or a ToolError for anything outside it."""
    path = (raw or "").strip()
    if path.rstrip("/") == ROOT:
        return ROOT
    parts = path.removeprefix(f"{ROOT}/").split("/")
    if not path.startswith(f"{ROOT}/") or any(p in ("", ".", "..") or "%" in p or "\\" in p for p in parts):
        raise ToolError(f"The path {raw} is not under {ROOT}. Every memory path starts with {ROOT}/.")
    return f"{ROOT}/" + "/".join(parts)


def _file(memory: Memory, path: str) -> MemoryFile | None:
    return next((f for f in memory.files if f.path == path), None)


def _under(memory: Memory, path: str) -> list[MemoryFile]:
    prefix = path.rstrip("/") + "/"
    return [f for f in memory.files if f.path.startswith(prefix)]


def _size(n: int) -> str:
    if n < 1024:
        return str(n)
    return f"{n / 1024:.1f}K" if n < 1024**2 else f"{n / 1024**2:.1f}M"


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(lines, start=start))


def _stamp(content: str, now: datetime) -> str:
    """Set the frontmatter's `modified` line to `now`; content without frontmatter is left as it is."""
    m = FRONTMATTER.match(content)
    if m is None:
        return content
    lines = [line for line in (m.group(1) or "").splitlines() if not line.startswith("modified:")]
    lines.append(f"modified: {now.isoformat(timespec='seconds')}")
    return "---\n" + "\n".join(lines) + "\n---" + m.group(2) + content[m.end() :]


def index_over_limit(content: str) -> bool:
    return len(content.splitlines()) > INDEX_MAX_LINES or len(content.encode()) > INDEX_MAX_BYTES


def index_near_limit(content: str) -> bool:
    lines, size = len(content.splitlines()), len(content.encode())
    return lines > INDEX_MAX_LINES * NEAR or size > INDEX_MAX_BYTES * NEAR


def _save(world, path: str, content: str, result: str) -> str:
    """Write a file with its `modified` stamp, and check the index against its read limit."""
    memory = world.app("memory")
    content = _stamp(content, world.now)
    stamp = world.now if FRONTMATTER.match(content) else None
    item = _file(memory, path)
    if item is None:
        memory.files.append(MemoryFile(path=path, content=content, modified=stamp))
    else:
        item.content, item.modified = content, stamp
    if path != INDEX:
        return result
    size = f"MEMORY.md is now {len(content.splitlines())} lines and {len(content.encode())} bytes"
    if index_over_limit(content):
        # The write stands; the error tells the agent that the part past the limit will not load.
        raise ToolError(
            f"{size}, over the read limit of {INDEX_MAX_LINES} lines or {INDEX_MAX_BYTES} bytes. The file was "
            "saved, but everything past the limit is dropped at the next session start. Rewrite the index: one "
            "short line per file, detail in topic files, stale entries merged or removed."
        )
    if index_near_limit(content):
        result += (
            f"\n\n{size}, near the read limit of {INDEX_MAX_LINES} lines or {INDEX_MAX_BYTES} bytes. Keep one "
            "short line per file, move detail into topic files, and merge or drop stale entries."
        )
    return result


def _view(memory: Memory, path: str, view_range: list[int] | None) -> str:
    item = _file(memory, path)
    if item is not None:
        lines = item.content.splitlines()
        start, end = 1, len(lines)
        if view_range is not None:
            if len(view_range) != 2 or not 1 <= view_range[0] <= max(len(lines), 1):
                raise ToolError(
                    f"Invalid view_range {view_range}: give [start_line, end_line] within [1, {len(lines)}]."
                )
            start = view_range[0]
            end = len(lines) if view_range[1] == -1 else min(view_range[1], len(lines))
        body = _numbered(lines[start - 1 : end], start)
        if len(body) > VIEW_MAX_CHARS:
            body = body[:VIEW_MAX_CHARS] + "\n<the file is truncated here; view the rest with view_range>"
        return f"Here's the content of {path} with line numbers:\n{body}"
    files = _under(memory, path)
    if path != ROOT and not files:
        raise ToolError(f"The path {path} does not exist. Please provide a valid path.")
    rows = {path: sum(len(f.content.encode()) for f in files)}
    for f in files:
        parts = f.path[len(path) + 1 :].split("/")
        for depth in range(1, min(len(parts), 2) + 1):
            entry = f"{path}/{'/'.join(parts[:depth])}"
            rows[entry] = rows.get(entry, 0) + len(f.content.encode())
    listing = "\n".join(f"{_size(rows[p])}\t{p}" for p in sorted(rows))
    return (
        f"Here're the files and directories up to 2 levels deep in {path}, excluding hidden items and "
        f"node_modules:\n{listing}"
    )


def _str_replace(world, path: str, old: str, new: str) -> str:
    item = _file(world.app("memory"), path)
    if item is None:
        raise ToolError(f"The path {path} does not exist. Please provide a valid path.")
    count = item.content.count(old) if old else 0
    if count == 0:
        raise ToolError(f"No replacement was performed, old_str `{old}` did not appear verbatim in {path}.")
    if count > 1:
        lines = [n for n, line in enumerate(item.content.splitlines(), start=1) if old in line] or ["(spans lines)"]
        raise ToolError(
            f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
            f"{', '.join(map(str, lines))}. Please ensure it is unique"
        )
    first = item.content[: item.content.index(old)].count("\n") + 1
    content = item.content.replace(old, new)
    stamped = _stamp(content, world.now)
    if FRONTMATTER.match(content) and first > FRONTMATTER.match(content).group(0).count("\n"):
        first += stamped.count("\n") - content.count("\n")
    lines = stamped.splitlines()
    start = max(first - SNIPPET_LINES, 1)
    end = min(first + new.count("\n") + SNIPPET_LINES, len(lines))
    return _save(world, path, content, "The memory file has been edited.\n" + _numbered(lines[start - 1 : end], start))


def _insert(world, path: str, line: int, text: str) -> str:
    item = _file(world.app("memory"), path)
    if item is None:
        raise ToolError(f"The path {path} does not exist")
    lines = item.content.split("\n")
    if not 0 <= line <= len(lines):
        raise ToolError(
            f"Invalid `insert_line` parameter: {line}. It should be within the range of lines of the "
            f"file: [0, {len(lines)}]"
        )
    lines.insert(line, text.removesuffix("\n"))
    return _save(world, path, "\n".join(lines), f"The file {path} has been edited.")


def _rename(memory: Memory, old: str, new: str) -> str:
    if old == ROOT:
        raise ToolError(f"The {ROOT} directory itself cannot be renamed")
    moving = [_file(memory, old)] if _file(memory, old) else _under(memory, old)
    if not moving:
        raise ToolError(f"The path {old} does not exist")
    if new == ROOT or _file(memory, new) or _under(memory, new):
        raise ToolError(f"The destination {new} already exists")
    if new.startswith(old + "/"):
        raise ToolError(f"Cannot move {old} into itself")
    for f in moving:
        f.path = new + f.path[len(old) :]
    return f"Successfully renamed {old} to {new}"


def _require(args: MemoryArgs, *names: str) -> None:
    missing = [n for n in names if getattr(args, n) is None]
    if missing:
        raise ToolError(f"the {args.command} command needs {', '.join(missing)}")


def memory(world, args: MemoryArgs) -> str:
    store = world.app("memory")
    if args.command == "rename":
        _require(args, "old_path", "new_path")
        return _rename(store, _clean(args.old_path), _clean(args.new_path))
    _require(args, "path")
    path = _clean(args.path)
    if args.command == "view":
        return _view(store, path, args.view_range)
    if args.command == "delete":
        if path == ROOT:
            raise ToolError(f"The {ROOT} directory itself cannot be deleted")
        gone = [_file(store, path)] if _file(store, path) else _under(store, path)
        if not gone:
            raise ToolError(f"The path {path} does not exist")
        store.files = [f for f in store.files if f not in gone]
        return f"Successfully deleted {path}"
    if path == ROOT or _under(store, path):
        raise ToolError(f"The path {path} is a directory, not a file")
    if args.command == "create":
        _require(args, "file_text")
        return _save(world, path, args.file_text, f"File created successfully at: {path}")
    if args.command == "str_replace":
        _require(args, "old_str")
        return _str_replace(world, path, args.old_str, args.new_str or "")
    _require(args, "insert_line", "insert_text")
    return _insert(world, path, args.insert_line, args.insert_text)


APP = App(
    name="memory",
    title="memory",
    state=Memory,
    keys={"files": "path"},
    tools=[
        Tool(
            "memory",
            "Your memory directory, /memories, which persists between sessions. Commands: view (list a "
            "directory up to 2 levels deep, or show a file with line numbers, optionally a view_range), create "
            "(create or overwrite a file with file_text), str_replace (replace old_str, which must occur exactly "
            "once, with new_str), insert (insert insert_text after line insert_line; 0 is the beginning), delete "
            "(a file or a directory with its contents) and rename (move old_path to new_path). Every path starts "
            "with /memories; the /memories directory itself cannot be deleted or renamed. Keep the directory "
            "organized: update or delete files that are no longer right instead of adding new ones.",
            MemoryArgs,
            memory,
            writes=True,
        ),
    ],
)
