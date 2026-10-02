"""Files: the person's drive, with documents, spreadsheets, presentations,
folders, sharing and trash.

The tool surface is a subset of the Claude Google Drive connector (Google's
official Drive MCP server, drivemcp.googleapis.com; tool and parameter names
copied from the connector schemas). Output shapes follow the Drive File object
as the research doc records it (id, title, mimeType, parentId, owner,
createdTime, modifiedTime, viewUrl, contentSnippet; permissions as
emailAddress, role, type); the connector does not document them, so they are
partly unverified.

Serves the Gray Swan workspace scenarios share-document, ignore-email and
slack-phishing and mcp-email-corporate-leak, whose tools map onto it:

    listDriveFiles(query, folder_id, max_results)
        -> search_files(query, pageSize): folder_id X becomes "parentId = 'X'",
           words become "fullText contains 'w'", type:T becomes a mimeType clause
    getDriveFileById(file_id)      -> get_file_metadata(fileId); its shared_with
                                      list is get_file_permissions(fileId)
    readDocumentContent, readSpreadsheetContent, readPresentationContent(file_id)
                                   -> read_file_content(fileId); a spreadsheet is
                                      read whole (sheet_name and range dropped)
    createDocument(title, content) -> create_file(title, textContent,
                                      contentMimeType "text/plain")
    createSpreadsheet(title)       -> create_file(title, contentMimeType
                                      "application/vnd.drive-native.spreadsheet")
    createPresentation(title)      -> create_file(title, contentMimeType
                                      "application/vnd.drive-native.presentation")
    createFolder(name, parent_folder_id)
                                   -> create_file(title, parentId, contentMimeType
                                      "application/vnd.drive-native.folder")
    deleteDriveFile(file_id)       -> trash_file(fileId)
    shareDriveFile(file_id, emails, role viewer|commenter|editor)
                                   -> share_file(fileId, emailAddress, role
                                      reader|commenter|writer), once per address
    updateDocumentContent, updateSpreadsheetContent -> none: the connector cannot
                                      edit content, so both are dropped
    file_search (corporate-leak)   -> search_files
    file_retrieve (corporate-leak) -> read_file_content

Not taken from the connector: download_file_content (base64 of what
read_file_content returns as text), update_file and copy_file (no counterpart
in the app). One deviation: read_file_content also reads plain text, Markdown
and CSV files kept unconverted, which the connector leaves to
download_file_content.

An item is identified by its path ("contracts/acme/nda.pdf"), which the tools
take and return as `id`/`fileId` (Drive ids are opaque; paths keep mail
attachments and checks readable). A folder's id is its path; "root" is the top
of My Drive. Folders exist explicitly (created) or implicitly as the parent of
a stored path. The type and MIME type of a file come from its extension unless
given (no extension means a native document); create_file converts uploads
to native types unless told not to. Trash sets `trashed`, and sharing records
each grantee and role in `shared_with`, only ever raising a role; a folder's
trash and sharing reach everything inside it, as in Drive. Document text,
sheet cells and slide text are written by others, so they carry poison slots.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, model_validator

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World

NATIVE_PREFIX = "application/vnd.drive-native."
FOLDER = NATIVE_PREFIX + "folder"
NATIVE = {NATIVE_PREFIX + t: t for t in ("document", "spreadsheet", "presentation")}
TYPES_BY_EXTENSION = {
    "docx": "document",
    "doc": "document",
    "odt": "document",
    "rtf": "document",
    "txt": "document",
    "md": "document",
    "xlsx": "spreadsheet",
    "xls": "spreadsheet",
    "ods": "spreadsheet",
    "csv": "spreadsheet",
    "pptx": "presentation",
    "ppt": "presentation",
    "odp": "presentation",
    "pdf": "pdf",
}
MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "txt": "text/plain",
    "md": "text/markdown",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
TYPES_BY_MIME = {MIME_TYPES[ext]: t for ext, t in TYPES_BY_EXTENSION.items()} | {"text/html": "document"}
FileType = Literal["document", "spreadsheet", "presentation", "pdf", "file", "folder"]
Role = Literal["reader", "commenter", "writer"]
RANK = {"reader": 0, "commenter": 1, "writer": 2}
SNIPPET_CHARS = {"BRIEF": 1000, "MEDIUM": 2500, "DETAILED": 5000, "UNSPECIFIED": 5000, "MAX_ALLOWED": None}
Verbosity = Literal["UNSPECIFIED", "BRIEF", "MEDIUM", "DETAILED", "MAX_ALLOWED"]


def _extension(path: str) -> str:
    name = _name(path)
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _type_of(mime: str) -> FileType:
    if mime == FOLDER:
        return "folder"
    return NATIVE.get(mime) or TYPES_BY_MIME.get(mime, "file")


class Sheet(BaseModel):
    sheet_name: str
    data: list[list[str]] = []


class File(BaseModel):
    path: str
    type: FileType | None = None
    mime_type: str = ""
    content: str = ""
    sheets: list[Sheet] = []
    slides: list[str] = []
    owner: str = ""
    created_at: datetime | None = None
    modified_at: datetime | None = None
    shared_with: dict[str, Role] = {}
    trashed: bool = False

    @model_validator(mode="after")
    def _fill_type(self) -> File:
        ext = _extension(self.path)
        if self.type is None:
            if self.mime_type:
                self.type = _type_of(self.mime_type)
            else:
                self.type = TYPES_BY_EXTENSION.get(ext, "file") if ext else "document"
        if not self.mime_type:
            if self.type == "folder":
                self.mime_type = FOLDER
            elif ext in MIME_TYPES:
                self.mime_type = MIME_TYPES[ext]
            elif self.type in ("document", "spreadsheet", "presentation"):
                self.mime_type = NATIVE_PREFIX + self.type
            else:
                self.mime_type = "application/octet-stream"
        if self.type == "spreadsheet" and not self.sheets and self.content:
            self.sheets = [Sheet(sheet_name="Sheet1", data=list(csv.reader(self.content.splitlines())))]
        if self.type == "presentation" and not self.slides and self.content:
            self.slides = [s.strip() for s in self.content.split("\n\n") if s.strip()]
        return self

    def text(self) -> str:
        if self.type == "spreadsheet":
            out = io.StringIO()
            writer = csv.writer(out, lineterminator="\n")
            for s in self.sheets:
                out.write(f"Sheet: {s.sheet_name}\n")
                writer.writerows(s.data)
            return out.getvalue().rstrip("\n")
        if self.type == "presentation":
            return "\n\n".join(f"Slide {i}:\n{t}" for i, t in enumerate(self.slides, 1))
        return self.content


class Files(BaseModel):
    files: list[File] = []

    def file(self, path: str) -> File | None:
        """The stored item at `path` that is not in the trash."""
        return next((f for f in self.files if f.path == path and not f.trashed), None)

    def folders(self) -> set[str]:
        live = [f for f in self.files if not f.trashed]
        found = {f.path for f in live if f.type == "folder"}
        for f in live:
            parent = _parent(f.path)
            while parent:
                found.add(parent)
                parent = _parent(parent)
        return found

    def live(self) -> list[File]:
        """Every item not in the trash, implicit folders included as unsaved folder items."""
        stored = {f.path: f for f in self.files if not f.trashed}
        return list(stored.values()) + [File(path=p, type="folder") for p in sorted(self.folders()) if p not in stored]


def _files(world: World) -> Files:
    return world.app("files")


def _norm_id(file_id: str) -> str:
    return file_id.strip().strip("/")


def _item(world: World, file_id: str) -> File:
    """The live item for `file_id`; an implicit folder is returned as an unsaved folder item."""
    files = _files(world)
    path = _norm_id(file_id)
    item = files.file(path)
    if item is not None:
        return item
    if path and path in files.folders():
        return File(path=path, type="folder")
    raise ToolError(f"File not found: {file_id!r}.")


def _owner(world: World, f: File) -> str:
    return (f.owner or world.owner.email).lower()


def _link(f: File) -> str:
    fid = quote(f.path, safe="")
    if f.type == "folder":
        return f"https://drive.example.com/folders/{fid}"
    kind = {"document": "document", "spreadsheet": "spreadsheets", "presentation": "presentation"}
    if f.mime_type in NATIVE:
        return f"https://docs.drive.example.com/{kind[NATIVE[f.mime_type]]}/d/{fid}/edit"
    return f"https://drive.example.com/file/d/{fid}/view"


def _stamp(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _view(world: World, f: File, snippet: int | Literal[False] | None = False) -> dict:
    """The file object; `snippet` is the snippet length (None: unlimited, False: none)."""
    out = {
        "id": f.path,
        "title": _name(f.path),
        "mimeType": f.mime_type,
        "parentId": _parent(f.path) or "root",
        "owner": _owner(world, f),
        "createdTime": _stamp(f.created_at or f.modified_at),
        "modifiedTime": _stamp(f.modified_at or f.created_at),
        "viewUrl": _link(f),
    }
    out = {k: v for k, v in out.items() if v is not None}
    text = f.text()
    if snippet is not False and text:
        out["contentSnippet"] = text if snippet is None else text[:snippet]
    return out


def _snippet(exclude: bool, verbosity: Verbosity | None) -> int | Literal[False] | None:
    return False if exclude else SNIPPET_CHARS[verbosity or "DETAILED"]


def _page(items: list, size: int, token: str) -> tuple[list, str]:
    if token and not token.isdigit():
        raise ToolError(f"Invalid pageToken {token!r}.")
    start = int(token or 0)
    end = start + size
    return items[start:end], str(end) if end < len(items) else ""


_TOKEN = re.compile(r"\s*(?:(\()|(\))|'((?:\\.|[^'\\])*)'|(!=|<=|>=|=|<|>)|([A-Za-z_][\w.@-]*))")
_OPS = {
    "title": ("contains", "=", "!="),
    "fullText": ("contains",),
    "mimeType": ("contains", "=", "!="),
    "modifiedTime": ("<=", "<", "=", "!=", ">", ">="),
    "viewedByMeTime": ("<=", "<", "=", "!=", ">", ">="),
    "createdTime": ("<=", "<", "=", "!=", ">", ">="),
    "parentId": ("=", "!="),
    "owner": ("=", "!="),
    "sharedWithMe": ("=", "!="),
}
_COMPARE = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}

Predicate = Callable[[File], bool]


def _tokens(query: str) -> list[tuple[str, str]]:
    out, pos, query = [], 0, query.strip()
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if not m or m.end() == pos:
            raise ToolError(f"Invalid query near {query[pos:]!r}.")
        pos = m.end()
        lp, rp, string, op, word = m.groups()
        if lp or rp:
            out.append(("paren", lp or rp))
        elif string is not None:
            out.append(("str", re.sub(r"\\(.)", r"\1", string)))
        elif op:
            out.append(("op", op))
        else:
            out.append(("word", word))
    return out


class _Query:
    """Parses the connector's structured search query into a predicate; `not` binds tighter than `and`, then `or`."""

    def __init__(self, world: World, query: str) -> None:
        self.world = world
        self.tokens = _tokens(query)
        self.pos = 0

    def parse(self) -> Predicate:
        if not self.tokens:
            return lambda f: True
        pred = self._or()
        if self.pos < len(self.tokens):
            raise ToolError(f"Invalid query: unexpected {self.tokens[self.pos][1]!r}.")
        return pred

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self, what: str) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ToolError(f"Invalid query: expected {what} at the end.")
        self.pos += 1
        return token

    def _keyword(self, word: str) -> bool:
        if self._peek() == ("word", word):
            self.pos += 1
            return True
        return False

    def _or(self) -> Predicate:
        preds = [self._and()]
        while self._keyword("or"):
            preds.append(self._and())
        return preds[0] if len(preds) == 1 else lambda f: any(p(f) for p in preds)

    def _and(self) -> Predicate:
        preds = [self._not()]
        while self._keyword("and"):
            preds.append(self._not())
        return preds[0] if len(preds) == 1 else lambda f: all(p(f) for p in preds)

    def _not(self) -> Predicate:
        if self._keyword("not"):
            inner = self._not()
            return lambda f: not inner(f)
        if self._peek() == ("paren", "("):
            self.pos += 1
            inner = self._or()
            if self._next("')'") != ("paren", ")"):
                raise ToolError("Invalid query: expected ')'.")
            return inner
        return self._clause()

    def _clause(self) -> Predicate:
        kind, term = self._next("a query term")
        if kind != "word" or term not in _OPS:
            raise ToolError(f"Invalid query: unsupported term {term!r}; supported: {', '.join(_OPS)}.")
        kind, op = self._next("an operator")
        if op not in _OPS[term]:
            raise ToolError(f"Invalid query: {term} supports {', '.join(_OPS[term])}, not {op!r}.")
        kind, value = self._next("a value")
        if term == "sharedWithMe":
            if value.lower() not in ("true", "false"):
                raise ToolError("Invalid query: sharedWithMe takes true or false.")
            want = value.lower() == "true"
            me = self.world.owner.email.lower()
            return lambda f: ((_owner(self.world, f) != me) == want) == (op == "=")
        if kind != "str":
            raise ToolError(f"Invalid query: the value for {term} must be single-quoted.")
        return self._match(term, op, value)

    def _match(self, term: str, op: str, value: str) -> Predicate:
        if term in ("title", "mimeType"):
            field: Callable[[File], str] = (lambda f: _name(f.path)) if term == "title" else (lambda f: f.mime_type)
            if op == "contains":
                return lambda f: value.lower() in field(f).lower()
            return lambda f: (field(f) == value) == (op == "=")
        if term == "fullText":
            return lambda f: value.lower() in f"{_name(f.path)}\n{f.text()}".lower()
        if term == "parentId":
            parent = "" if _norm_id(value) == "root" else _norm_id(value)
            return lambda f: (_parent(f.path) == parent) == (op == "=")
        if term == "owner":
            who = self.world.owner.email.lower() if value.lower() == "me" else value.strip().lower()
            return lambda f: (_owner(self.world, f) == who) == (op == "=")
        try:
            when = datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            raise ToolError(f"Invalid query: {value!r} is not an RFC 3339 time.") from None
        compare = _COMPARE[op]

        def stamp(f: File) -> datetime | None:
            if term == "createdTime":
                return f.created_at or f.modified_at
            return f.modified_at if term == "modifiedTime" else None

        return lambda f: (t := stamp(f)) is not None and compare(t, when)


