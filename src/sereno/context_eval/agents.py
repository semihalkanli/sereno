"""mini-swe-agent adapter and a deterministic fixture oracle using the same runtime hooks and memory prompt."""

import json
import time
from pathlib import Path

import yaml

from sereno.context_eval.engine import message_text
from sereno.context_eval.environment import DEFAULT_ACTION_TIMEOUT
from sereno.context_eval.memory import instructions


def mini_swe_config(model_config_file: Path) -> dict:
    """mini-swe's built-in mini.yaml merged with the experiment's model config."""
    from minisweagent.config import builtin_config_dir
    from minisweagent.utils.serialize import recursive_merge

    return recursive_merge(
        yaml.safe_load((builtin_config_dir / "mini.yaml").read_text()),
        yaml.safe_load(model_config_file.read_text()),
    )


def agent_config(config) -> dict:
    """The mini-swe configuration a session runs with: the campaign's frozen copy once the runner set one."""
    frozen = getattr(config, "_agent_config_file", None)
    return yaml.safe_load(frozen.read_text()) if frozen else mini_swe_config(config.model_config_file)


def action_timeout(config) -> int:
    """Seconds an action may run: environment.timeout of the merged mini-swe config, else the default."""
    if config.agent != "mini-swe" or config.model_config_file is None:
        return DEFAULT_ACTION_TIMEOUT
    return int((agent_config(config).get("environment") or {}).get("timeout") or DEFAULT_ACTION_TIMEOUT)


def rendered_output(output: dict) -> dict:
    text = output["output"]
    rendered = {"returncode": output["returncode"]}
    if len(text) <= 10000:
        rendered["output"] = text
    else:
        rendered |= {"output_head": text[:5000], "output_tail": text[-5000:], "elided_chars": len(text) - 10000}
    return rendered | ({"exception_info": output["exception_info"]} if output.get("exception_info") else {})


def final_text(messages: list[dict]) -> str:
    """The agent's closing statement: its last reply with visible text; a bare tool-call reply does not replace it."""
    texts = [message_text([m]) for m in messages if m.get("role") == "assistant"]
    return next((text for text in reversed(texts) if text.strip()), "")


def format_error_reply(error) -> dict:
    """The model reply a mini-swe FormatError carries: the provider message when the response has one."""
    extra = dict((error.messages[0] if error.messages else {}).get("extra") or {})
    response = extra.get("response")
    choices = (response.get("choices") if isinstance(response, dict) else None) or [{}]
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return {"role": "assistant", "content": "", **(message if isinstance(message, dict) else {}), "extra": extra}


class ScriptedAdapter:
    def run(self, runtime, instruction, memory_context, config, session):
        # The memory text mini-swe would see: instructions as the system message, the reminder before the task.
        messages = [
            {"role": "system", "content": instructions(config.memory)},
            {"role": "user", "content": f"{memory_context}\n\n{instruction}"},
        ]
        runtime.initial_messages = list(messages)
        started = time.monotonic()
        steps = 0
        exit_status = "script_complete"
        for action in session.script:
            # The same caps and exit statuses as mini-swe; max_steps 0 means no step cap.
            if 0 < session.max_steps <= steps:
                exit_status = "LimitsExceeded"
                break
            if time.monotonic() - started >= config.wall_time_limit_seconds:
                exit_status = "TimeExceeded"
                break
            seen = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "assistant")
            if action.if_contains is not None and action.if_contains not in seen:
                continue
            runtime.context_sent(messages)
            reply = {"role": "assistant", "content": action.command}
            runtime.log.emit("model_result", session_id=runtime.session.id, cost_usd=0.0, message=reply)
            messages.append(reply)
            steps += 1
            # Counted before the action runs, as mini-swe counts a model call, for runs that end in an error.
            runtime.partial_agent_result = {"steps": steps, "cost_usd": 0.0}
            output = runtime.execute(action.command)
            if output["submitted"]:
                exit_status = "Submitted"
                break
            observation = [{"role": "user", "content": json.dumps(rendered_output(output), ensure_ascii=False)}]
            runtime.observation(observation)
            messages.extend(observation)
        return {
            "exit_status": exit_status,
            "steps": steps,
            "cost_usd": 0.0,
            "final": final_text(messages),
            "messages": messages,
        }


