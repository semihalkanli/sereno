"""Versioned experiment contracts. Payloads are data; extensions are explicit Python code."""

import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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
    max_steps: int = Field(100, ge=1)


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
    marker: str | None = None
    command_contains: str | None = None
    output_contains: str | None = None
    max_fires: int = Field(1, ge=1, le=1000)

    @model_validator(mode="after")
    def coherent(self):
        if (self.text is None) == (self.payload_file is None):
            raise ValueError("provide exactly one of text or payload_file")
        if self.text == "" or self.marker == "" or self.old_text == "":
            raise ValueError("text, marker and old_text cannot be empty")
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
        if self.old_text is not None and self.operation != "replace":
            raise ValueError("old_text requires replace")
        if self.strategy == "once" and self.max_fires != 1:
            raise ValueError("once requires max_fires=1; use repeat or sequence")
        return self


def validate_path(path: str, root: str) -> str:
    parts = PurePosixPath(path).parts
    if not path.startswith(root + "/") or any(p in {"..", ".git"} for p in parts):
        raise ValueError(f"path must be a file below {root}, outside .git")
    if "\\" in path or str(PurePosixPath(path)) != path:
        raise ValueError("path must be canonical POSIX")
    return path


class Check(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    source: Literal["memory", "context", "observations", "final", "workspace", "patch"]
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
            re.compile(self.regex)
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
    enabled: bool = True
    seed: dict[str, str] = {}
    max_files: int = Field(100, ge=1, le=10000)
    max_bytes: int = Field(1_000_000, ge=25000)

    @model_validator(mode="after")
    def coherent(self):
        for path in self.seed:
            validate_path(path, "/memories")
        if not self.enabled and self.seed:
            raise ValueError("disabled memory cannot have seed files")
        if len(self.seed) > self.max_files or sum(len(t.encode()) for t in self.seed.values()) > self.max_bytes:
            raise ValueError("seed exceeds memory limits")
        return self


class ExperimentConfig(Contract):
    schema_version: Literal["1.0"] = "1.0"
    dataset_root: Path
    targets: list[Target] = Field(min_length=1)
    agent: str = "mini-swe"
    model_config_file: Path | None = None
    sessions: list[Session] = Field(min_length=1)
    variants: dict[str, list[Intervention]] = {"default": []}
    checks: list[Check] = []
    metrics: list[MetricSpec] = []
    memory: MemoryConfig = MemoryConfig()
    arms: list[Literal["clean", "attack_carry", "attack_reset"]] = ["clean", "attack_carry", "attack_reset"]
    repeats: int = Field(1, ge=1, le=1000)
    workers: int = Field(1, ge=1, le=64)
    seed: int = Field(0, ge=0)
    cost_limit_usd: float = Field(2.0, gt=0)
    campaign_cost_limit_usd: float = Field(20.0, gt=0)
    wall_time_limit_seconds: int = Field(10800, ge=1)

    @model_validator(mode="after")
    def coherent(self):
        ids = [s.id for s in self.sessions]
        if len(set(ids)) != len(ids) or len(set(self.arms)) != len(self.arms):
            raise ValueError("session IDs and arms must be unique")
        if len({t.task_id for t in self.targets}) != len(self.targets):
            raise ValueError("target task IDs must be unique")
        if not self.arms or not self.variants:
            raise ValueError("arms and variants cannot be empty")
        if "attack_reset" in self.arms and "attack_carry" not in self.arms:
            raise ValueError("attack_reset requires attack_carry")
        exposure = [s.exposure for s in self.sessions]
        if any(exposure[i] and not exposure[i - 1] for i in range(1, len(exposure))):
            raise ValueError("exposure sessions must precede all probes")
        for name, events in self.variants.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise ValueError("variant names must be filesystem-safe")
            if len({e.id for e in events}) != len(events):
                raise ValueError("intervention IDs must be unique per variant")
            markers = [e.marker for e in events if e.marker]
            if len(markers) != len(set(markers)):
                raise ValueError("markers must be unique per variant for source attribution")
            for event in events:
                if set(event.sessions) - set(ids):
                    raise ValueError(f"{event.id}: unknown sessions")
                if any(not s.exposure for s in self.sessions if s.id in event.sessions):
                    raise ValueError(f"{event.id}: interventions cannot target clean probe sessions")
                if event.method == "memory" and not self.memory.enabled:
                    raise ValueError("memory interventions require enabled memory")
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("check IDs must be unique")
        for check in self.checks:
            if set(check.sessions) - set(ids):
                raise ValueError(f"{check.id}: unknown sessions")
        return self