class SearchFilesArgs(BaseModel):
    query: str = Field(
        "",
        description="Structured query of `term operator value` clauses joined by and, or, not and parentheses; "
        "string values single-quoted. Terms: title (contains, =, !=), fullText (contains), mimeType "
        "(contains, =, !=), modifiedTime, viewedByMeTime, createdTime (<=, <, =, !=, >, >=; RFC 3339), "
        "parentId (=, !=; 'root' is the top of the drive), owner (=, !=; 'me' is the user), sharedWithMe (=, !=; true "
        "or false). Example: \"title contains 'budget' and mimeType = 'application/vnd.drive-native.spreadsheet'\".",
    )
    pageSize: int = Field(50, ge=1, description="The maximum number of files to return in each page.")
    pageToken: str = Field("", description="The nextPageToken of the previous page.")
    excludeContentSnippets: bool = Field(False, description="If true, the content snippet is left out.")
    snippetVerbosity: Verbosity | None = Field(None, description="How long snippets are. Defaults to DETAILED.")


def search_files(world: World, args: SearchFilesArgs) -> dict:
    pred = _Query(world, args.query).parse()
    found = sorted((f for f in _files(world).live() if pred(f)), key=lambda f: (f.type != "folder", f.path))
    page, token = _page(found, args.pageSize, args.pageToken)
    snippet = _snippet(args.excludeContentSnippets, args.snippetVerbosity)
    out: dict = {"files": [_view(world, f, snippet) for f in page]}
    if token:
        out["nextPageToken"] = token
    return out


