"""Files: the person's documents, with list and read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


class File(BaseModel):
    path: str
    content: str


class Files(BaseModel):
    files: list[File] = []

    def file(self, path: str) -> File | None:
        return next((f for f in self.files if f.path == path), None)


def _files(world: World) -> Files:
    return world.app("files")


class ListFilesArgs(BaseModel):
    directory: str = Field("", description="Directory prefix, for example 'contracts/'. Empty lists everything.")


def list_files(world: World, args: ListFilesArgs) -> list[str]:
    return sorted(f.path for f in _files(world).files if f.path.startswith(args.directory))


class ReadFileArgs(BaseModel):
    path: str


def read_file(world: World, args: ReadFileArgs) -> str:
    file = _files(world).file(args.path)
    if file is None:
        raise ToolError(f"No such file: {args.path}.")
    return file.content


APP = App(
    name="files",
    title="files",
    state=Files,
    keys={"files": "path"},
    tools=[
        Tool("list_files", "files", "List the paths of the user's files.", ListFilesArgs, list_files),
        Tool("read_file", "files", "Read a text file from the user's files.", ReadFileArgs, read_file),
    ],
)
