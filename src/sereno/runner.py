"""Run one scenario end to end and write its event log."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sereno.events import EventLog
from sereno.loop import SessionResult, run_session
from sereno.model import ChatModel
from sereno.scenarios import Scenario
from sereno.tools import Toolset
from sereno.world import TOOLS

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "runs" / "agent"


def git_state() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def new_run_id(scenario: str, label: str) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{scenario}_{label}"


def run_scenario(
    scenario: Scenario, model: ChatModel, log_path: Path, run_id: str, max_steps: int = 30
) -> tuple[SessionResult, dict[str, bool]]:
    world = scenario.initial_world()
    pre = world.model_copy(deep=True)
    toolset = Toolset(world, TOOLS)
    system_prompt = scenario.system_prompt(world)

    with EventLog(log_path, run_id) as log:
        log.emit(
            "run_start",
            scenario=scenario.name,
            model=model.name,
            provider=model.provider,
            temperature=model.temperature,
            max_steps=max_steps,
            git=git_state(),
        )
        log.emit(
            "session_start",
            date=world.today.isoformat(),
            owner=world.owner.model_dump(),
            tools=list(toolset.tools),
            system_prompt=system_prompt,
        )
        log.emit("world_state", reason="initial", state=world.snapshot())
        result = run_session(model, toolset, system_prompt, scenario.prompt, log, max_steps=max_steps)
        checks = scenario.grade(pre, world, result.final_text)
        log.emit("grade", checks=checks, passed=all(checks.values()))
        log.emit(
            "run_end", cost_usd=round(result.cost_usd, 8), model_calls=result.model_calls, tool_calls=result.tool_calls
        )
    return result, checks