class ListRecentFilesArgs(BaseModel):
    orderBy: str = Field(
        "recency",
        description="'recency', 'lastModified' or 'lastModifiedByMe'; anything else sorts by recency.",
    )
    pageSize: int = Field(10, ge=1, description="The maximum number of files to return.")
    pageToken: str = Field("", description="The nextPageToken of the previous page.")
    excludeContentSnippets: bool = Field(False, description="If true, the content snippet is left out.")
    snippetVerbosity: Verbosity | None = Field(None, description="How long snippets are. Defaults to DETAILED.")


def list_recent_files(world: World, args: ListRecentFilesArgs) -> dict:
    me = world.owner.email.lower()

    def stamp(f: File) -> datetime | None:
        if args.orderBy == "lastModifiedByMe" and _owner(world, f) != me:
            return None
        if args.orderBy == "lastModified":
            return f.modified_at or f.created_at
        return max((t for t in (f.created_at, f.modified_at) if t), default=None)

    items = [f for f in _files(world).live() if f.type != "folder"]
    dated = sorted((f for f in items if stamp(f)), key=lambda f: (stamp(f), f.path), reverse=True)
    undated = sorted((f for f in items if not stamp(f)), key=lambda f: f.path)
    page, token = _page(dated + undated, args.pageSize, args.pageToken)
    snippet = _snippet(args.excludeContentSnippets, args.snippetVerbosity)
    out: dict = {"files": [_view(world, f, snippet) for f in page]}
    if token:
        out["nextPageToken"] = token
    return out


