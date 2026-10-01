"""Scenarios: an initial world, a user prompt and a grader over the final world."""

from collections.abc import Callable
from dataclasses import dataclass

from sereno.scenarios import kickoff
from sereno.world import World


@dataclass(frozen=True)
class Scenario:
    name: str
    initial_world: Callable[[], World]
    system_prompt: Callable[[World], str]
    prompt: str
    grade: Callable[[World, World, str | None], dict[str, bool]]


SCENARIOS = {
    kickoff.NAME: Scenario(kickoff.NAME, kickoff.initial_world, kickoff.system_prompt, kickoff.PROMPT, kickoff.grade),
}
