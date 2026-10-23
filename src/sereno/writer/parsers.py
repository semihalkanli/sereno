"""The memory route's answer: parsed strictly, checked against the harness's own rules, mapped to the hook contract.

The provider's answer is a writer envelope (status, actions, notebook, explanation), not the hook's response. A problem
in it is named by a short neutral code. Structural codes mean the answer cannot be mapped onto the hook contract; the
harness codes mean the actions are well formed but break a content, path, ownership or size rule that the harness
enforces itself, which the harness records as `rejected` when it receives them. Raw validator output stays on the host
and never goes back to the provider.
"""

import json
import re
from dataclasses import dataclass, field

from sereno.context_eval.adaptive import ResponseError, parse_actions, validate_actions
from sereno.context_eval.schema import AdaptiveLimits

SCHEMA_VERSION = "1.0"
STATUSES = ("ready", "no_change", "unable")
KEYS = frozenset({"status", "actions", "notebook", "explanation"})
EXPLANATION_CHARS = 4000

CODE_TEXT = {
    "invalid_json": "the answer is not one valid json object",
    "wrong_keys": "the object must have exactly the keys status, actions, notebook and explanation",
    "bad_status": "status must be ready, no_change or unable",
    "bad_actions": "actions must be a list of write actions (op, path, content) and delete actions (op, path) with no "
    "other field",
    "status_actions": "ready needs at least one action; no_change and unable need an empty actions list",
    "bad_notebook": "notebook must be a string",
    "notebook_too_large": "notebook is over its size limit",
    "bad_explanation": "explanation must be a short string",
    "no_effective_change": "ready needs at least one action that changes the current files",
    "bad_path": "a path is not an allowed file path below the memory root",
    "duplicate_path": "a path appears in more than one action",
    "not_owned": "an action deletes a file that is not yours to delete",
    "original_lines": "an action replaces a file that is not yours to rewrite; it may only gain lines",
    "file_too_large": "a written file is over the size limit",
    "too_many_files": "the answer touches more files than allowed",
    "path_conflict": "a path is both a file and a directory",
    "content_constraint": "the content of a changed file does not meet the memory folder's content constraints",
    "invalid_content": "a written file is not valid UTF-8 text",
    "memory_limit": "the memory would be over its size limits",
    "other_constraint": "an action breaks a rule of the memory folder",
}
HARNESS_CODES = frozenset(
    {
        "bad_path",
        "duplicate_path",
        "not_owned",
        "original_lines",
        "file_too_large",
        "too_many_files",
        "path_conflict",
        "content_constraint",
        "invalid_content",
        "memory_limit",
        "other_constraint",
    }
)
# What the harness's violation text means, first match wins. Pinned by a test per rule, so a reworded harness message
# fails the suite instead of turning into a vague code.
VIOLATION_CODES = (
    (re.compile(r"already has an action"), "duplicate_path"),
    (re.compile(r"so it cannot be deleted"), "not_owned"),
    (re.compile(r"so a write may only add lines"), "original_lines"),
    (re.compile(r"matches forbidden pattern"), "content_constraint"),
    (re.compile(r"content is not valid UTF-8|content holds a NUL byte"), "invalid_content"),
    (re.compile(r"is over \d+ bytes"), "file_too_large"),
    (re.compile(r"files touched, over the limit|actions, over the limit"), "too_many_files"),
    (re.compile(r"is a file, not a directory|is a directory with files in it"), "path_conflict"),
    (re.compile(r"over the memory limits"), "memory_limit"),
    (re.compile(r"canonical file path|usable file name|out of reach"), "bad_path"),
)


@dataclass(frozen=True)
class Answer:
    status: str
    actions: list[dict]
    notebook: str
    explanation: str

    def record(self) -> dict:
        return {
            "status": self.status,
            "actions": self.actions,
            "notebook": self.notebook,
            "explanation": self.explanation,
        }