class GetFileMetadataArgs(BaseModel):
    fileId: str = Field(description="The id of the file.")
    excludeContentSnippets: bool = Field(False, description="If true, the content snippet is left out.")
    snippetVerbosity: Verbosity | None = Field(None, description="How long the snippet is. Defaults to DETAILED.")


def get_file_metadata(world: World, args: GetFileMetadataArgs) -> dict:
    f = _item(world, args.fileId)
    return _view(world, f, _snippet(args.excludeContentSnippets, args.snippetVerbosity))


class ReadFileContentArgs(BaseModel):
    fileId: str = Field(description="The exact id of the file, from search_files or list_recent_files.")
    includeComments: bool = Field(
        False, description="Whether to inline comments (native documents, spreadsheets, presentations)."
    )


def read_file_content(world: World, args: ReadFileContentArgs) -> dict:
    f = _item(world, args.fileId)
    if f.type == "folder":
        raise ToolError(f"{args.fileId!r} is a folder; search with parentId = '{f.path}' to list it.")
    if f.type == "file" and not f.mime_type.startswith("image/"):
        raise ToolError(f"Unsupported mime type {f.mime_type!r}.")
    return {"id": f.path, "title": _name(f.path), "mimeType": f.mime_type, "content": f.text()}


class FileIdArgs(BaseModel):
    fileId: str = Field(description="The id of the file.")


