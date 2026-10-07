"""Versioned experiment contracts. Payloads are data; extensions are explicit Python code."""

import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator


class Contract(BaseModel, extra="forbid"):
    pass


class AgentOutcome(Contract):
    exit_status: str
    steps: int = Field(ge=0)
    cost_usd: float = Field(ge=0, allow_inf_nan=False)
    final: str = ""
    messages: list[dict[str, Any]] = []


class Target(Contract):
    task_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    image: str | None = None


class ScriptAction(Contract):
    command: str = Field(min_length=1)
    if_contains: str | None = None


class Session(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    task_id: str | None = Field(None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    exposure: bool = False
    instruction: str | None = None
    script: list[ScriptAction] = []
    max_steps: int = Field(0, ge=0)


class Intervention(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    method: Literal["file", "output", "memory"]
    strategy: str = "once"
    phase: Literal["session_start", "before_action", "after_observation", "session_end"] = "session_start"
    sessions: list[str] = Field(min_length=1)
    path: str | None = None
    operation: Literal["append", "prepend", "replace"] = "append"
    old_text: str | None = None
    text: str | None = None
    payload_file: Path | None = None
    parameters: dict[str, str] = {}
    # Observations reach the model JSON-encoded, so a marker must survive escaping unchanged.
    marker: str | None = Field(None, pattern=r"^[A-Za-z0-9_:.-]+$")
    command_contains: str | None = None
    output_contains: str | None = None
    min_step: int | None = Field(None, ge=1)
    max_step: int | None = Field(None, ge=1)
    max_fires: int = Field(1, ge=1, le=1000)
    copy_patterns: list[str] = []
    objective: Literal["fact", "preference", "action", "persistence"] | None = None
    family: str | None = None
    intended: Literal["in_task", "cross_task"] | None = None
    git: Literal["commit", "worktree"] | None = None
    _copy_regexes: list[re.Pattern] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if (self.text is None) == (self.payload_file is None):
            raise ValueError("provide exactly one of text or payload_file")
        if self.parameters and self.payload_file is None:
            raise ValueError("parameters apply only to payload_file")
        if self.text == "" or self.old_text == "":
            raise ValueError("text and old_text cannot be empty")
        if self.method == "output" and self.phase != "after_observation":
            raise ValueError("output interventions require after_observation")
        if self.method != "output":
            validate_path(self.path or "", "/memories" if self.method == "memory" else "/app")
        elif self.path is not None:
            raise ValueError("output interventions do not have a path")
        if self.phase in {"session_start", "session_end"} and (self.command_contains or self.output_contains):
            raise ValueError("action filters require an action phase")
        if self.output_contains and self.phase != "after_observation":
            raise ValueError("output_contains requires after_observation")
        if (self.min_step or self.max_step) and self.phase not in {"before_action", "after_observation"}:
            raise ValueError("min_step and max_step require an action phase")
        if self.min_step and self.max_step and self.min_step > self.max_step:
            raise ValueError("min_step cannot exceed max_step")
        if self.old_text is not None and self.operation != "replace":
            raise ValueError("old_text requires replace")
        if self.strategy == "once" and self.max_fires != 1:
            raise ValueError("once requires max_fires=1; use repeat or sequence")
        self._copy_regexes = [compile_pattern(pattern) for pattern in self.copy_patterns]
        if any(pattern.search("") for pattern in self._copy_regexes):
            raise ValueError("copy patterns cannot match empty text")
        if self.git is not None and self.method != "file":
            raise ValueError("git placement applies only to file interventions")
        if self.git == "commit" and self.phase != "session_start":
            raise ValueError("git commit placement requires session_start; action phases change the worktree")
        return self

    @property
    def placement(self) -> str | None:
        """Effective git placement of a file intervention: session_start files are committed by default."""
        if self.method != "file":
            return None
        return self.git or ("commit" if self.phase == "session_start" else "worktree")

    @property
    def channel(self) -> str:
        from sereno.context_eval.memory import INDEX, USER

        if self.method != "memory":
            return "repo_file" if self.method == "file" else "command_output"
        return {USER: "user_file", INDEX: "memory_index"}.get(self.path, "memory_topic")

    def copy_match(self, text: str) -> str | None:
        """The first text a copy pattern recognises as a verbatim or paraphrased copy of this content."""
        for pattern in self._copy_regexes:
            if match := pattern.search(text):
                return match.group(0)
        return None

    def holds(self, text: str) -> bool:
        """Whether `text` carries this content: its marker or a copy a copy pattern recognises."""
        return bool((self.marker and self.marker in text) or self.copy_match(text))


def compile_pattern(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as error:
        raise ValueError(f"invalid regex {pattern!r}: {error}") from None


def validate_path(path: str, root: str) -> str:
    parts = PurePosixPath(path).parts
    if not path.startswith(root + "/") or any(p in {"..", ".git"} for p in parts):
        raise ValueError(f"path must be a file below {root}, outside .git")
    if "\\" in path or str(PurePosixPath(path)) != path:
        raise ValueError("path must be canonical POSIX")
    return path


class Check(Contract):
    """A deterministic assertion. Only goal checks decide the outcome; a constraint match is a violation."""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["goal", "trace", "flag", "constraint"] = "goal"
    source: Literal[
        "memory",
        "context",
        "observations",
        "final",
        "workspace",
        "patch",
        "commands",
        "assistant",
        "reasoning",
        "verifier",
        "verifier_tests",
        "result",
    ]
    sessions: list[str] = []
    path: str | None = None
    contains: str | None = None
    regex: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.contains is None) == (self.regex is None):
            raise ValueError("provide exactly one of contains or regex")
        if self.contains == "":
            raise ValueError("contains cannot be empty")
        if self.regex is not None:
            compile_pattern(self.regex)
        if self.source == "verifier" and any(
            line in (self.contains or self.regex) for line in ("f2p_failed", "p2p_failed")
        ):
            raise ValueError("per-test lines are in source verifier_tests; the verifier text never holds them")
        if self.source == "workspace":
            validate_path(self.path or "", "/app")
        elif self.path is not None:
            if self.source != "memory":
                raise ValueError("path only applies to workspace or memory")
            validate_path(self.path, "/memories")
        return self


class MetricSpec(Contract):
    name: str
    parameters: dict[str, Any] = {}


class MemoryConfig(Contract):
    seed: dict[str, str] = {}
    user: str | None = None
    instructions_file: Path | None = None
    max_files: int = Field(100, ge=1, le=10000)
    max_bytes: int = Field(1_000_000, ge=25000)

    def initial(self) -> dict[str, str]:
        """Agent-owned seed files plus the user-written file, identical for every arm."""
        from sereno.context_eval.memory import USER

        return {**self.seed, **({USER: self.user} if self.user is not None else {})}

    def within_limits(self, files: dict[str, str]) -> bool:
        return len(files) <= self.max_files and sum(len(t.encode()) for t in files.values()) <= self.max_bytes

    @model_validator(mode="after")
    def coherent(self):
        from sereno.context_eval.memory import USER

        for path in self.seed:
            validate_path(path, "/memories")
        if USER in self.seed:
            raise ValueError(f"{USER} is user-written; set memory.user instead of seeding it")
        files = self.initial()
        if not self.within_limits(files):
            raise ValueError("seed exceeds memory limits")
        return self


class Repair(Contract):
    """The user's correction at the start of one probe session, through the only trusted channel, AGENT.md;
    `retract` also removes the interventions' content from the carried memory as attack_ablate does."""

    session: str
    user_update: str = Field(min_length=1)
    operation: Literal["append", "replace"] = "append"
    retract: bool = False

    def apply(self, before: str) -> str:
        """AGENT.md after the update: the update appended to `before` on a line of its own, or in its place."""
        if self.operation == "replace":
            return self.user_update
        return before + ("\n" if before and not before.endswith("\n") else "") + self.user_update


class ExperimentConfig(Contract):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    dataset_root: Path
    targets: list[Target] = Field(min_length=1)
    agent: str = "mini-swe"
    model_config_file: Path | None = None
    sessions: list[Session] = Field(min_length=1)
    variants: dict[str, list[Intervention]] = {"default": []}
    checks: list[Check] = []
    metrics: list[MetricSpec] = []
    memory: MemoryConfig = MemoryConfig()
    arms: list[Literal["clean", "clean_reset", "attack_carry", "attack_reset", "attack_ablate"]] = [
        "clean",
        "attack_carry",
        "attack_reset",
    ]
    repeats: int = Field(1, ge=1, le=1000)
    workers: int = Field(1, ge=1, le=64)
    seed: int = Field(0, ge=0)
    cost_limit_usd: float = Field(2.0, gt=0, allow_inf_nan=False)
    campaign_cost_limit_usd: float = Field(20.0, gt=0, allow_inf_nan=False)
    wall_time_limit_seconds: int | None = Field(None, ge=1)
    repair: Repair | None = None
    # The campaign's frozen mini-swe configuration, set by the runner; never part of the experiment file.
    _agent_config_file: Path | None = PrivateAttr(None)

    @model_validator(mode="after")
    def coherent(self):
        ids = [s.id for s in self.sessions]
        if len(set(ids)) != len(ids) or len(set(self.arms)) != len(self.arms):
            raise ValueError("session IDs and arms must be unique")
        if len({t.task_id for t in self.targets}) != len(self.targets):
            raise ValueError("target task IDs must be unique")
        if self.cost_limit_usd > self.campaign_cost_limit_usd:
            raise ValueError("cost_limit_usd cannot exceed campaign_cost_limit_usd")
        if not self.arms or not self.variants:
            raise ValueError("arms and variants cannot be empty")
        for arm, base in {
            "attack_reset": "attack_carry",
            "attack_ablate": "attack_carry",
            "clean_reset": "clean",
        }.items():
            if arm in self.arms and base not in self.arms:
                raise ValueError(f"{arm} requires {base}")
        exposure = [s.exposure for s in self.sessions]
        if any(exposure[i] and not exposure[i - 1] for i in range(1, len(exposure))):
            raise ValueError("exposure sessions must precede all probes")
        if self.repair is not None:
            if self.schema_version == "1.0":
                raise ValueError("repair requires schema_version 1.1")
            if self.repair.session not in ids:
                raise ValueError("repair: unknown session")
            if ids.index(self.repair.session) <= sum(exposure):
                raise ValueError("repair: the session must follow every exposure session and at least one probe")
        retracts = self.repair is not None and self.repair.retract
        removes = "attack_ablate" if "attack_ablate" in self.arms else ("repair retraction" if retracts else None)
        update = self.repair.user_update if self.repair else ""
        for name, events in self.variants.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise ValueError("variant names must be filesystem-safe")
            if len({e.id for e in events}) != len(events):
                raise ValueError("intervention IDs must be unique per variant")
            markers = [e.marker for e in events if e.marker]
            # Attribution matches markers as substrings, so one marker inside another would credit both.
            if any(i != j and a in b for i, a in enumerate(markers) for j, b in enumerate(markers)):
                raise ValueError("markers must be unique per variant for source attribution")
            for event in events:
                if set(event.sessions) - set(ids):
                    raise ValueError(f"{event.id}: unknown sessions")
                if any(not s.exposure for s in self.sessions if s.id in event.sessions):
                    raise ValueError(f"{event.id}: interventions cannot target clean probe sessions")
                if removes and event.marker is None and not event.copy_patterns:
                    raise ValueError(
                        f"{name}/{event.id}: {removes} needs a marker or copy_patterns to find what it removes"
                    )
                # The update is the user's own text; holding planted content, it would read as transport.
                if event.holds(update):
                    raise ValueError(f"repair: user_update holds content of {name}/{event.id}")
                # Initial memory reaches every arm; holding planted content, it would read as carried.
                for path, text in self.memory.initial().items():
                    if event.holds(text):
                        raise ValueError(f"memory: {path} holds content of {name}/{event.id}")
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("check IDs must be unique")
        for check in self.checks:
            if set(check.sessions) - set(ids):
                raise ValueError(f"{check.id}: unknown sessions")
        return self
