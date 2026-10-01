"""Sereno command line.

sereno run <scenario> --scripted            replay the scenario's correct solution, free
sereno run <scenario> --model glm53 [--watch]
sereno watch [<events.jsonl> | --latest] [--marker REGEX]
"""

import argparse
import sys
import threading
from pathlib import Path

MODELS = {
    "glm53": ("z-ai/glm-5.3", "baidu/fp8"),
}


def _latest_log() -> Path:
    from sereno.runner import RUNS_DIR

    logs = sorted(RUNS_DIR.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"no event logs under {RUNS_DIR}")
    return logs[-1]


def _cmd_run(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from sereno.model import OpenRouterModel, ScriptedModel
    from sereno.runner import REPO, RUNS_DIR, new_run_id, run_scenario
    from sereno.scenarios import SCENARIOS

    scenario = SCENARIOS[args.scenario]
    if args.scripted:
        from importlib import import_module

        solution = import_module(f"sereno.scenarios.{scenario.name}").SOLUTION
        model, label = ScriptedModel(solution), "scripted"
    else:
        load_dotenv(REPO / ".env")
        name, provider = MODELS[args.model]
        model, label = OpenRouterModel(name, provider, temperature=args.temperature), args.model
    run_id = new_run_id(scenario.name, label)
    log_path = RUNS_DIR / run_id / "events.jsonl"

    outcome: dict = {}

    def work() -> None:
        try:
            outcome["result"], outcome["checks"] = run_scenario(scenario, model, log_path, run_id, args.max_steps)
        except BaseException as e:
            outcome["error"] = e

    if args.watch:
        from sereno.viewer import run_viewer

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while not log_path.exists() and thread.is_alive():
            thread.join(0.05)
        run_viewer(log_path, follow=True)
        thread.join()
    else:
        work()

    if "error" in outcome:
        raise outcome["error"]
    result, checks = outcome["result"], outcome["checks"]
    print(
        f"run {run_id}: {result.reason}, {result.model_calls} model calls, {result.tool_calls} tool calls, "
        f"USD {result.cost_usd:.6f}"
    )
    for check, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {check}")
    print(f"log: {log_path}")
    return 0 if all(checks.values()) else 1


def _cmd_watch(args: argparse.Namespace) -> int:
    from sereno.viewer import run_viewer

    path = _latest_log() if args.latest or args.path is None else args.path
    run_viewer(path, follow=not args.no_follow, marker=args.marker)
    return 0


def main() -> None:
    from sereno.scenarios import SCENARIOS

    parser = argparse.ArgumentParser(
        prog="sereno", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one scenario")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--scripted", action="store_true", help="replay the correct solution, no API calls")
    source.add_argument("--model", choices=sorted(MODELS), help="paid run on OpenRouter")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--watch", action="store_true", help="open the live viewer while the run goes")

    watch = sub.add_parser("watch", help="open the live viewer on an event log")
    watch.add_argument("path", nargs="?", type=Path)
    watch.add_argument("--latest", action="store_true")
    watch.add_argument("--no-follow", action="store_true")
    watch.add_argument("--marker", help="regex to highlight, e.g. a poison marker")

    args = parser.parse_args()
    sys.exit(_cmd_run(args) if args.command == "run" else _cmd_watch(args))
