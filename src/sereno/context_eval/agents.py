"""mini-swe-agent adapter and a deterministic fixture oracle using the same runtime hooks."""

import json
import time


def rendered_output(output: dict) -> dict:
    text = output["output"]
    if len(text) <= 10000:
        return {"returncode": output["returncode"], "output": text}
    return {
        "returncode": output["returncode"],
        "output_head": text[:5000],
        "output_tail": text[-5000:],
        "elided_chars": len(text) - 10000,
    }


class ScriptedAdapter:
    def run(self, runtime, instruction, memory_context, config, session):
        messages = [{"role": "user", "content": instruction + "\n" + memory_context}]
        started = time.monotonic()
        steps, final = 0, ""
        exit_status = "script_complete"
        for action in session.script:
            if steps >= session.max_steps or time.monotonic() - started >= config.wall_time_limit_seconds:
                exit_status = "session_boundary"
                break
            seen = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") != "assistant")
            if action.if_contains is not None and action.if_contains not in seen:
                continue
            runtime.context_sent(messages)
            messages.append({"role": "assistant", "content": action.command})
            output = runtime.execute(action.command)
            steps += 1
            final = runtime.last_output
            if output["submitted"]:
                exit_status = "Submitted"
                break
            observation = [{"role": "user", "content": json.dumps(rendered_output(output), ensure_ascii=False)}]
            runtime.observation(observation)
            messages.extend(observation)
        return {"exit_status": exit_status, "steps": steps, "cost_usd": 0.0, "final": final, "messages": messages}


class MiniSweAdapter:
    def run(self, runtime, instruction, memory_context, config, session):
        import yaml
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.config import builtin_config_dir
        from minisweagent.exceptions import Submitted
        from minisweagent.models import get_model
        from minisweagent.utils.serialize import recursive_merge

        merged = recursive_merge(
            yaml.safe_load((builtin_config_dir / "mini.yaml").read_text()),
            yaml.safe_load(config.model_config_file.read_text()),
        )
        merged["model"]["cost_tracking"] = "default"
        # env settings from model configs must not forward host credentials or mount host paths.
        environment_vars = merged.get("environment", {}).get("env", {})

        class EnvironmentProxy:
            def execute(self, action, **kwargs):
                output = runtime.execute(action["command"])
                if output["submitted"]:
                    final = "\n".join(runtime.last_output.lstrip().splitlines()[1:])
                    raise Submitted(
                        {"role": "exit", "content": final, "extra": {"exit_status": "Submitted", "submission": final}}
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
            def query(self, messages):
                runtime.context_sent(messages)
                message = model.query(messages)
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
        agent_kwargs.update(
            step_limit=session.max_steps,
            cost_limit=config.cost_limit_usd,
            wall_time_limit_seconds=config.wall_time_limit_seconds,
            output_path=None,
        )
        agent = InstrumentedAgent(ModelProxy(), EnvironmentProxy(), **agent_kwargs)
        info = {}
        try:
            info = agent.run(instruction + "\n\n" + memory_context)
        finally:
            runtime.agent_trajectory = agent.serialize()
            runtime.partial_agent_result = {"steps": agent.n_calls, "cost_usd": agent.cost}
        status = info.get("exit_status", "unknown")
        return {
            "exit_status": status,
            "steps": agent.n_calls,
            "cost_usd": agent.cost,
            "final": info.get("submission", runtime.last_output),
            "messages": agent.messages,
        }