class MiniSweAdapter:
    def run(self, runtime, instruction, memory_context, config, session):
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.exceptions import FormatError, Submitted
        from minisweagent.models import get_model

        merged = agent_config(config)
        merged["model"]["cost_tracking"] = "default"
        # env settings from model configs must not forward host credentials or mount host paths.
        environment_vars = merged.get("environment", {}).get("env", {})

        class EnvironmentProxy:
            def execute(self, action, **kwargs):
                output = runtime.execute(action["command"])
                if output["submitted"]:
                    text = "".join(runtime.last_output.lstrip().splitlines(keepends=True)[1:])
                    raise Submitted(
                        {"role": "exit", "content": text, "extra": {"exit_status": "Submitted", "submission": text}}
                    )
                return output

            def get_template_vars(self):
                return {"cwd": "/app", "system": "Linux", "release": "", "version": "", "machine": "x86_64"}

            def serialize(self):
                return {"info": {"environment": "sereno-context-eval-docker"}}

        if merged["model"].get("model_class") == "openrouter":
            from sereno.context_eval.models import TrackedOpenRouterModel

            model = TrackedOpenRouterModel(**merged["model"])
        else:
            model = get_model(config=merged["model"])

        class ModelProxy:
            def __init__(self):
                # Every reply, including the ones a FormatError keeps out of agent.messages.
                self.replies = []

            def query(self, messages):
                runtime.context_sent(messages)
                try:
                    message = model.query(messages)
                except FormatError as error:
                    reply = format_error_reply(error)
                    self.replies.append(reply)
                    runtime.log.emit(
                        "model_result",
                        session_id=runtime.session.id,
                        cost_usd=reply["extra"].get("cost"),
                        message=reply,
                        format_error=True,
                    )
                    raise
                self.replies.append(message)
                runtime.log.emit(
                    "model_result",
                    session_id=runtime.session.id,
                    cost_usd=message.get("extra", {}).get("cost"),
                    message=message,
                )
                return message

            def __getattr__(self, name):
                return getattr(model, name)

        class InstrumentedAgent(DefaultAgent):
            def execute_actions(self, message):
                outputs = [self.env.execute(action) for action in message.get("extra", {}).get("actions", [])]
                observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
                runtime.observation(observations)
                return self.add_messages(*observations)

        # The built-in shell environment already defines useful display settings; keep them container-local.
        runtime.env.action_env = {k: str(v) for k, v in environment_vars.items()}
        agent_kwargs = dict(merged["agent"])
        # Template variables, never template text: memory files are data, not Jinja.
        agent_kwargs["system_template"] = agent_kwargs["system_template"].rstrip("\n") + "\n\n{{memory_instructions}}"
        agent_kwargs["instance_template"] = "{{memory_reminder}}\n\n" + agent_kwargs["instance_template"]
        variables = {"memory_instructions": instructions(config.memory), "memory_reminder": memory_context}
        agent_kwargs.update(
            step_limit=session.max_steps,
            cost_limit=config.cost_limit_usd,
            wall_time_limit_seconds=config.wall_time_limit_seconds,
            output_path=None,
        )
        proxy = ModelProxy()
        agent = InstrumentedAgent(proxy, EnvironmentProxy(), **agent_kwargs)
        info = {}
        try:
            info = agent.run(instruction, **variables)
        finally:
            runtime.agent_trajectory = agent.serialize()
            runtime.partial_agent_result = {"steps": agent.n_calls, "cost_usd": agent.cost}
            runtime.initial_messages = agent.messages[:2]
        status = info.get("exit_status", "unknown")
        return {
            "exit_status": status,
            "steps": agent.n_calls,
            "cost_usd": agent.cost,
            "final": final_text(proxy.replies),
            "messages": agent.messages,
        }
