"""Run one chain end to end, session by session, and write its event log.

The world carries over from one session to the next; the conversation does
not. Each session sets the clock, applies its outside changes, replays its
history (calls in it run on the world), then runs the user's turns. Memory,
checkpoints and compaction come later (plan phase 3).
"""

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sereno.chain import LoadedChain, Message, apply_change
from sereno.checks import grade
from sereno.events import EventLog
from sereno.loop import SessionResult, run_session, run_tool_call
from sereno.tools import Toolset

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "runs" / "agent"


@dataclass
class ChainResult:
    sessions: list[SessionResult] = field(default_factory=list)
    checks: dict[str, dict[str, bool]] = field(default_factory=dict)
    """Group ("s1", ..., "final", "attack") -> check name -> passed."""

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.sessions)

    @property
    def model_calls(self) -> int:
        return sum(s.model_calls for s in self.sessions)

    @property
    def tool_calls(self) -> int:
        return sum(s.tool_calls for s in self.sessions)

    @property
    def reason(self) -> str:
        return self.sessions[-1].reason if self.sessions else "not_run"

    def task_passed(self) -> bool:
        return all(all(c.values()) for group, c in self.checks.items() if group != "attack")


def git_state() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def new_run_id(chain: str, label: str) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{chain}_{label}"


def _history(history: list[Message], toolset: Toolset, log: EventLog) -> list[dict]:
    messages = []
    n = 0
    for entry in history:
        if entry.role == "user":
            messages.append({"role": "user", "content": entry.content})
            log.emit("user_message", text=entry.content, prefilled=True)
            continue
        calls = []
        for call in entry.calls:
            n += 1
            calls.append(
                {
                    "id": f"hist-{n}",
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.args)},
                }
            )
        messages.append({"role": "assistant", "content": entry.content, **({"tool_calls": calls} if calls else {})})
        log.emit(
            "model_response",
            text=entry.content,
            tool_calls=[
                {"id": c["id"], "name": c["function"]["name"], "args": c["function"]["arguments"]} for c in calls
            ],
            prefilled=True,
        )
        messages.extend(run_tool_call(toolset, call, log, None, prefilled=True) for call in calls)
    return messages


def run_chain(
    loaded: LoadedChain,
    make_model,
    log_path: Path,
    run_id: str,
    max_steps: int = 30,
    sessions: list[str] | None = None,
) -> ChainResult:
    """Run the chain's sessions (all, or only the ids in `sessions`, in chain order).

    `make_model(session_id)` returns the model for one session, so a scripted
    model can replay a per-session solution.
    """
    chain = loaded.chain
    world = loaded.initial_world()
    start = world.copy()
    result = ChainResult()
    model = None

    with EventLog(log_path, run_id) as log:
        for number, session in enumerate(chain.sessions, start=1):
            if sessions is not None and session.id not in sessions:
                continue
            model = make_model(session.id)
            log.session, log.turn = number, 1
            if number == 1 or (sessions is not None and session.id == sessions[0]):
                log.emit(
                    "run_start",
                    scenario=chain.id,
                    chain=chain.id,
                    attack=loaded.attack.id if loaded.attack else None,
                    marker=loaded.attack.marker if loaded.attack else None,
                    model=model.name,
                    provider=model.provider,
                    temperature=model.temperature,
                    max_steps=max_steps,
                    git=git_state(),
                )
            if session.now < world.now:
                raise ValueError(f"session {session.id} starts at {session.now}, before the world clock {world.now}")
            world.now = session.now
            for change in session.changes:
                apply_change(world, change)
            toolset = Toolset(world, world.tools())
            system_prompt = loaded.system_prompt(world)
            log.emit(
                "session_start",
                session_id=session.id,
                date=world.today.isoformat(),
                owner=world.owner.model_dump(),
                tools=list(toolset.tools),
                system_prompt=system_prompt,
                changes=[c.model_dump(exclude_none=True) for c in session.changes],
            )
            log.emit("world_state", reason="initial" if number == 1 else "session_start", state=world.snapshot())
            pre = world.copy()
            messages = [{"role": "system", "content": system_prompt}, *_history(session.history, toolset, log)]
            outcome = run_session(model, toolset, messages, session.turns, log, max_steps=max_steps)
            result.sessions.append(outcome)
            checks = grade(session.checks, pre, world)
            result.checks[session.id] = checks
            log.emit("grade", session_id=session.id, group=session.id, checks=checks, passed=all(checks.values()))

        if sessions is None:
            if chain.final_checks:
                checks = grade(chain.final_checks, start, world)
                result.checks["final"] = checks
                log.emit("grade", group="final", checks=checks, passed=all(checks.values()))
            if loaded.attack and loaded.attack.checks:
                checks = grade(loaded.attack.checks, start, world)
                result.checks["attack"] = checks
                log.emit("grade", group="attack", checks=checks, passed=all(checks.values()))
        log.emit(
            "run_end",
            cost_usd=round(result.cost_usd, 8),
            model_calls=result.model_calls,
            tool_calls=result.tool_calls,
        )
    return result
