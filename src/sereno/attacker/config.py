"""The attacker agent's configuration: the operator's brief, the provider block and the runtime's bounds.

The brief is a file the operator supplies; this package writes no attack text. Everything model-specific lives in the
`provider` block, so pointing the runtime at another model of the same API is a configuration change and another API
is one adapter module. The bounds are part of the measurement: one invocation's wall clock, the number of model
calls, and how much of the observation reaches the model in the first message.
"""

import argparse
import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, ValidationError

from sereno.attacker.errors import ConfigError
from sereno.attacker.identity import code_sha256
from sereno.context_eval.config import fingerprint, reject_inline_credentials
from sereno.context_eval.schema import harness_controlled

SCHEMA_VERSION = "1.0"
# Fields the runtime builds itself; a configuration that sets them would be silently overridden.
BARRED_REQUEST_KEYS = frozenset({"messages", "model", "stream", "system", "tools"})
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A product token such as `sereno-attacker/1.0`, never a generic SDK or HTTP-library name.
PRODUCT_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
GENERIC_AGENTS = ("python-httpx", "httpx", "openai", "litellm", "requests", "aiohttp", "urllib", "curl")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
# Headers the runtime sets itself; a session-header policy may not replace one of them.
RUNTIME_HEADERS = frozenset({"authorization", "content-type"})
# The opencode Go API's chat-completions route: the only route the openai-compatible adapter's body fits. Another
# route under the same prefix, such as the Messages path, would take that body and answer nothing usable.
OPENCODE_HOST = "opencode.ai"
OPENCODE_PATH = "/inference/go/"
OPENCODE_ROUTE = "/chat/completions"


class Settings(BaseModel, extra="forbid"):
    pass


class HttpSettings(Settings):
    """One logical call is at most `attempts` transport attempts, all inside the invocation's deadline."""

    connect_timeout_seconds: float = Field(default=20.0, gt=0)
    response_timeout_seconds: float = Field(default=600.0, gt=0)
    attempts: int = Field(default=2, ge=1, le=10)
    retry_statuses: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])
    retry_delay_seconds: float = Field(default=5.0, ge=0)
    retry_after_max_seconds: float = Field(default=60.0, ge=0)


class ProviderSettings(Settings):
    """The API behind the agent. `request` is merged into every request body, so provider-specific fields such as
    OpenRouter's routing block or an output limit travel with the model they belong to."""

    kind: Literal["openai-compatible", "anthropic"]
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    # The environment variable that holds the key; null for a model served without one.
    credential_env: str | None = None
    # Anthropic's Messages API version header; unused by the openai-compatible adapter.
    api_version: str = "2023-06-01"
    headers: dict[str, str] = Field(default_factory=dict)
    # The client's own product token, sent as `User-Agent`; null leaves the client's default.
    user_agent: str | None = None
    # A header the runtime fills with one stable id per invocation (one conversation), for APIs that require a session
    # id. The id is derived from the request, never from a path.
    session_header: str | None = None
    # OpenRouter reports the serving provider; when this is set, another provider's answer is refused (decision 99).
    expected_provider: str | None = None
    request: dict = Field(default_factory=dict)


class LoopSettings(Settings):
    """The loop's bounds: how many model calls one invocation may make and how much of the observation and of every
    tool result reaches the model."""

    max_steps: int = Field(default=24, ge=1, le=200)
    max_tool_calls_per_step: int = Field(default=8, ge=1, le=64)
    max_tool_output_bytes: int = Field(default=40_000, ge=1_000)
    observation_max_bytes: int = Field(default=150_000, ge=10_000)
    observation_file_bytes: int = Field(default=30_000, ge=500)
    observation_paths: int = Field(default=200, ge=1)
    transcript_page_messages: int = Field(default=40, ge=1, le=1_000)
    transcript_page_bytes: int = Field(default=30_000, ge=1_000)
    search_limit: int = Field(default=40, ge=1, le=500)
    max_queued_reads: int = Field(default=24, ge=1, le=1_000)


class AttackerSettings(Settings):
    schema_version: Literal["1.0"]
    # The operator's brief: the methods and techniques prompt for the attacker role.
    prompt_file: Path
    # Appended to the brief under its own heading; empty adds nothing.
    objective: str = ""
    provider: ProviderSettings
    http: HttpSettings = Field(default_factory=HttpSettings)
    agent: LoopSettings = Field(default_factory=LoopSettings)
    deadline_seconds: int = Field(default=900, ge=1, le=86_400)
    decline_retries: int = Field(default=2, ge=0, le=10)


@dataclass(frozen=True)
class AttackerConfig:
    path: Path
    settings: AttackerSettings
    prompt: str

    @property
    def prompt_sha256(self) -> str:
        return fingerprint(self.prompt)

    @property
    def config_sha256(self) -> str:
        """One hash over everything that shapes a call: the settings and the exact prompt text."""
        return fingerprint({"settings": self.settings.model_dump(mode="json"), "prompt": self.prompt})

    def identity(self) -> dict:
        return {"config_sha256": self.config_sha256, "prompt_sha256": self.prompt_sha256, "code_sha256": code_sha256()}

    def credential(self) -> str | None:
        env = self.settings.provider.credential_env
        if env is None:
            return None
        key = os.environ.get(env, "").strip()
        if not key:
            raise ConfigError(f"{env} is not set", "missing_credential")
        return key


def check_base_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return
    raise ConfigError(f"base_url {url!r} must be https, or http on a loopback host", "endpoint")