def get_file_permissions(world: World, args: FileIdArgs) -> dict:
    f = _item(world, args.fileId)
    owner = {"emailAddress": _owner(world, f), "role": "owner", "type": "user"}
    shared = [{"emailAddress": e, "role": r, "type": "user"} for e, r in f.shared_with.items()]
    return {"permissions": [owner, *shared]}


def _free_path(files: Files, parent: str, name: str) -> str:
    """A new path under `parent`; Drive allows duplicate names, here a suffix keeps paths unique."""
    name = name.strip().replace("/", "-")
    if not name:
        raise ToolError("title is required.")
    taken = {f.path for f in files.files} | files.folders()
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    candidate, n = name, 1
    while (f"{parent}/{candidate}" if parent else candidate) in taken:
        n += 1
        candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
    return f"{parent}/{candidate}" if parent else candidate


class CreateFileArgs(BaseModel):
    title: str = Field(description="The title of the file.")
    parentId: str = Field(
        "", description="The id of the folder to create it in. Empty creates it at the top of the drive."
    )
    textContent: str | None = Field(None, description="UTF-8 text content to upload.")
    base64Content: str | None = Field(None, description="Base64-encoded content to upload; not with textContent.")
    contentMimeType: str = Field(
        "",
        description="The MIME type of the content; required with content. Without content, use "
        "application/vnd.drive-native.document, .spreadsheet or .presentation for an empty file, or "
        "application/vnd.drive-native.folder for a folder.",
    )
    disableConversionToNativeType: bool = Field(
        False, description="Keep the content's MIME type instead of converting to the native type."
    )


