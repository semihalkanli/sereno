"""The writer's configuration: the frozen design package plus the settings that belong to a campaign.

The design package (writer-design.json and the files it names) is the contract; `writer.json` only points to it and
adds what a campaign decides: the deadline, the input cap, the repository stores and optionally the endpoint. Nothing
here widens a limit: the content patterns must equal the frozen design's exactly, and the deadline must end before the
hook's timeout.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from sereno.context_eval.config import fingerprint
from sereno.context_eval.schema import AdaptiveLimits, Contract
from sereno.writer.errors import ConfigError

SCHEMA_VERSION = "1.0"
DESIGN_SCHEMA = "design-1"
API_KEYS = frozenset({"model", "provider", "max_tokens", "response_format"})
# Fields the generation body never carries: sampling, effort and anything that changes how many answers come back.
BARRED_API_KEYS = frozenset(
    {
        "messages",
        "reasoning",
        "reasoning_effort",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "seed",
        "n",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "logit_bias",
    }
)
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class ViewSettings(Contract):
    """How much of the base-commit repository goes into the view, and where a long output line is cut."""

    # A supplied file over this size is listed in the tree and named as omitted, never cut.
    max_file_bytes: int = Field(default=200_000, ge=1)
    # Commit subjects reachable from the base commit.
    history_commits: int = Field(default=20, ge=0)
    # A command output line over this many characters is cut with a marker that names how much is missing.
    line_chars: int = Field(default=2000, ge=80)


class Settings(Contract):
    """The campaign-side file, `writer.json`."""

    schema_version: Literal["1.0"]
    route: Literal["memory"]
    # writer-design.json; its folder holds the files the design names.
    design: Path
    # Wall clock for one run of the program; the hook's timeout_seconds must be larger.
    deadline_seconds: int = Field(ge=1)
    # Largest request body, in UTF-8 bytes. A larger input is refused, never cut.
    max_input_bytes: int = Field(ge=1)
    # Repository address -> a bare git directory holding the base commit.
    repository_stores: dict[str, Path] = Field(default_factory=dict)
    # Overrides the design's endpoint; plain http is accepted for a loopback host only.
    endpoint: str | None = None
    notebook_max_utf8_bytes: int | None = Field(None, ge=1)
    view: ViewSettings = Field(default_factory=ViewSettings)


@dataclass(frozen=True)
class Control:
    """Request control from the design's proposed_request_control; the deadline comes from the settings."""

    connect_timeout: float
    response_timeout: float
    attempts: int
    retry_statuses: frozenset[int]
    retry_delay: float
    retry_after_max: float
    repairs: int


@dataclass(frozen=True)
class WriterConfig:
    settings: Settings
    endpoint: str
    credential_env: str
    api: dict
    system_prompt: str
    last_write: str | None
    objective: str
    memory_root: str
    forbidden_patterns: tuple[str, ...]
    notebook_max_bytes: int
    control: Control
    deadline_seconds: int
    assignment_review: str | None
    files: dict[str, str] = field(default_factory=dict)
    # File name -> sha256 of every design-package file read (not writer.json, whose content is in `settings`).
    package: dict[str, str] = field(default_factory=dict)

    @property
    def prompt_sha256(self) -> str:
        return fingerprint(self.system_prompt)

    @property
    def config_sha256(self) -> str:
        """One hash over everything that shapes a request: the settings, the bytes of every design-package file, the
        derived values. Machine paths are left out, so the same writer on another checkout has the same hash."""
        settings = self.settings.model_dump(mode="json")
        settings["repository_stores"] = sorted(settings["repository_stores"])
        return fingerprint(
            {
                "settings": settings,
                "package": dict(sorted(self.package.items())),
                "endpoint": self.endpoint,
                "api": self.api,
                "system_prompt": self.system_prompt,
                "last_write": self.last_write,
                "objective": self.objective,
                "memory_root": self.memory_root,
                "forbidden_patterns": list(self.forbidden_patterns),
                "notebook_max_bytes": self.notebook_max_bytes,
                "control": [
                    self.control.connect_timeout,
                    self.control.response_timeout,
                    self.control.attempts,
                    sorted(self.control.retry_statuses),
                    self.control.retry_delay,
                    self.control.retry_after_max,
                    self.control.repairs,
                ],
                "deadline_seconds": self.deadline_seconds,
            }
        )

    def identity(self) -> dict:
        """The two hashes that name this writer: its configuration and its code. They are command words, so the
        campaign manifest records them and `summarize` cannot pool campaigns of different writers."""
        return {"config_sha256": self.config_sha256, "code_sha256": code_sha256()}

    def credential(self) -> str:
        key = os.environ.get(self.credential_env, "").strip()
        if not key:
            raise ConfigError(f"{self.credential_env} is not set", "missing_credential")
        return key


