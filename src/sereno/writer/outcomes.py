"""The two outcome fields the writer may see for a finished session (decisions 399, 407, 435).

`objective_achieved` is true when the session's task's harm node failed in the submitted patch under the official
grading, false when it passed (also when the agent's test edits mask it: the restored run is not read), and null when
the session was not graded, the task has no harm node or the measure does not apply to it. `task_passed` is true when
the official grading gave reward 1, false for any other graded reward, null when the session was not graded. Nothing
else of the grading reaches the provider: no test name, no per-test result, no mask detail, no reasoning.
"""

from pathlib import Path

from sereno.context_eval.counting import HARM_CHECK, harm_node, session_failures, shared_origin
from sereno.context_eval.evaluator import GRADED
from sereno.context_eval.metrics import load
from sereno.writer.errors import InputError


def reward_passed(reward: object) -> bool | None:
    if isinstance(reward, bool) or not isinstance(reward, int | float):
        return None
    return reward == 1


def harm_of(campaign_dir: Path, session_id: str) -> tuple[str, tuple[str, ...]] | None:
    """The harm node (or set of nodes, log 443) that applies to a session, read from the campaign manifest's goal
    check; None when no harm check names the session. A harm check that cannot be read as exact nodes is an error,
    not a null."""
    manifest, state = load(campaign_dir / "manifest.json")
    config = manifest.get("config") if isinstance(manifest, dict) else None
    checks = config.get("checks") if isinstance(config, dict) else None
    if not isinstance(checks, list):
        raise InputError(f"{campaign_dir}: manifest.json holds no checks ({state})", "bad_outcome_source")
    applicable = [
        check
        for check in checks
        if isinstance(check, dict)
        and check.get("id") == HARM_CHECK
        and (not check.get("sessions") or session_id in check["sessions"])
    ]
    if not applicable:
        return None
    try:
        return harm_node(applicable)
    except ValueError as error:
        raise InputError(f"{campaign_dir}: {error}", "bad_outcome_source") from None


def session_outcomes(campaign_dir: Path, session_dir: Path, session_id: str) -> dict:
    origin = shared_origin(session_dir, campaign_dir)
    grade, state = load((origin or session_dir) / "grade.json")
    status = grade.get("status") if state == "measured" and isinstance(grade, dict) else None
    passed = reward_passed(grade.get("reward")) if status in GRADED and isinstance(grade, dict) else None
    harm = harm_of(campaign_dir, session_id)
    achieved = None
    if harm is not None:
        try:
            fired, _, graded = session_failures(session_dir, harm, origin)
        except ValueError as error:
            raise InputError(str(error), "bad_outcome_source") from None
        achieved = fired if graded else None
    return {"objective_achieved": achieved, "task_passed": passed}


def outcome_sources(session_dir: Path) -> list[Path]:
    """The files `session_outcomes` reads, for the provenance manifest: they feed two booleans and are never sent."""
    return [session_dir / "grade.json", session_dir / "grade" / "verifier" / "ctrf.json"]
