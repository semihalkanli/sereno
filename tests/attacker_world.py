"""Shared fixtures for the attacker runtime tests: one protocol 1.0 request and scripted provider turns.

The strings here are inert fixture values, in the style of `tests/run_attacker_stub.py`; none is an instruction.
"""

import itertools
import json
from pathlib import Path
from typing import Literal

import yaml

from sereno.attacker.config import AttackerConfig, AttackerSettings, HttpSettings, LoopSettings, ProviderSettings
from sereno.attacker.providers.base import Completion, ToolCall

PROMPT = "Fixture operator brief: use the available tools."
INERT = "ATTACKER_AGENT_FIXTURE inert marker\n"
MEMORY_ROOT = "/root/.claude/projects/{project}/memory"
NOTE = f"{MEMORY_ROOT}/fixture-note.md"
CODE = "def alpha():\n    return 1\n"
_ids = itertools.count(1)


def make_config(
    tmp_path: Path,
    *,
    kind: Literal["openai-compatible", "anthropic"] = "openai-compatible",
    model: str = "fixture-model",
    base_url: str = "https://example.test/v1",
    request: dict | None = None,
    credential_env: str | None = None,
    deadline: int = 60,
    agent: LoopSettings | None = None,
    user_agent: str | None = None,
    session_header: str | None = None,
) -> AttackerConfig:
    prompt_path = tmp_path / "brief.md"
    prompt_path.write_text(PROMPT)
    settings = AttackerSettings(
        schema_version="1.0",
        prompt_file=prompt_path,
        objective="",
        provider=ProviderSettings(
            kind=kind,
            model=model,
            base_url=base_url,
            credential_env=credential_env,
            request=request or {},
            user_agent=user_agent,
            session_header=session_header,
        ),
        http=HttpSettings(attempts=1),
        agent=agent or LoopSettings(),
        deadline_seconds=deadline,
    )
    return AttackerConfig(path=tmp_path / "attacker.yaml", settings=settings, prompt=PROMPT)


def make_request(**contract) -> dict:
    base_contract = {
        "kind": "in_task",
        "stage": "exposure",
        "writes_open": True,
        "project": "fixture",
        "memory_root": MEMORY_ROOT,
        "limits": {"max_files_touched": 3, "max_file_bytes": 20000},
        "budget": {
            "max_calls": 10,
            "max_calls_per_session": 10,
            "max_tokens": 100000,
            "max_seconds": 600,
            "max_writes": 3,
        },
        "used": {
            "calls": 0,
            "tokens": 0,
            "seconds": 0.0,
            "writes": 0,
            "cost_usd": 0.0,
            "unreported_usage_calls": 0,
            "skipped_calls": 0,
        },
        "session_call": 1,
        "timeout_seconds": 300,
        "strategy_max_bytes": 20000,
        "notebook_max_bytes": 20000,
        "code_root": "/app",
        "max_reads_per_call": 10,
        "max_read_bytes": 200000,
        "response_keys": sorted(
            [
                "schema_version",
                "actions",
                "strategy",
                "notebook",
                "reads",
                "writer_usage",
                "status",
                "loop_ending",
                "declined_after_actions",
            ]
        ),
    }
    return {
        "schema_version": "1.0",
        "contract": base_contract | contract,
        "strategy": {"version": 0, "text": "start", "sha256": "x"},
        "notebook": "",
        "observation": {
            "session": {"id": "exposure", "task_id": "fixture", "instruction": "do the task", "step": 1},
            "code_index": {
                "root": "/app",
                "status": "ok",
                "entries": 3,
                "files": ["/app/README.md", "/app/pkg/core.py", "/app/tests/test_core.py"],
            },
            "code_views": {"/app/pkg/core.py": {"status": "ok", "bytes": len(CODE), "text": CODE}},
            "requested_files": {},
            "transcript": [
                {"role": "user", "content": "fixture task"},
                {
                    "role": "assistant",
                    "content": "fixture answer",
                    "tool_calls": [{"function": {"name": "list_files", "arguments": "{}"}}],
                },
                {"role": "tool", "content": "fixture tool result", "tool_call_id": "call-1"},
            ],
            "completed_sessions": [
                {
                    "session_id": "earlier",
                    "task_id": "previous",
                    "stage": "exposure",
                    "instruction": "previous task",
                    "transcript": [
                        {"role": "user", "content": "earlier task"},
                        {"role": "assistant", "content": "earlier answer"},
                    ],
                    "summary": "earlier summary",
                    "calls": 2,
                }
            ],
            "memory": {"files": {NOTE: "carried fixture line\n"}, "owned": []},
            "previous_result": None,
            "session_summary": None,
        },
    }


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(f"call_{next(_ids)}", name, json.dumps(arguments, ensure_ascii=False))


def completion(*calls: ToolCall, text: str = "", usage=None, reasoning=None, finish: str = "tool_calls") -> Completion:
    return Completion(
        text=text,
        tool_calls=tuple(calls),
        reasoning=reasoning or {},
        finish_reason=finish,
        usage=usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


class ScriptedProvider:
    name = "scripted"

    def __init__(self, *completions: Completion):
        self.completions = list(completions)
        self.turns: list[list] = []

    def complete(self, turns, tools, call=1):
        self.turns.append(list(turns))
        if not self.completions:
            raise AssertionError("the loop asked for more completions than the script has")
        return self.completions.pop(0)

    def close(self) -> None:
        pass


def write_config(
    tmp_path: Path, *, deadline: int = 60, provider: dict | None = None, agent: dict | None = None
) -> Path:
    (tmp_path / "brief.md").write_text(PROMPT)
    data = {
        "schema_version": "1.0",
        "prompt_file": "brief.md",
        "objective": "",
        "provider": {
            "kind": "openai-compatible",
            "model": "fixture-model",
            "base_url": "https://example.test/v1",
            "credential_env": None,
        }
        | (provider or {}),
        "agent": agent or {},
        "deadline_seconds": deadline,
    }
    path = tmp_path / "attacker.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path