def code_sha256() -> str:
    """A hash over this package's sources and the harness package it imports (sereno.context_eval, whose
    validate_actions and memory layout decide what the writer checks), recorded with every attempt: the campaign
    manifest hashes the words of the command that name files, and with `python -m` that leaves the writer's own code
    out."""
    package = Path(__file__).parent
    digest = hashlib.sha256()
    for source in sorted([*package.glob("*.py"), *(package.parent / "context_eval").glob("*.py")]):
        name = f"{source.parent.name}/{source.name}"
        digest.update(name.encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()


def read_text(path: Path, files: dict[str, str]) -> str:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from None
    files[str(path)] = hashlib.sha256(raw).hexdigest()
    return text


def read_json(path: Path, files: dict[str, str]) -> dict:
    try:
        data = json.loads(read_text(path, files))
    except ValueError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold an object")
    return data


def check_api(api: dict) -> None:
    """The generation body of README "Provider request": exactly these four fields, no sampling or effort, no
    fallback, every parameter required, the 128K length limit, a json object answer."""
    if set(api) & BARRED_API_KEYS:
        raise ConfigError(f"api.json carries barred fields {sorted(set(api) & BARRED_API_KEYS)}")
    if set(api) != API_KEYS:
        raise ConfigError(f"api.json must hold exactly {sorted(API_KEYS)}")
    provider = api["provider"]
    order = provider.get("order") if isinstance(provider, dict) else None
    if not (isinstance(order, list) and len(order) == 1 and isinstance(order[0], str)):
        raise ConfigError("api.json provider.order must name exactly one provider")
    if provider.get("allow_fallbacks") is not False or provider.get("require_parameters") is not True:
        raise ConfigError("api.json provider must set allow_fallbacks false and require_parameters true")
    if set(provider) != {"order", "allow_fallbacks", "require_parameters"}:
        raise ConfigError("api.json provider holds fields beyond order, allow_fallbacks and require_parameters")
    if not isinstance(api["model"], str) or not api["model"]:
        raise ConfigError("api.json model must be a string")
    if api["max_tokens"] != 128000 or isinstance(api["max_tokens"], bool):
        raise ConfigError("api.json max_tokens must be 128000 (decision 370)")
    if api["response_format"] != {"type": "json_object"}:
        raise ConfigError("api.json response_format must be a json_object")


def check_endpoint(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return
    raise ConfigError(f"endpoint {endpoint!r} must be https, or http on a loopback host")


def control_of(design: dict) -> Control:
    raw = design.get("proposed_request_control")
    if not isinstance(raw, dict):
        raise ConfigError("the design has no proposed_request_control")
    for flag in ("automatic_effort_escalation", "retry_refusal", "retry_truncation"):
        if raw.get(flag) is not False:
            raise ConfigError(f"the design must set {flag} false")
    try:
        control = Control(
            connect_timeout=float(raw["connect_timeout_seconds"]),
            response_timeout=float(raw["response_timeout_seconds"]),
            attempts=int(raw["transport_attempts_per_call"]),
            retry_statuses=frozenset(int(status) for status in raw["retry_http_statuses"]),
            retry_delay=float(raw["retry_delay_seconds"]),
            retry_after_max=float(raw["retry_after_max_seconds"]),
            repairs=int(raw["structural_repair_calls"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(f"proposed_request_control is incomplete or malformed: {error!r}") from None
    if (
        control.attempts < 1
        or control.repairs < 0
        or raw.get("max_generation_calls_per_transition") != control.repairs + 1
    ):
        raise ConfigError("the design's call counts do not agree: one author call plus the structural repairs")
    return control


def load_config(path: Path) -> WriterConfig:
    """Read writer.json and the design package behind it, and refuse anything that departs from the frozen design."""
    path = Path(path).resolve()
    files: dict[str, str] = {}
    try:
        settings = Settings.model_validate(read_json(path, files))
    except ValidationError as error:
        raise ConfigError(f"{path}: {error}") from None
    design_path = (path.parent / settings.design).resolve()
    design = read_json(design_path, files)
    if design.get("schema_version") != DESIGN_SCHEMA:
        raise ConfigError(f"the design's schema_version must be {DESIGN_SCHEMA!r}")
    folder = design_path.parent
    api = read_json(folder / str(design.get("api_settings_file")), files)
    check_api(api)
    job = (design.get("jobs") or {}).get(settings.route)
    if not isinstance(job, dict):
        raise ConfigError(f"the design has no job for the {settings.route} route")
    system_prompt = read_text(folder / str(job.get("system_prompt")), files)
    if not system_prompt.strip():
        raise ConfigError("the system prompt is empty")
    last_write = None
    if isinstance(job.get("last_write_message"), dict):
        last_write = read_text(folder / str(job["last_write_message"].get("file")), files).strip() or None
        if last_write is None:
            raise ConfigError("the last-write sentence is empty")
    review = design.get("assignment_review") or {}
    fragments = read_json(folder / str(review.get("file")), files)
    fragment = fragments.get(settings.route)
    if not (
        isinstance(fragment, dict)
        and all(isinstance(fragment.get(key), str) and fragment[key].strip() for key in ("objective", "memory_root"))
    ):
        raise ConfigError(f"the assignment file has no complete {settings.route} fragment (objective, memory_root)")
    limits = (design.get("harness_settings") or {}).get("limits") or {}
    patterns = limits.get("forbidden_patterns")
    try:
        AdaptiveLimits.model_validate(limits)
    except ValidationError as error:
        raise ConfigError(f"the design's limits do not load: {error}") from None
    if not (isinstance(patterns, list) and patterns):
        raise ConfigError("the design has no forbidden_patterns")
    endpoint = settings.endpoint or design.get("endpoint")
    if not isinstance(endpoint, str):
        raise ConfigError("the design names no endpoint")
    check_endpoint(endpoint)
    credential_env = design.get("credential_env_name")
    if not isinstance(credential_env, str) or not credential_env:
        raise ConfigError("the design names no credential variable")
    notebook_max = settings.notebook_max_utf8_bytes or (design.get("state") or {}).get(
        "proposed_notebook_max_utf8_bytes"
    )
    if not isinstance(notebook_max, int) or isinstance(notebook_max, bool) or notebook_max < 1:
        raise ConfigError("no usable notebook size limit")
    return WriterConfig(
        settings=settings,
        endpoint=endpoint,
        credential_env=credential_env,
        api=api,
        system_prompt=system_prompt,
        last_write=last_write,
        objective=fragment["objective"],
        memory_root=fragment["memory_root"].rstrip("/"),
        forbidden_patterns=tuple(patterns),
        notebook_max_bytes=notebook_max,
        control=control_of(design),
        deadline_seconds=settings.deadline_seconds,
        assignment_review=review.get("status"),
        files=files,
        package={Path(name).name: digest for name, digest in files.items() if Path(name) != path},
    )


def check_identity(config: WriterConfig, config_sha256: str, code_sha256_: str) -> None:
    """The command carries the hashes the campaign was set up with; a program whose configuration or code has changed
    since then refuses to run instead of becoming a different attacker under the same manifest."""
    for name, expected, actual in (
        ("config_sha256", config_sha256, config.config_sha256),
        ("code_sha256", code_sha256_, code_sha256()),
    ):
        if expected != actual:
            raise ConfigError(f"{name} is {actual}, the command expects {expected}", "identity_mismatch")


def check_limits(config: WriterConfig, limits: object) -> AdaptiveLimits:
    """The limits the harness sends with a request, loaded as the harness loads them. The content patterns must equal
    the frozen design's, in order: a validation with other limits than the executing harness enforces proves nothing."""
    try:
        loaded = AdaptiveLimits.model_validate(limits)
    except ValidationError as error:
        raise ConfigError(f"the request's limits do not load: {error}") from None
    if tuple(loaded.forbidden_patterns) != config.forbidden_patterns:
        raise ConfigError("the campaign's forbidden_patterns differ from the frozen design's", "patterns_differ")
    return loaded


def campaign_timeout(campaign_dir: Path) -> int:
    """The hook's timeout_seconds as the campaign manifest recorded it."""
    try:
        manifest = json.loads((Path(campaign_dir) / "manifest.json").read_text())
        timeout = manifest["adaptive"]["timeout_seconds"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigError(f"{campaign_dir}: manifest.json does not record the adaptive timeout") from None
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ConfigError("the manifest's adaptive timeout_seconds is not a number")
    return timeout


def check_deadline(config: WriterConfig, timeout_seconds: int) -> None:
    """The deadline must end before the hook kills the program, so a slow provider is a clean failure."""
    if timeout_seconds <= config.deadline_seconds:
        raise ConfigError(
            f"the campaign's timeout_seconds ({timeout_seconds}) must be larger than the writer deadline "
            f"({config.deadline_seconds})",
            "deadline",
        )
