"""The writer's state for one chain: the private notebook and the record of every attempt.

The notebook is a two-step transaction. A candidate (answer, notebook, hook response) is staged under its transition
and attempt; the committed notebook advances only when the harness's receipt for that transition names exactly the
response the writer produced and ends as applied, no_change or rejected. Nothing is inferred from a successful provider
answer. A rejected candidate keeps its observations but is labelled not applied; a failed or timed-out step discards
its staged candidate. State lives in the chain's own `state/writer/` folder and never reads another chain's.

    state/writer/notebook.json            the committed notebook
    state/writer/NNN/aKK/                 one attempt at transition NNN: input, manifest, view, transport records,
                                          candidates.jsonl, candidate.json (staged), attempt.json (terminal)
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sereno.context_eval.config import fingerprint
from sereno.context_eval.evaluator import read_object
from sereno.writer.errors import InputError
from sereno.writer.request import Request

APPLIED_STATUSES = ("applied", "no_change", "rejected")
FAILED_STATUSES = ("writer_failed", "timeout")


def now() -> str:
    return datetime.now(UTC).isoformat()


def dump(value) -> bytes:
    """JSON as UTF-8 bytes, locale-independent. A lone surrogate in a provider's text, which JSON allows, is written as
    an escape instead of failing a write that follows a paid call."""
    return (json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8", "backslashreplace")


def write_bytes_atomic(path: Path, raw: bytes) -> None:
    partial = path.with_name(f".{path.name}.partial")
    partial.write_bytes(raw)
    partial.replace(path)


def write_atomic(path: Path, value) -> None:
    write_bytes_atomic(path, dump(value))


@dataclass
class AttemptDir:
    path: Path

    def write(self, name: str, value) -> None:
        write_atomic(self.path / name, value)

    def append(self, name: str, value) -> None:
        line = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8", "backslashreplace")
        with (self.path / name).open("ab") as stream:
            stream.write(line)

    def read(self, name: str) -> dict | None:
        return read_object(self.path / name)


class Chain:
    """The state of one chain at one transition."""

    def __init__(self, req: Request):
        self.req = req
        self.root = req.state_dir / "writer"
        self.folder = self.root / f"{req.transition:03d}"
        # A hash of the harness's receipt for the transition before this one, set by `reconcile`.
        self.receipt_sha256: str | None = None

    def attempts(self, transition: int | None = None) -> list[Path]:
        folder = self.folder if transition is None else self.root / f"{transition:03d}"
        return sorted(p for p in folder.glob("a*") if p.is_dir()) if folder.is_dir() else []

    def mark_killed(self) -> list[str]:
        """An attempt with no terminal record was cut short: record that, so it is never counted as a completed call.
        Its transport records stay as they are."""
        marked = []
        for folder in sorted(self.root.glob("[0-9][0-9][0-9]/a*")) if self.root.is_dir() else []:
            if folder.is_dir() and not (folder / "attempt.json").exists():
                write_atomic(
                    folder / "attempt.json",
                    {"outcome": "killed_incomplete", "detected_at": now(), "note": "no terminal record was written"},
                )
                marked.append(str(folder.relative_to(self.root)))
        return marked

    def committed(self) -> dict | None:
        record = read_object(self.root / "notebook.json")
        if self.root.joinpath("notebook.json").exists() and record is None:
            raise InputError("state/writer/notebook.json cannot be read", "bad_state")
        return record

    def reconcile(self) -> tuple[str, dict | None]:
        """Commit or discard the previous transition's staged candidate by the harness's receipt, then return the
        notebook this transition starts from and what the previous write came to, as the writer may be told."""
        previous = self.req.transition - 1
        committed = self.committed()
        text = committed["notebook"] if committed else ""
        if previous < 1:
            return text, None
        if committed and committed["transition"] > previous:
            raise InputError("the committed notebook is from a later transition than this one", "bad_state")
        if committed and committed["transition"] == previous:
            self.receipt_sha256 = committed["receipt_sha256"]
            return text, committed["previous_write"]
        folder = self.req.arm_dir / "transitions" / f"{previous:03d}"
        receipt = read_object(folder / "transition.json")
        if receipt is None or not isinstance(receipt.get("status"), str):
            raise InputError(f"transition {previous:03d} has no readable receipt", "bad_state")
        status = receipt["status"]
        self.receipt_sha256 = fingerprint(receipt)
        if status not in APPLIED_STATUSES:
            self.log({"event": "discarded", "transition": previous, "status": status})
            return text, None
        staged = self.staged(previous, receipt.get("response_sha256"))
        if staged is None:
            raise InputError(
                f"transition {previous:03d} ended {status} on a response the writer has no staged candidate for",
                "bad_state",
            )
        label = {"before_session": previous, "status": status, "applied": status == "applied"}
        write_atomic(
            self.root / "notebook.json",
            {
                "transition": previous,
                "notebook": staged["notebook"],
                "previous_write": label,
                "receipt_sha256": self.receipt_sha256,
                "candidate": {key: staged[key] for key in ("attempt", "input_sha256", "response_sha256")},
                "committed_at": now(),
            },
        )
        self.log({"event": "committed", "transition": previous, "status": status, "attempt": staged["attempt"]})
        return staged["notebook"], label

    def staged(self, transition: int, response_sha256: object) -> dict | None:
        """The staged candidate of `transition` whose response is the one the receipt names."""
        for folder in reversed(self.attempts(transition)):
            candidate = read_object(folder / "candidate.json")
            if candidate and candidate.get("response_sha256") == response_sha256 and response_sha256:
                return candidate
        return None

    def consumed_as_failure(self, response_sha256: str) -> bool:
        """Whether the hook already ended a step on this exact response as writer_failed or timeout: such a response
        is not reused, since replaying it would loop."""
        for kind in ("superseded", "stopped"):
            for record in (self.req.arm_dir / "transitions" / kind).glob(
                f"*/{self.req.transition:03d}/transition.json"
            ):
                data = read_object(record)
                if data and data.get("response_sha256") == response_sha256 and data.get("status") in FAILED_STATUSES:
                    return True
        return False

    def reusable(self, input_sha256: str) -> tuple[AttemptDir, dict] | None:
        """A complete candidate recorded for exactly this input, or None. On a resume it is delivered again without a
        new provider call, so a transition is never paid for twice."""
        for folder in reversed(self.attempts()):
            candidate = read_object(folder / "candidate.json")
            if not candidate or candidate.get("input_sha256") != input_sha256:
                continue
            if not isinstance(candidate.get("response"), dict) or not candidate.get("response_sha256"):
                continue
            if self.consumed_as_failure(candidate["response_sha256"]):
                continue
            return AttemptDir(folder), candidate
        return None

    def new_attempt(self) -> AttemptDir:
        number = len(self.attempts()) + 1
        folder = self.folder / f"a{number:02d}"
        folder.mkdir(parents=True)
        return AttemptDir(folder)

    def log(self, event: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = (json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n").encode("utf-8", "backslashreplace")
        with (self.root / "log.jsonl").open("ab") as stream:
            stream.write(line)
