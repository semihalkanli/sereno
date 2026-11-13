"""The provider registry: one adapter per API kind, chosen by the configuration's `provider.kind`."""

import time

import httpx

from sereno.attacker.config import AttackerConfig
from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ConfigError
from sereno.attacker.providers.anthropic import AnthropicProvider
from sereno.attacker.providers.base import Completion, Provider, ToolCall, ToolSpec, Turn
from sereno.attacker.providers.openai import OpenAICompatibleProvider
from sereno.attacker.providers.transport import Sink

KINDS = ("anthropic", "openai-compatible")
__all__ = ["KINDS", "Completion", "Provider", "ToolCall", "ToolSpec", "Turn", "build_provider"]


def build_provider(
    config: AttackerConfig,
    deadline: Deadline,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep=time.sleep,
    sink: Sink | None = None,
) -> Provider:
    kind = config.settings.provider.kind
    if kind == "openai-compatible":
        return OpenAICompatibleProvider(config, deadline, transport=transport, sleep=sleep, sink=sink)
    if kind == "anthropic":
        return AnthropicProvider(config, deadline, transport=transport, sleep=sleep, sink=sink)
    raise ConfigError(f"unknown provider kind {kind!r}", "provider_kind")
