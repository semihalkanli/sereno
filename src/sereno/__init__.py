"""Sereno command line.

sereno run <chain> --scripted                 replay the chain's correct solution, free
sereno run <chain> --model glm53|glm53flash [--watch] [--attack ID] [--repeats K]
                  [--temperature T] [--top-p P] [--reasoning-effort LEVEL]
sereno watch                                  agent view: every run under runs/agent, live
sereno watch <events.jsonl> | --latest [--marker REGEX]   one run's transcript
"""

import argparse
import sys
import threading
from pathlib import Path

MODELS = {
    "glm53": ("z-ai/glm-5.3", "baidu/fp8"),
    "glm53flash": ("z-ai/glm-5.3-flash", "z-ai"),
}


def _latest_log() -> Path:
    from sereno.runner import RUNS_DIR

    logs = sorted(RUNS_DIR.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"no event logs under {RUNS_DIR}")
    return logs[-1]


def _print_checks(result) -> None:
    for group, checks in result.checks.items():
        for check, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {group}/{check}")


def _run_once(loaded, make_model, run_id: str, args: argparse.Namespace):
    from sereno.runner import RUNS_DIR, run_chain

    log_path = RUNS_DIR / run_id / "events.jsonl"
    outcome: dict = {}

    def work() -> None:
        try:
            outcome["result"] = run_chain(loaded, make_model, log_path, run_id, args.max_steps, args.until)
        except BaseException as e:
            outcome["error"] = e

    if args.watch:
        from sereno.viewer import run_viewer

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while not log_path.exists() and thread.is_alive():
            thread.join(0.05)
        run_viewer(log_path, follow=True, marker=loaded.attack.marker if loaded.attack else None)
        thread.join()
    else:
        work()

    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    print(
        f"run {run_id}: {result.reason}, {len(result.sessions)} sessions, {result.model_calls} model calls, "
        f"{result.tool_calls} tool calls, USD {result.cost_usd:.6f}"
    )
    _print_checks(result)
    print(f"log: {log_path}")
    return result


def _cmd_run(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from sereno.chain import load_chain
    from sereno.model import OpenRouterModel, ScriptedModel
    from sereno.runner import REPO, new_run_id

    if args.watch and args.repeats > 1:
        sys.exit("--watch runs one repeat; use 'sereno watch --latest' alongside repeats")
    loaded = load_chain(args.chain, args.attack)
    if args.until and args.until not in [s.id for s in loaded.chain.sessions]:
        sys.exit(f"chain {args.chain} has no session {args.until!r}")
    if args.scripted:
        if loaded.solution is None:
            sys.exit(f"chain {args.chain} has no solution.json")
        label = "scripted"

        def make_model(session_id: str):
            return ScriptedModel(loaded.solution[session_id])
    else:
        load_dotenv(REPO / ".env")
        name, provider = MODELS[args.model]
        sampling = [
            f"t{args.temperature:g}" if args.temperature is not None else "",
            f"p{args.top_p:g}" if args.top_p is not None else "",
        ]
        label = f"{args.model}_{'_'.join(x for x in sampling if x) or 'default'}"
        if args.reasoning_effort:
            label = f"{label}_{args.reasoning_effort}"
        model = OpenRouterModel(
            name, provider, temperature=args.temperature, top_p=args.top_p, reasoning_effort=args.reasoning_effort
        )

        def make_model(session_id: str):
            return model

    if args.attack:
        label = f"{label}_{args.attack}"
    if args.until:
        label = f"{label}_until-{args.until}"
    base_id = new_run_id(args.chain, label)
    runs = []
    for i in range(1, args.repeats + 1):
        run_id = base_id if args.repeats == 1 else f"{base_id}_r{i}"
        runs.append(_run_once(loaded, make_model, run_id, args))

    if args.repeats > 1:
        k = len(runs)
        passed = sum(r.task_passed() for r in runs)
        print(
            f"\nsummary over k={k}: {passed}/{k} runs passed every task check, "
            f"USD {sum(r.cost_usd for r in runs):.6f}, model calls {[r.model_calls for r in runs]}"
        )
        for group, checks in runs[0].checks.items():
            for check in checks:
                print(f"  {sum(r.checks[group][check] for r in runs)}/{k}  {group}/{check}")
    return 0 if all(r.task_passed() for r in runs) else 1


def _cmd_watch(args: argparse.Namespace) -> int:
    from sereno.runner import RUNS_DIR
    from sereno.viewer import run_agent_view, run_viewer

    if args.path is None and not args.latest:
        run_agent_view(RUNS_DIR, marker=args.marker)
        return 0
    path = _latest_log() if args.latest else args.path
    run_viewer(path, follow=not args.no_follow, marker=args.marker)
    return 0


def main() -> None:
    from sereno.chain import chain_ids

    parser = argparse.ArgumentParser(
        prog="sereno", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one chain")
    run.add_argument("chain", choices=chain_ids())
    run.add_argument("--attack", help="attack id from chains/<chain>/attacks/; without it slots get their defaults")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--scripted", action="store_true", help="replay the correct solution, no API calls")
    source.add_argument("--model", choices=sorted(MODELS), help="paid run on OpenRouter")
    run.add_argument("--temperature", type=float, help="without it the provider's default applies")
    run.add_argument("--top-p", type=float, help="without it the provider's default applies")
    run.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="sent as reasoning.effort; without it the provider's default applies",
    )
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--watch", action="store_true", help="open the live viewer while the run goes")
    run.add_argument("--until", help="stop after this session id (final checks are skipped)")
    run.add_argument("--repeats", type=int, default=1, help="run the chain k times and summarise")

    watch = sub.add_parser("watch", help="open the agent view, or one run's live transcript")
    watch.add_argument("path", nargs="?", type=Path)
    watch.add_argument("--latest", action="store_true")
    watch.add_argument("--no-follow", action="store_true")
    watch.add_argument("--marker", help="regex to highlight, e.g. a poison marker")

    args = parser.parse_args()
    sys.exit(_cmd_run(args) if args.command == "run" else _cmd_watch(args))