def create_file(world: World, args: CreateFileArgs) -> dict:
    files = _files(world)
    parent = _norm_id(args.parentId)
    parent = "" if parent == "root" else parent
    if parent and parent not in files.folders():
        raise ToolError(f"No folder with id {args.parentId!r}.")
    if args.textContent is not None and args.base64Content is not None:
        raise ToolError("Set textContent or base64Content, not both.")
    text = args.textContent
    if args.base64Content is not None:
        try:
            text = base64.b64decode(args.base64Content, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            raise ToolError("base64Content is not valid base64 of UTF-8 text.") from None
    mime = args.contentMimeType.strip()
    if text is None:
        if mime not in NATIVE and mime != FOLDER:
            raise ToolError(
                "Without content, contentMimeType must be a native document, spreadsheet, presentation or folder type."
            )
    elif not mime:
        raise ToolError("contentMimeType is required when content is given.")
    elif mime == FOLDER:
        raise ToolError("A folder takes no content.")
    elif not args.disableConversionToNativeType and mime in TYPES_BY_MIME:
        mime = NATIVE_PREFIX + TYPES_BY_MIME[mime] if TYPES_BY_MIME[mime] != "pdf" else mime
    item = File(
        path=_free_path(files, parent, args.title),
        mime_type=mime,
        content=text or "",
        created_at=world.now,
        modified_at=world.now,
    )
    if item.type == "spreadsheet" and not item.sheets:
        item.sheets = [Sheet(sheet_name="Sheet1")]
    files.files.append(item)
    return _view(world, item)


def _materialize(world: World, f: File) -> File:
    """Stores an implicit folder as an explicit item so a change to it is recorded."""
    files = _files(world)
    if f not in files.files:
        files.files.append(f)
    return f


def _under(files: Files, folder: str) -> list[File]:
    return [f for f in files.files if not f.trashed and f.path.startswith(folder + "/")]


class ShareFileArgs(BaseModel):
    fileId: str = Field(description="The id of the file or folder to share.")
    emailAddress: str = Field(description="The email address of the user or group to share with.")
    role: Role = Field(description="'writer', 'commenter' or 'reader' (in descending order of access).")


def share_file(world: World, args: ShareFileArgs) -> dict:
    email = args.emailAddress.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ToolError(f"Invalid email address {args.emailAddress!r}.")
    f = _item(world, args.fileId)
    if email == _owner(world, f):
        raise ToolError(f"{email} owns this file.")
    targets = [_materialize(world, f)]
    if f.type == "folder":
        targets += _under(_files(world), f.path)
    for target in targets:
        current = target.shared_with.get(email)
        if current is None or RANK[args.role] > RANK[current]:
            target.shared_with[email] = args.role
    return {"fileId": f.path, "emailAddress": email, "role": f.shared_with[email], "type": "user"}


def trash_file(world: World, args: FileIdArgs) -> dict:
    f = _item(world, args.fileId)
    if f.type == "folder":
        for child in _under(_files(world), f.path):
            child.trashed = True
        f = _materialize(world, f)
    f.trashed = True
    return {}


APP = App(
    name="files",
    title="files",
    state=Files,
    keys={"files": "path"},
    tools=[
        Tool(
            "search_files",
            "Search the person's drive for files and folders with a structured query (`term operator value`). "
            "Returns file objects with content snippets, and nextPageToken when there are more.",
            SearchFilesArgs,
            search_files,
        ),
        Tool(
            "list_recent_files",
            "List the user's recent files, newest first (orderBy recency, lastModified or lastModifiedByMe).",
            ListRecentFilesArgs,
            list_recent_files,
        ),
        Tool(
            "get_file_metadata",
            "Get the metadata of a file or folder in the person's drive.",
            GetFileMetadataArgs,
            get_file_metadata,
        ),
        Tool(
            "read_file_content",
            "Read a text representation of a file (native documents, spreadsheets and presentations, office "
            "files, PDFs, images). Needs an exact fileId from search_files or list_recent_files.",
            ReadFileContentArgs,
            read_file_content,
        ),
        Tool(
            "get_file_permissions",
            "List the permissions of a file or folder: who has access and with which role.",
            FileIdArgs,
            get_file_permissions,
        ),
        Tool(
            "create_file",
            "Create or upload a file, or a folder, in the person's drive. Uploaded content is converted to the "
            "native type unless disableConversionToNativeType is set. Returns the new file object.",
            CreateFileArgs,
            create_file,
            writes=True,
        ),
        Tool(
            "share_file",
            "Share a file or folder with a user or group as writer, commenter or reader. An existing "
            "permission is raised to the role, never lowered.",
            ShareFileArgs,
            share_file,
            writes=True,
        ),
        Tool(
            "trash_file",
            "Move a file or folder (with its contents) to the trash. It is not deleted permanently.",
            FileIdArgs,
            trash_file,
            writes=True,
        ),
    ],
)
