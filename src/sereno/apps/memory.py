"""The agent's own memory directory, after Claude Code's auto memory.

The agent keeps one file per fact plus an index, `MEMORY.md`, and reads and
writes them with file tools scoped to the directory. The harness loads the
index at every session start (see `sereno.memory`) and stamps `modified` with
the world clock on every file that has a frontmatter block. A chain links this
app by setting `memory: true`, not through `apps`.
"""

import re
from datetime import datetime

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.apps._common import find
from sereno.tools import Tool, ToolError

INDEX = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000
NEAR = 0.8
"""The share of either limit at which a write to the index gets a reminder to shorten it."""
FRONTMATTER = re.compile(r"\A---\n(.*?\n)?---(\n|\Z)", re.DOTALL)


class MemoryFile(BaseModel):
    path: str
    content: str
    modified: datetime | None = None


class Memory(BaseModel):
    files: list[MemoryFile] = []


def _path(raw: str) -> str:
    path = raw.strip().removeprefix("memory/").strip("/")
    parts = path.split("/")
    if not path or raw.strip().startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ToolError(f"Invalid path {raw!r}: give a path inside the memory directory, such as {INDEX!r}.")
    return path


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


def _save(world, path: str, content: str) -> dict:
    memory = world.app("memory")
    content = _stamp(content, world.now)
    stamp = world.now if FRONTMATTER.match(content) else None
    existing = next((f for f in memory.files if f.path == path), None)
    if existing is None:
        memory.files.append(MemoryFile(path=path, content=content, modified=stamp))
    else:
        existing.content, existing.modified = content, stamp
    result = {"path": path, "status": "updated" if existing else "created"}
    if path != INDEX:
        return result
    size = f"{INDEX} is now {len(content.splitlines())} lines and {len(content.encode())} bytes"
    if index_over_limit(content):
        # The write stands; the error tells the agent that the part past the limit will not load.
        raise ToolError(
            f"{size}, over the read limit of {INDEX_MAX_LINES} lines or {INDEX_MAX_BYTES} bytes. The file was "
            "saved, but everything past the limit is dropped at the next session start. Rewrite the index: one "
            "short line per file, detail in topic files, stale entries merged or removed."
        )
    if index_near_limit(content):
        result["reminder"] = (
            f"{size}, near the read limit of {INDEX_MAX_LINES} lines or {INDEX_MAX_BYTES} bytes. Keep one short "
            "line per file, move detail into topic files, and merge or drop stale entries."
        )
    return result


class ViewArgs(BaseModel):
    path: str = Field("", description="A file in the memory directory; empty to list the directory.")


class WriteArgs(BaseModel):
    path: str = Field(description="File path inside the memory directory, e.g. MEMORY.md or user_role.md.")
    content: str = Field(description="The whole new content of the file.")


class EditArgs(BaseModel):
    path: str
    old_string: str = Field(description="Exact text to replace; must occur once unless replace_all is set.")
    new_string: str
    replace_all: bool = False


class DeleteArgs(BaseModel):
    path: str


def memory_view(world, args: ViewArgs):
    files = world.app("memory").files
    if not args.path.strip().strip("/") or args.path.strip().strip("/") == "memory":
        if not files:
            return "The memory directory is empty."
        return "\n".join(f"{f.path}  ({len(f.content.encode())} bytes)" for f in sorted(files, key=lambda f: f.path))
    path = _path(args.path)
    item = find(files, f"No file {path!r} in the memory directory.", path=path)
    return "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(item.content.splitlines(), start=1)) or "(empty file)"


def memory_write(world, args: WriteArgs):
    return _save(world, _path(args.path), args.content)


def memory_edit(world, args: EditArgs):
    path = _path(args.path)
    item = find(world.app("memory").files, f"No file {path!r} in the memory directory.", path=path)
    count = item.content.count(args.old_string) if args.old_string else 0
    if count == 0:
        raise ToolError(f"old_string not found in {path}.")
    if count > 1 and not args.replace_all:
        raise ToolError(f"old_string occurs {count} times in {path}; give more context or set replace_all.")
    return _save(world, path, item.content.replace(args.old_string, args.new_string))


def memory_delete(world, args: DeleteArgs):
    memory = world.app("memory")
    path = _path(args.path)
    item = find(memory.files, f"No file {path!r} in the memory directory.", path=path)
    memory.files.remove(item)
    return {"path": path, "status": "deleted"}


APP = App(
    name="memory",
    title="memory",
    state=Memory,
    keys={"files": "path"},
    tools=[
        Tool(
            "memory_view",
            "List the memory directory, or read one of its files with line numbers.",
            ViewArgs,
            memory_view,
        ),
        Tool(
            "memory_write",
            "Create a file in the memory directory, or overwrite it with new content.",
            WriteArgs,
            memory_write,
            writes=True,
        ),
        Tool(
            "memory_edit",
            "Replace exact text in a file of the memory directory.",
            EditArgs,
            memory_edit,
            writes=True,
        ),
        Tool("memory_delete", "Delete a file from the memory directory.", DeleteArgs, memory_delete, writes=True),
    ],
)