@dataclass
class Checked:
    """`codes` hold every problem found, in order and without repeats; `violations` is the harness's own text, which
    stays on the host."""

    codes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    effective: bool = False

    @property
    def structural(self) -> list[str]:
        return [code for code in self.codes if code not in HARNESS_CODES]

    @property
    def usable(self) -> bool:
        return not self.codes

    @property
    def deliverable(self) -> bool:
        """Whether the answer can go to the hook: clean, or only breaking rules the harness enforces itself."""
        return not self.structural


def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def action_shape_ok(action: object) -> bool:
    if not isinstance(action, dict) or action.get("op") not in ("write", "delete"):
        return False
    wanted = {"op", "path", "content"} if action["op"] == "write" else {"op", "path"}
    if set(action) != wanted or not isinstance(action["path"], str) or not action["path"]:
        return False
    return action["op"] == "delete" or isinstance(action["content"], str)


def parse_answer(text: str, notebook_max_bytes: int) -> tuple[Answer | None, list[str]]:
    """The writer envelope in `text`, or the structural codes that stop it from being one."""
    try:
        data = json.loads(text, object_pairs_hook=no_duplicate_keys)
    except (ValueError, RecursionError):
        return None, ["invalid_json"]
    if not isinstance(data, dict):
        return None, ["invalid_json"]
    if set(data) != KEYS:
        return None, ["wrong_keys"]
    codes = []
    status, actions, notebook, explanation = (data[key] for key in ("status", "actions", "notebook", "explanation"))
    if status not in STATUSES:
        codes.append("bad_status")
    if not (isinstance(actions, list) and all(action_shape_ok(action) for action in actions)):
        codes.append("bad_actions")
    elif status in STATUSES and bool(actions) != (status == "ready"):
        codes.append("status_actions")
    if not isinstance(notebook, str):
        codes.append("bad_notebook")
    else:
        try:
            if len(notebook.encode("utf-8")) > notebook_max_bytes:
                codes.append("notebook_too_large")
        except UnicodeEncodeError:
            codes.append("bad_notebook")
    if not isinstance(explanation, str) or len(explanation) > EXPLANATION_CHARS:
        codes.append("bad_explanation")
    if codes:
        return None, codes
    return Answer(status, actions, notebook, explanation), []


def violation_code(text: str) -> str:
    return next((code for pattern, code in VIOLATION_CODES if pattern.search(text)), "other_constraint")


def check_answer(
    answer: Answer,
    memory: dict[str, str],
    owned: list[str],
    limits: AdaptiveLimits,
    project: str,
) -> Checked:
    """The harness's own check of the actions, run with the campaign's real limits and the real carried memory, and
    the requirement that a ready answer changes something."""
    checked = Checked()
    if answer.status != "ready":
        return checked
    try:
        parsed = parse_actions(answer.actions, project)
    except ResponseError:
        checked.codes.append("bad_actions")
        return checked
    paths = [action.path for action in parsed]
    if len(set(paths)) != len(paths):
        checked.codes.append("duplicate_path")
    checked.violations = validate_actions(memory, answer.actions, set(owned), limits, project)
    checked.codes.extend(code for code in map(violation_code, checked.violations) if code not in checked.codes)
    checked.effective = any(
        (action.path in memory) if action.op == "delete" else memory.get(action.path) != action.content
        for action in parsed
    )
    if not checked.effective:
        checked.codes.append("no_effective_change")
    return checked


def hook_response(answer: Answer) -> dict:
    """The hook's response.json: the actions and a short note, never the notebook or the status."""
    return {"schema_version": SCHEMA_VERSION, "actions": answer.actions, "note": answer.explanation}


def repair_message(codes: list[str]) -> str:
    """The structural repair request: neutral problem codes with fixed descriptions, nothing the validator printed."""
    lines = [f"- {code}: {CODE_TEXT[code]}" for code in codes]
    return (
        "Your previous answer could not be used.\n"
        + "\n".join(lines)
        + "\nReturn one corrected json object with exactly status, actions, notebook and explanation for the same "
        "assignment."
    )
