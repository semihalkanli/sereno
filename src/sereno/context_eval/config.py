"""Resolve configs without model calls or implicit plugin imports."""

import hashlib
import importlib
import json
import re
from pathlib import Path
from string import Template

import yaml

from sereno.context_eval.contracts import Registry
from sereno.context_eval.schema import ExperimentConfig

CREDENTIAL_KEY = re.compile(
    r"(^|_)(api_?key|private_?key|access_?key|authorization|secret|password|passwd|token|credentials?)(_|$)"
)


def load_config(path: Path) -> ExperimentConfig:
    path = path.resolve()
    try:
        raw_config = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError("invalid experiment YAML syntax") from error
    config = ExperimentConfig.model_validate(raw_config)
    root = path.parent
    config.dataset_root = resolve(root, config.dataset_root)
    if config.model_config_file:
        config.model_config_file = resolve(root, config.model_config_file)
    if config.memory.instructions_file:
        config.memory.instructions_file = resolve(root, config.memory.instructions_file)
    for events in config.variants.values():
        for event in events:
            if event.payload_file:
                event.payload_file = resolve(root, event.payload_file)
                raw = event.payload_file.read_text()
                try:
                    event.text = Template(raw).substitute(event.parameters) if event.parameters else raw
                except KeyError as error:
                    raise ValueError(f"{event.id}: missing payload template parameter") from error
            if not event.text:
                raise ValueError(f"{event.id}: empty payload")
            event.payload_file, event.parameters = None, {}
    return config


def resolve(root: Path, path: Path) -> Path:
    return (root / path.expanduser()).resolve()


def fingerprint(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def default_registry(plugins: list[str] = ()) -> Registry:
    from sereno.context_eval.agents import AnthropicAdapter, MiniSweAdapter, ScriptedAdapter
    from sereno.context_eval.engine import BoundedStrategy

    registry = Registry()
    registry.register_agent("mini-swe", MiniSweAdapter())
    registry.register_agent("anthropic", AnthropicAdapter())
    registry.register_agent("scripted", ScriptedAdapter())
    for name in ("once", "repeat", "sequence"):
        registry.register_strategy(name, BoundedStrategy(name))
    for name in plugins:
        try:
            module = importlib.import_module(name)
        except ImportError as error:
            raise ValueError(f"cannot import plugin module {name}: {error}") from error
        module.register(registry)
        source = getattr(module, "__file__", None)
        registry.plugins.append(
            {"module": name, "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None}
        )
    return registry


def validate(config: ExperimentConfig, registry: Registry) -> dict:
    from sereno.context_eval.agents import MINI_SWE_AGENTS
    from sereno.context_eval.dataset import load_task
    from sereno.context_eval.memory import instructions

    # Python callers can modify a config after construction; validate the complete current contract again.
    config = ExperimentConfig.model_validate(config.model_dump())

    if config.agent not in registry.agents:
        raise ValueError(f"unknown agent adapter: {config.agent}")
    if config.agent in MINI_SWE_AGENTS and not config.model_config_file:
        raise ValueError(f"{config.agent} requires model_config_file")
    if not instructions(config.memory).strip():
        raise ValueError("memory instructions cannot be empty")
    if config.model_config_file:
        try:
            model = yaml.safe_load(config.model_config_file.read_text())
        except yaml.YAMLError as error:
            raise ValueError("invalid model YAML syntax") from error
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("model"), dict)
            or not model["model"].get("model_name")
        ):
            raise ValueError("model config requires model.model_name")
        builtin_models = {
            "",
            "litellm",
            "litellm_textbased",
            "litellm_response",
            "openrouter",
            "openrouter_textbased",
            "openrouter_response",
            "portkey",
            "portkey_response",
            "requesty",
            "deterministic",
        }
        if config.agent == "anthropic":
            validate_anthropic_model(config.model_config_file)
        elif model["model"].get("model_class", "") not in builtin_models:
            raise ValueError("model_class must name a built-in mini-swe model, not an import path")
        reject_inline_credentials(model)
    tasks = {}
    for target in config.targets:
        tasks[target.task_id] = load_task(config.dataset_root, target.task_id)
    for session in config.sessions:
        if session.task_id:
            tasks[session.task_id] = load_task(config.dataset_root, session.task_id)
        if config.agent == "scripted" and not session.script:
            raise ValueError(f"{session.id}: scripted adapter requires script actions")
    for events in config.variants.values():
        for event in events:
            if event.payload_file is not None or event.text is None:
                raise ValueError("unresolved payload; load YAML with load_config before validating or running")
            if event.strategy not in registry.strategies:
                raise ValueError(f"unknown strategy: {event.strategy}")
    for metric in config.metrics:
        if metric.name not in registry.metrics:
            raise ValueError(f"unknown custom metric: {metric.name}")
    return {
        "valid": True,
        "cases": len(config.targets) * len(config.variants) * config.repeats,
        "sessions_per_case": len(config.sessions) * len(config.arms),
        "tasks": sorted(tasks),
        "agent": config.agent,
    }


def validate_anthropic_model(model_config_file: Path):
    """The Anthropic agent's model config before any session starts: known fields and messages.create arguments,
    max_tokens set, and a price for the model, so a session never runs unpriced."""
    from sereno.context_eval.agents import mini_swe_config
    from sereno.context_eval.anthropic_model import AnthropicModelConfig
    from sereno.pricing import ANTHROPIC_PRICES

    model = AnthropicModelConfig(**mini_swe_config(model_config_file, "anthropic")["model"])
    if model.model_name not in ANTHROPIC_PRICES:
        raise ValueError(f"no price for model {model.model_name} in sereno.pricing")


def reject_inline_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            # Whole key segments, so token budgets such as thinking_budget_tokens are not taken for credentials;
            # camelCase humps, '-' and '.' separate segments too, so x-api-key and accessToken are caught.
            words = re.sub(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])", r"\1\3_\2\4", str(key))
            if CREDENTIAL_KEY.search(re.sub(r"[-.]", "_", words.lower())):
                raise ValueError("inline credentials are not supported; use provider environment variables")
            reject_inline_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_inline_credentials(item)
