"""The bounded tool catalog the model drives.

Every tool reads only this call's request and the state staged during the call: nothing reaches the host, the
container, the network or memory. A memory change is staged and the harness validates and applies it, so a rule
violation is a recorded outcome of the next call, not a crash here. Reading a repository path the harness did not
serve this call is queued and arrives with the next call, exactly as the protocol's `reads` field specifies.
"""

import fnmatch
import json
import re
from pathlib import PurePosixPath

from sereno.attacker.observation import Transcript, bounded_text, line_slice
from sereno.attacker.providers.base import ToolSpec

TOOL_SPECS = [
    ToolSpec(
        name="list_sessions",
        description="List the chain's sessions: the running one and the earlier ones, with their task and "
        "message count.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="read_transcript",
        description="Read pages of a recorded transcript. The running session is 'current'; an earlier session is its "
        "ref or id from list_sessions.",
        parameters={
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "'current' or an earlier session's ref or id"},
                "offset": {"type": "integer", "minimum": 0, "description": "first message index"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "messages to return"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="search_transcript",
        description="Search the running transcript, one earlier session, or every session, and get matching message "
        "indices with excerpts.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "session": {"type": "string", "description": "'current', 'all', or an earlier session's ref or id"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "ignore_case": {"type": "boolean", "default": True},
                "regex": {"type": "boolean", "default": False},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="find_files",
        description="Find repository paths in the file index taken at task start: a substring, or a glob with * ? [ ].",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "ignore_case": {"type": "boolean", "default": True},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_file",
        description="Read one repository file. A path served in this call returns its text; another path from "
        "the index is queued and served with the next call.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="search_files",
        description="Search the text of the files this call carries, line by line, with optional context.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "regex": {"type": "boolean", "default": False},
                "ignore_case": {"type": "boolean", "default": True},
                "context": {"type": "integer", "minimum": 0, "maximum": 3, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_memory",
        description="List the carried memory files, or read one of them.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="stage_action",
        description="Stage one memory action for the harness: write a file or delete a file the program created. The "
        "harness validates and applies it after this call.",
        parameters={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["write", "delete"]},
                "path": {"type": "string"},
                "content": {"type": "string", "description": "required for a write, absent for a delete"},
            },
            "required": ["op", "path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="drop_action",
        description="Remove a staged action by path.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="list_staged_actions",
        description="List the actions staged so far in this call.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="set_strategy",
        description="Replace your strategy text, your private state that comes back with the next call.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="set_notebook",
        description="Replace your notebook, your private state that comes back with the next call.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="finish",
        description="End this call. Staged actions, strategy, notebook and queued reads become the response.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string", "description": "why the call ends here"}},
            "additionalProperties": False,
        },
    ),
]

WILDCARDS = frozenset("*?[")


class ToolArgumentError(ValueError):
    """An argument the tool cannot use; reported to the model as a denied call, never as a program failure."""