def check_identity(config: AttackerConfig, pins: dict[str, str | None], *, required: bool) -> None:
    identity = config.identity()
    for name, actual in identity.items():
        expected = pins.get(name)
        if expected is None:
            if required:
                raise ConfigError(
                    f"live mode requires --{name.replace('_', '-')}; obtain it with --check", "missing_pin"
                )
        elif expected != actual:
            raise ConfigError(f"{name} is {actual}, the command expects {expected}", "identity_mismatch")


def check_command_identity(command: list[str]) -> None:
    for index in range(len(command) - 1):
        if command[index : index + 2] != ["-m", "sereno.attacker"]:
            continue
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--config", type=Path)
        for name in ("config", "prompt", "code"):
            parser.add_argument(f"--{name}-sha256")
        try:
            args, _ = parser.parse_known_args(command[index + 2 :])
        except argparse.ArgumentError as error:
            raise ConfigError(str(error), "arguments") from None
        if args.config is None:
            raise ConfigError("the attacker command needs --config", "missing_pin")
        config = load_config(args.config)
        check_identity(config, {name: getattr(args, name) for name in config.identity()}, required=True)
        return


def check_opencode_base_url(kind: str, url: str) -> None:
    """A base_url on the opencode Go API must be the chat-completions route the openai-compatible adapter speaks: the
    runtime builds a chat-completions body, and another route under the same prefix would accept it and answer
    nothing the adapter can read. Any other host, and the Messages adapter, are left to `check_base_url`."""
    parts = urlsplit(url)
    if parts.hostname != OPENCODE_HOST or kind != "openai-compatible":
        return
    if not parts.path.startswith(OPENCODE_PATH) or not parts.path.endswith(OPENCODE_ROUTE):
        raise ConfigError(
            f"base_url {url!r} must be under {OPENCODE_PATH} and end with {OPENCODE_ROUTE} on {OPENCODE_HOST}",
            "endpoint",
        )


def check_settings(settings: AttackerSettings) -> None:
    provider = settings.provider
    check_base_url(provider.base_url)
    check_opencode_base_url(provider.kind, provider.base_url)
    if provider.credential_env is not None and not ENV_NAME.match(provider.credential_env):
        raise ConfigError(f"credential_env {provider.credential_env!r} is not a variable name", "credential_env")
    if provider.credential_env is not None and harness_controlled(provider.credential_env):
        # Such a name would hand the program a variable the harness sets for itself, not a credential.
        raise ConfigError(
            f"credential_env {provider.credential_env!r} is a harness-controlled variable", "credential_env"
        )
    if provider.user_agent is not None:
        if not PRODUCT_AGENT.fullmatch(provider.user_agent):
            raise ConfigError(
                f"user_agent {provider.user_agent!r} is not a product token such as sereno-attacker/1.0", "user_agent"
            )
        if provider.user_agent.lower().startswith(GENERIC_AGENTS):
            raise ConfigError(f"user_agent {provider.user_agent!r} is a generic SDK or HTTP-library name", "user_agent")
    if provider.session_header is not None:
        if not HEADER_NAME.fullmatch(provider.session_header):
            raise ConfigError(f"session_header {provider.session_header!r} is not a header name", "session_header")
        if provider.session_header.lower() in RUNTIME_HEADERS:
            raise ConfigError(
                f"session_header {provider.session_header!r} would replace a header the runtime sets",
                "session_header",
            )
        if provider.session_header.lower() in {name.lower() for name in provider.headers}:
            raise ConfigError(
                f"session_header {provider.session_header!r} is also a configured header", "session_header"
            )
    if barred := sorted(set(provider.request) & BARRED_REQUEST_KEYS):
        raise ConfigError(f"provider.request carries {barred}; the runtime builds those fields", "barred_field")
    if provider.kind == "anthropic":
        # The Messages API has no default for max_tokens, so a configuration without it cannot call the model.
        if not isinstance(provider.request.get("max_tokens"), int) or isinstance(
            provider.request.get("max_tokens"), bool
        ):
            raise ConfigError("an anthropic provider needs provider.request.max_tokens", "missing_max_tokens")


def credential_scan_value(raw: dict) -> dict:
    """The configuration minus the provider's credential variable name, which is a name, not a secret."""
    scanned = copy.deepcopy(raw)
    provider = scanned.get("provider")
    if isinstance(provider, dict):
        provider.pop("credential_env", None)
    return scanned


def load_config(path: Path) -> AttackerConfig:
    """Read the configuration and the brief it points at, and refuse anything ambiguous before a model is called."""
    path = Path(path).resolve()
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}", "config") from None
    except yaml.YAMLError as error:
        raise ConfigError(f"{path} is not valid YAML: {error}", "config") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a mapping", "config")
    try:
        reject_inline_credentials(credential_scan_value(raw))
    except ValueError as error:
        raise ConfigError(str(error), "inline_credential") from None
    try:
        settings = AttackerSettings.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"{path}: {error}", "config") from None
    check_settings(settings)
    prompt_path = (path.parent / settings.prompt_file).resolve()
    try:
        prompt = prompt_path.read_text()
    except OSError as error:
        raise ConfigError(f"cannot read the prompt file {prompt_path}: {error}", "prompt") from None
    if not prompt.strip():
        raise ConfigError(f"the prompt file {prompt_path} is empty", "prompt")
    if settings.objective.strip():
        prompt = prompt.rstrip() + "\n\n## Objective\n\n" + settings.objective.strip() + "\n"
    return AttackerConfig(path=path, settings=settings, prompt=prompt)
