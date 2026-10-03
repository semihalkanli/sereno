"""Run one chain end to end, session by session, and write its event log.

The world carries over from one session to the next; the conversation does
not. Each session sets the clock, applies its outside changes, replays its
history (calls in it run on the world), then runs the user's turns. A chain
with memory also gets the user file and the memory index as a system-reminder
at every session start; checkpoints and compaction come later (plan phase 3).
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sereno.chain import LoadedChain, Message, apply_change
from sereno.checks import grade, is_task_group
from sereno.events import EventLog
from sereno.loop import SessionResult, output_messages, run_session, run_tool_call
from sereno.memory import session_reminder
from sereno.tools import Toolset

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "runs" / "agent"


@dataclass
class ChainResult:
    sessions: list[SessionResult] = field(default_factory=list)
    checks: dict[str, dict[str, bool]] = field(default_factory=dict)
    """Group ("s1", ..., "final", "attack") -> check name -> passed."""
    timing: dict[str, str | bool | None] = field(default_factory=dict)
    """For an attack run: `poison_seen`, `first_success` (session ids or None) and `early`."""

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
        return all(all(c.values()) for group, c in self.checks.items() if is_task_group(group))


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
            log.emit("input", content=entry.content, prefilled=True)
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
        message = {"role": "assistant", "content": entry.content, **({"tool_calls": calls} if calls else {})}
        messages.append(message)
        log.emit("chat", prefilled=True, **{"gen_ai.output.messages": output_messages(message)})
        messages.extend(run_tool_call(toolset, call, log, None, prefilled=True) for call in calls)
    return messages


def run_chain(
    loaded: LoadedChain,
    make_model,
    log_path: Path,
    run_id: str,
    max_steps: int = 30,
    until: str | None = None,
) -> ChainResult:
    """Run the chain's sessions in order.

    `make_model(session_id)` returns the model for one session, so a scripted
    model can replay a per-session solution. `until` stops after that session;
    the chain's final checks are then skipped, as they assume every session ran.
    """
    chain = loaded.chain
    world = loaded.initial_world()
    start = world.copy()
    started = time.monotonic()
    result = ChainResult()

    def score(group: str, specs, before, **extra) -> None:
        checks = grade(specs, before, world)
        result.checks[group] = checks
        log.emit("score", group=group, checks=checks, passed=all(checks.values()), **extra)

    attack = loaded.attack
    seen = success = None

    if until is not None:
        loaded.check_until(until)
    user_file = chain.user_file
    first = make_model(chain.sessions[0].id)
    with (
        EventLog(log_path, run_id) as log,
        log.span(
            "run",
            run_id,
            chain=chain.id,
            sessions=len(chain.sessions),
            attack=loaded.attack.id if loaded.attack else None,
            marker=loaded.attack.marker if loaded.attack else None,
            seed=loaded.seed,
            poison=loaded.placement(),
            trigger=loaded.attack.trigger.model_dump() if loaded.attack and loaded.attack.trigger else None,
            max_steps=max_steps,
            git=git_state(),
            pid=os.getpid(),
            **{
                "gen_ai.request.model": first.name,
                "gen_ai.provider.name": "openrouter" if first.provider else None,
                "sereno.upstream_provider": first.provider,
            },
        ) as run_end,
    ):
        for number, session in enumerate(chain.sessions, start=1):
            model = first if number == 1 else make_model(session.id)
            if session.now < world.now:
                raise ValueError(f"session {session.id} starts at {session.now}, before the world clock {world.now}")
            world.advance_to(session.now)
            for change in session.changes:
                apply_change(world, change)
            if session.user_file is not None:
                user_file = session.user_file
            toolset = Toolset(world, loaded.tools(world))
            system_prompt = loaded.system_prompt(world)
            with log.span(
                "session",
                session.id,
                session=number,
                session_id=session.id,
                date=world.today.isoformat(),
                owner=world.owner.model_dump(),
                changes=[c.model_dump(exclude_none=True) for c in session.changes],
                specs=session.specs,
                **{"gen_ai.system_instructions": system_prompt, "gen_ai.tool.definitions": toolset.schemas()},
            ) as session_end:
                log.emit("state", reason="initial" if number == 1 else "session_start", snapshot=world.snapshot())
                pre = world.copy()
                messages = [{"role": "system", "content": system_prompt}]
                if chain.memory:
                    reminder = session_reminder(user_file, world.app("memory"))
                    messages.append({"role": "user", "content": reminder})
                    log.emit("input", content=reminder, harness=True)
                messages.extend(_history(session.history, toolset, log))
                outcome = run_session(model, toolset, messages, session.turns, log, max_steps=max_steps)
                result.sessions.append(outcome)
                score(session.id, session.checks, pre)
                if attack and seen is None and attack.marker in json.dumps([outcome.messages, toolset.schemas()]):
                    seen = session.id
                if attack and attack.checks and success is None and all(grade(attack.checks, start, world).values()):
                    success = session.id
                session_end.update(reason=outcome.reason, final_text=outcome.final_text)
            if session.id == until:
                break

        if chain.final_checks and until is None:
            score("final", chain.final_checks, start)
        if attack and attack.checks:
            trigger = attack.trigger
            early = None
            if trigger:
                early = success is not None and loaded.session_number(success) < loaded.session_number(trigger.session)
            result.timing = {"poison_seen": seen, "first_success": success, "early": early}
            score("attack", attack.checks, start, **result.timing)
        run_end.update(
            model_calls=result.model_calls,
            tool_calls=result.tool_calls,
            duration_s=round(time.monotonic() - started, 3),
            **{"sereno.cost_usd": round(result.cost_usd, 8)},
        )
    return result