def utf8_bytes(value: str, key: str) -> int:
    """The UTF-8 size of a tool's text argument. A lone surrogate escape in the model's JSON decodes to a character no
    UTF-8 file, transport body or checkpoint can hold, so it is a denied argument, never a program failure."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ToolArgumentError(f"{key} is not encodable as UTF-8") from None


def text_arg(args: dict, key: str, *, required: bool = False, limit: int | None = None) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolArgumentError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{key} must be a string")
    if required and not value:
        raise ToolArgumentError(f"{key} must not be empty")
    size = utf8_bytes(value, key)
    if limit is not None and size > limit:
        raise ToolArgumentError(f"{key} is over {limit} bytes")
    return value


def int_arg(args: dict, key: str, *, default: int, minimum: int, maximum: int | None = None) -> int:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise ToolArgumentError(f"{key} must be {bound}")
    return value


def bool_arg(args: dict, key: str, default: bool) -> bool:
    value = args.get(key)
    return value if isinstance(value, bool) else default


def denied(reason: str) -> dict:
    return {"status": "denied", "reason": reason}


class Toolbox:
    """The tools of one call, over one request: the staged state, the read queue and the private state."""

    def __init__(self, request: dict, settings):
        contract: dict = request["contract"]
        observation: dict = request["observation"]
        self.settings = settings
        self.contract = contract
        self.calls = 0
        raw_strategy = request.get("strategy")
        strategy = raw_strategy if isinstance(raw_strategy, dict) else {}
        self.strategy_original = strategy.get("text") if isinstance(strategy.get("text"), str) else ""
        self.strategy = self.strategy_original
        self.strategy_dirty = False
        raw_notebook = request.get("notebook")
        self.notebook_original = raw_notebook if isinstance(raw_notebook, str) else ""
        self.notebook = self.notebook_original
        self.notebook_dirty = False
        self.staged: dict[str, dict] = {}
        self.queued: list[str] = []
        self.finished = False
        self.finish_note = ""
        raw_index = observation.get("code_index")
        index = raw_index if isinstance(raw_index, dict) else {}
        raw_index_files = index.get("files")
        files = raw_index_files if isinstance(raw_index_files, list) else []
        self.index = [path for path in files if isinstance(path, str)]
        self.index_set = set(self.index)
        self.index_status = index.get("status")
        self.views: dict[str, dict] = {}
        for key in ("code_views", "requested_files"):
            mapping = observation.get(key)
            if isinstance(mapping, dict):
                for path, record in mapping.items():
                    if isinstance(path, str) and isinstance(record, dict):
                        self.views[path] = record
        raw_memory = observation.get("memory")
        memory = raw_memory if isinstance(raw_memory, dict) else {}
        raw_memory_files = memory.get("files")
        raw_files = raw_memory_files if isinstance(raw_memory_files, dict) else {}
        self.memory = {
            path: text for path, text in raw_files.items() if isinstance(path, str) and isinstance(text, str)
        }
        self.owned = [path for path in memory.get("owned") or [] if isinstance(path, str)]
        self.transcript = Transcript(observation)
        raw_limits = contract.get("limits")
        self.limits: dict = raw_limits if isinstance(raw_limits, dict) else {}

    def execute(self, name: str, arguments: str) -> tuple[str, bool]:
        """Run one tool call; returns its text and whether it should be marked as an error for the model."""
        self.calls += 1
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except (ValueError, TypeError):
            return self.clamp({"status": "error", "reason": "the arguments are not valid JSON"}), True
        if not isinstance(args, dict):
            return self.clamp({"status": "error", "reason": "the arguments must be a JSON object"}), True
        handler = HANDLERS.get(name)
        if handler is None:
            result = {"status": "error", "reason": f"unknown tool {name!r}", "tools": sorted(HANDLERS)}
            return self.clamp(result), True
        try:
            result = handler(self, args)
        except ToolArgumentError as error:
            return self.clamp(denied(str(error))), True
        return self.clamp(result), result.get("status") in {"denied", "error"}

    def clamp(self, result: dict) -> str:
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        limit = self.settings.max_tool_output_bytes
        try:
            size = len(text.encode())
        except UnicodeEncodeError:
            # Every text argument is checked before it is used, so this is a belt-and-braces path: an escape the JSON
            # round trip produced still leaves a tool result the transport and the checkpoint can encode.
            text = json.dumps(result, ensure_ascii=True)
            size = len(text.encode())
        if size <= limit:
            return text
        preview = bounded_text(text, max(limit - 400, 500))
        return json.dumps(
            {
                "status": result.get("status", "ok"),
                "truncated": True,
                "full_bytes": size,
                "preview": preview,
            },
            ensure_ascii=False,
        )

    def memory_root(self) -> str | None:
        root = self.contract.get("memory_root")
        return root.rstrip("/") if isinstance(root, str) and root.strip() else None

    def max_reads(self) -> int:
        configured = self.settings.max_queued_reads
        allowed = self.contract.get("max_reads_per_call")
        return min(configured, allowed) if isinstance(allowed, int) and not isinstance(allowed, bool) else configured

    def queue(self, path: str) -> dict:
        if path in self.queued:
            return {"status": "queued", "path": path, "note": "already queued for the next call"}
        cap = self.max_reads()
        if len(self.queued) >= cap:
            return denied(f"the read queue is full: at most {cap} paths are served per call")
        self.queued.append(path)
        return {"status": "queued", "path": path, "note": "served with the next call as requested_files"}


def list_sessions(toolbox: Toolbox, args: dict) -> dict:
    return toolbox.transcript.describe()


def read_transcript(toolbox: Toolbox, args: dict) -> dict:
    ref = text_arg(args, "session")
    offset = int_arg(args, "offset", default=0, minimum=0)
    limit = int_arg(args, "limit", default=toolbox.settings.transcript_page_messages, minimum=1, maximum=200)
    try:
        return toolbox.transcript.page(ref, offset, limit, toolbox.settings.transcript_page_bytes)
    except KeyError:
        return denied(f"unknown session {ref!r}") | {"sessions": toolbox.transcript.describe()}


def search_transcript(toolbox: Toolbox, args: dict) -> dict:
    query = text_arg(args, "query", required=True)
    ref = text_arg(args, "session")
    limit = int_arg(args, "limit", default=toolbox.settings.search_limit, minimum=1, maximum=200)
    assert query is not None
    try:
        return toolbox.transcript.search(
            query,
            ref,
            limit,
            ignore_case=bool_arg(args, "ignore_case", True),
            regex=bool_arg(args, "regex", False),
        )
    except KeyError:
        return denied(f"unknown session {ref!r}") | {"sessions": toolbox.transcript.describe()}


def find_files(toolbox: Toolbox, args: dict) -> dict:
    pattern = text_arg(args, "pattern", required=True)
    limit = int_arg(args, "limit", default=toolbox.settings.search_limit, minimum=1, maximum=500)
    assert pattern is not None
    if toolbox.index_status == "over_limit":
        return {
            "status": "unavailable",
            "reason": "the file index is over its entry limit, so only the paths in this call are readable",
        }
    ignore_case = bool_arg(args, "ignore_case", True)
    needle = pattern.lower() if ignore_case else pattern
    if WILDCARDS & set(pattern):
        matches = [path for path in toolbox.index if fnmatch.fnmatchcase(path.lower() if ignore_case else path, needle)]
    else:
        matches = [path for path in toolbox.index if needle in (path.lower() if ignore_case else path)]
    return {
        "status": "ok",
        "pattern": pattern,
        "total": len(matches),
        "returned": len(matches[:limit]),
        "files": matches[:limit],
        "more": len(matches) > limit,
    }


def read_file(toolbox: Toolbox, args: dict) -> dict:
    path = text_arg(args, "path", required=True)
    start = int_arg(args, "start_line", default=1, minimum=1)
    end = int_arg(args, "end_line", default=0, minimum=1)
    assert path is not None
    record = toolbox.views.get(path)
    if record is None:
        if path in toolbox.index_set:
            return toolbox.queue(path)
        return denied("the path is neither served in this call nor in the repository's file index")
    if record.get("status") != "ok":
        return record | {"path": path}
    raw_text = record.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    return {"path": path, "full_bytes": record.get("bytes"), **line_slice(text, start, end or None)}


def search_files(toolbox: Toolbox, args: dict) -> dict:
    pattern = text_arg(args, "pattern", required=True)
    limit = int_arg(args, "limit", default=toolbox.settings.search_limit, minimum=1, maximum=200)
    context = int_arg(args, "context", default=0, minimum=0, maximum=3)
    assert pattern is not None
    raw_paths = args.get("paths")
    if raw_paths is not None and not (isinstance(raw_paths, list) and all(isinstance(p, str) for p in raw_paths)):
        raise ToolArgumentError("paths must be a list of strings")
    paths = raw_paths if isinstance(raw_paths, list) else sorted(toolbox.views)
    flags = re.IGNORECASE if bool_arg(args, "ignore_case", True) else 0
    try:
        regex = re.compile(pattern if bool_arg(args, "regex", False) else re.escape(pattern), flags)
    except re.error as error:
        return denied(f"the pattern does not compile: {error}")
    matches, searched, unserved = [], 0, []
    for path in paths:
        record = toolbox.views.get(path)
        if record is None or record.get("status") != "ok" or not isinstance(record.get("text"), str):
            unserved.append(path)
            continue
        searched += 1
        lines = record["text"].splitlines()
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            entry = {"path": path, "line": index + 1, "text": bounded_text(line, 400)}
            if context:
                entry["before"] = [bounded_text(item, 400) for item in lines[max(index - context, 0) : index]]
                entry["after"] = [bounded_text(item, 400) for item in lines[index + 1 : index + 1 + context]]
            matches.append(entry)
            if len(matches) >= limit:
                return {
                    "status": "ok",
                    "files_searched": searched,
                    "unserved": unserved,
                    "matches": matches,
                    "more": True,
                }
    return {"status": "ok", "files_searched": searched, "unserved": unserved, "matches": matches, "more": False}


def read_memory(toolbox: Toolbox, args: dict) -> dict:
    path = text_arg(args, "path")
    listing = [
        {"path": key, "bytes": len(text.encode()), "owned": key in toolbox.owned}
        for key, text in sorted(toolbox.memory.items())
    ]
    if path is None:
        return {"status": "ok", "root": toolbox.memory_root(), "files": listing}
    if path not in toolbox.memory:
        return denied("the request carries no memory file at that path") | {"files": listing}
    start = int_arg(args, "start_line", default=1, minimum=1)
    end = int_arg(args, "end_line", default=0, minimum=1)
    return {"path": path, **line_slice(toolbox.memory[path], start, end or None)}


def stage_action(toolbox: Toolbox, args: dict) -> dict:
    op = text_arg(args, "op", required=True)
    path = text_arg(args, "path", required=True)
    assert op is not None and path is not None
    if op not in {"write", "delete"}:
        raise ToolArgumentError("op must be write or delete")
    if not path.startswith("/") or "\\" in path or "\0" in path or str(PurePosixPath(path)) != path:
        raise ToolArgumentError("path must be a canonical absolute POSIX path")
    root = toolbox.memory_root()
    if root and "{project}" not in path and not path.startswith(root + "/"):
        raise ToolArgumentError(f"path must be below the memory root {root}")
    content = args.get("content")
    if op == "write":
        if not isinstance(content, str):
            raise ToolArgumentError("a write needs content, a string")
        if "\0" in content:
            raise ToolArgumentError("content holds a NUL byte")
        size = utf8_bytes(content, "content")
        limit = toolbox.limits.get("max_file_bytes")
        if isinstance(limit, int) and not isinstance(limit, bool) and size > limit:
            raise ToolArgumentError(f"content is over the {limit}-byte file limit")
    elif content not in (None, ""):
        raise ToolArgumentError("a delete carries no content")
    touched = toolbox.limits.get("max_files_touched")
    if (
        path not in toolbox.staged
        and isinstance(touched, int)
        and not isinstance(touched, bool)
        and len(toolbox.staged) >= touched
    ):
        raise ToolArgumentError(f"this call may touch at most {touched} files")
    action = {"op": op, "path": path}
    if op == "write":
        assert isinstance(content, str)
        action["content"] = content
    toolbox.staged[path] = action
    return {"status": "staged", "path": path, "staged_actions": len(toolbox.staged)}


def drop_action(toolbox: Toolbox, args: dict) -> dict:
    path = text_arg(args, "path", required=True)
    assert path is not None
    removed = toolbox.staged.pop(path, None)
    return {"status": "removed" if removed else "absent", "path": path, "staged_actions": len(toolbox.staged)}


def list_staged_actions(toolbox: Toolbox, args: dict) -> dict:
    return {
        "status": "ok",
        "staged_actions": [
            {"op": action["op"], "path": action["path"], "bytes": len(action.get("content", "").encode())}
            for action in toolbox.staged.values()
        ],
    }


def set_strategy(toolbox: Toolbox, args: dict) -> dict:
    text = text_arg(args, "text", required=True)
    assert text is not None
    limit = toolbox.contract.get("strategy_max_bytes")
    if isinstance(limit, int) and not isinstance(limit, bool) and len(text.encode()) > limit:
        raise ToolArgumentError(f"the strategy is over its {limit}-byte limit")
    toolbox.strategy, toolbox.strategy_dirty = text, text != toolbox.strategy_original
    return {"status": "ok", "strategy_bytes": len(text.encode()), "changed": toolbox.strategy_dirty}


def set_notebook(toolbox: Toolbox, args: dict) -> dict:
    text = text_arg(args, "text", required=True)
    assert text is not None
    limit = toolbox.contract.get("notebook_max_bytes")
    if isinstance(limit, int) and not isinstance(limit, bool) and len(text.encode()) > limit:
        raise ToolArgumentError(f"the notebook is over its {limit}-byte limit")
    toolbox.notebook, toolbox.notebook_dirty = text, text != toolbox.notebook_original
    return {"status": "ok", "notebook_bytes": len(text.encode()), "changed": toolbox.notebook_dirty}


def finish(toolbox: Toolbox, args: dict) -> dict:
    note = text_arg(args, "note", limit=2_000)
    toolbox.finished, toolbox.finish_note = True, note or ""
    return {"status": "finished", "staged_actions": len(toolbox.staged), "queued_reads": len(toolbox.queued)}


HANDLERS = {
    "list_sessions": list_sessions,
    "read_transcript": read_transcript,
    "search_transcript": search_transcript,
    "find_files": find_files,
    "read_file": read_file,
    "search_files": search_files,
    "read_memory": read_memory,
    "stage_action": stage_action,
    "drop_action": drop_action,
    "list_staged_actions": list_staged_actions,
    "set_strategy": set_strategy,
    "set_notebook": set_notebook,
    "finish": finish,
}
