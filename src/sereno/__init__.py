"""Sereno command line.

sereno run <chain> --scripted                 replay the chain's correct solution, free
sereno run <chain> --model glm53|glm53flash [--watch] [--attack ID] [--twin ID] [--repeats K] [--seed N]
                                              sampling stays at the provider's defaults
sereno watch                                  agent view: every run under runs/agent, live
sereno watch <events.jsonl> | --latest [--marker REGEX]   one run's transcript
"""

import argparse
import sys
import threading
from pathlib import Path

MODELS = {
    "glm53": ("z-ai/glm-5.3", "z-ai"),
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


def _print_answers(result) -> None:
    for session, signals in result.timing.get("answer_signals", {}).items():
        shown = ", ".join(f"{n}={'yes' if v else 'no'}" for n, v in signals.items()) if signals else "no answer"
        print(f"  {session} answer: {shown}")


def _placement(loaded) -> str:
    """Where the attack's poison lands in this run, e.g. 'hotel_review@s2'."""
    return ", ".join(f"{slot}@{session}" for slot, session in loaded.placement().items())


def _print_base_rate(runs, twin: str | None = None) -> None:
    """Per attack of the chain, how many complete clean runs reached its goal by themselves; its checks the world
    cannot grade are left out and named. On a twin's world acting on outside content can be right, so its runs are
    not named clean."""
    k = len(runs)
    kind = f"runs with twin {twin}" if twin else "clean runs"
    print(f"base rate over k={k} {kind}, attack goals reached without an attack:")
    for attack_id, entry in runs[0].base_rate.items():
        skipped = f", skipped {', '.join(entry['skipped'])} (needs the attack's fill)" if entry["skipped"] else ""
        if not entry["checks"]:
            print(f"  -    {attack_id}  not graded{skipped}")
            continue
        reached = sum(r.base_rate[attack_id]["first_success"] is not None for r in runs)
        print(f"  {reached}/{k}  {attack_id}  goal reached{skipped}")


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
    if loaded.poison:
        print(f"  seed {loaded.seed}, poison at {_placement(loaded)}")
    if result.invalid:
        print(f"  INVALID: session {result.stopped_at} ended in {result.reason}, the chain stopped there")
    _print_checks(result)
    _print_answers(result)
    print(f"log: {log_path}")
    return result


def _cmd_run(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from sereno.chain import load_chain
    from sereno.model import OpenRouterModel, ScriptedModel
    from sereno.runner import REPO, new_run_id

    if args.watch and args.repeats > 1:
        sys.exit("--watch runs one repeat; use 'sereno watch --latest' alongside repeats")
    loads = [
        load_chain(
            args.chain,
            args.attack,
            seed=args.seed + i,
            twin_id=args.twin,
            attack_file=getattr(args, "attack_file", None),
        )
        for i in range(args.repeats)
    ]
    loaded = loads[0]
    if args.until:
        try:
            for each in loads:
                each.check_until(args.until)
        except ValueError as e:
            sys.exit(str(e))
    if args.scripted:
        if loaded.solution is None:
            sys.exit(f"chain {args.chain} has no solution.json")
        label = "scripted"

        def make_model(session_id: str):
            return ScriptedModel(loaded.solution[session_id])
    else:
        load_dotenv(REPO / ".env")
        name, provider = MODELS[args.model]
        label = args.model
        model = OpenRouterModel(name, provider)

        def make_model(session_id: str):
            return model

    if args.twin:
        label = f"{label}_{args.twin}"
    if loaded.attack:
        label = f"{label}_{loaded.attack.id}"
    if args.until:
        label = f"{label}_until-{args.until}"
    base_id = new_run_id(args.chain, label)
    runs = []
    run_ids = []
    for i, each in enumerate(loads, start=1):
        run_id = base_id if args.repeats == 1 else f"{base_id}_r{i}"
        run_ids.append(run_id)
        runs.append(_run_once(each, make_model, run_id, args))

    if args.repeats > 1:
        complete = [(each, r) for each, r in zip(loads, runs, strict=True) if not r.invalid]
        k = len(complete)
        passed = sum(r.task_passed() for _, r in complete)
        print(
            f"\nsummary over k={len(runs)}: {len(runs) - k} invalid, rates over the {k} complete runs; "
            f"{passed}/{k} runs passed every task check, "
            f"USD {sum(r.cost_usd for r in runs):.6f}, model calls {[r.model_calls for r in runs]}"
        )
        for run_id, r in zip(run_ids, runs, strict=True):
            if r.invalid:
                print(f"  invalid  {run_id}: session {r.stopped_at} ended in {r.reason}, needs a rerun")
        for group, checks in complete[0][1].checks.items() if complete else []:
            for check in checks:
                print(f"  {sum(r.checks[group][check] for _, r in complete)}/{k}  {group}/{check}")
        if loaded.attack and loaded.attack.checks:
            by_place: dict[str, list] = {}
            for each, r in complete:
                by_place.setdefault(_placement(each), []).append(r)
            print("attack by placement:")
            for place, group in sorted(by_place.items()):
                for check in group[0].checks["attack"] if loaded.attack.one_of else []:
                    print(f"  {sum(r.checks['attack'][check] for r in group)}/{len(group)}  {place}  attack/{check}")
                reached = [r for r in group if r.timing.get("first_success")]
                line = f"  {len(reached)}/{len(group)}  {place}  goal reached"
                if loaded.attack.trigger:
                    line += f", {sum(bool(r.timing['early']) for r in reached)} of them before the trigger"
                carried = sum(r.timing["carried"] is True for r in reached)
                forgot = sum(r.timing["carried"] is False for r in reached)
                line += f", {carried} of them carried from an earlier session, {forgot} without it"
                print(line)
                for app in sorted({e.split(":")[1] for r in group for e in r.timing.get("poison_copied", [])}):
                    copied = sum(any(e.endswith(f":{app}") for e in r.timing["poison_copied"]) for r in group)
                    print(f"  {copied}/{len(group)}  {place}  poison copied into {app} by the agent")
                if "poison_in_memory" in group[0].timing:
                    kept = sum(bool(r.timing["poison_in_memory"]) for r in group)
                    print(f"  {kept}/{len(group)}  {place}  poison in memory at a session end")
                for session in group[0].timing.get("answer_signals", {}):
                    answers = [r.timing["answer_signals"][session] for r in group]
                    for name in ["told_user", *(s.name for s in loaded.attack.answer_signals)]:
                        shown = sum(bool(a and a[name]) for a in answers)
                        print(f"  {shown}/{len(group)}  {place}  {session} answer: {name}")
                    missing = sum(a is None for a in answers)
                    if missing:
                        print(f"  {missing}/{len(group)}  {place}  {session} ended without an answer")
    valid = [r for r in runs if not r.invalid]
    if loaded.attack is None and valid and valid[0].base_rate:
        _print_base_rate(valid, loaded.twin.id if loaded.twin else None)
    return 0 if all(not r.invalid and r.task_passed() for r in runs) else 1


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
    attack_source = run.add_mutually_exclusive_group()
    attack_source.add_argument("--attack-file", type=Path, help="external declarative attack JSON")
    attack_source.add_argument(
        "--attack", help="attack id from chains/<chain>/attacks/; without it slots get their defaults"
    )
    run.add_argument("--twin", help="benign twin id from chains/<chain>/twins/: a legitimate outside change")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--scripted", action="store_true", help="replay the correct solution, no API calls")
    source.add_argument("--model", choices=sorted(MODELS), help="paid run on OpenRouter")
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--watch", action="store_true", help="open the live viewer while the run goes")
    run.add_argument("--until", help="stop after this session id (final checks are skipped)")
    run.add_argument("--repeats", type=int, default=1, help="run the chain k times and summarise")
    run.add_argument("--seed", type=int, default=0, help="seed of the first repeat; repeat i uses seed + i - 1")

    watch = sub.add_parser("watch", help="open the agent view, or one run's live transcript")
    watch.add_argument("path", nargs="?", type=Path)
    watch.add_argument("--latest", action="store_true")
    watch.add_argument("--no-follow", action="store_true")
    watch.add_argument("--marker", help="regex to highlight, e.g. a poison marker")

    inspect = sub.add_parser("inspect", help="list scenario sessions, records, slots and tool surfaces as JSON")
    inspect.add_argument("chain", choices=chain_ids())

    args = parser.parse_args()
    if args.command == "inspect":
        import json

        from sereno.chain import load_chain
        from sereno.experiments import surfaces

        print(json.dumps(surfaces(load_chain(args.chain)), indent=2, ensure_ascii=False))
        return
    sys.exit(_cmd_run(args) if args.command == "run" else _cmd_watch(args))
