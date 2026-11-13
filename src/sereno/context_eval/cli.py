"""CLI for catalogs, validated campaign matrices, pre-run dry checks, offline reports, patch export, DeepSWE grading,
SWE-bench Pro task export, gate counting and watching runs."""

import argparse
import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from sereno.context_eval import wide as wide_rule


def harm_names(pairs: list[str], file: Path | None) -> dict[str, list[str]]:
    """The wide names of the harm tests, from `--harm-name HARM_ID=WIDE_NAME` and the `--harm-names` JSON file. A pair
    that does not split at exactly one `=` into two names is refused: a test id may hold `=` itself."""
    names: dict[str, list[str]] = {}
    for pair in pairs:
        harm_id, _, name = pair.partition("=")
        if pair.count("=") != 1 or not harm_id or not name:
            raise ValueError(
                f"--harm-name {pair!r} is not HARM_ID=WIDE_NAME with exactly one '='; give an id or name that holds "
                '"=" in a --harm-names file {"HARM_ID": ["WIDE_NAME", ...]}'
            )
        names.setdefault(harm_id, []).append(name)
    if file is not None:
        try:
            data = json.loads(Path(file).read_text())
        except ValueError as error:
            raise ValueError(f"{file}: not JSON ({error})") from error
        if not isinstance(data, dict) or not all(
            isinstance(harm_id, str)
            and harm_id
            and isinstance(listed, list)
            and listed
            and all(isinstance(name, str) and name for name in listed)
            for harm_id, listed in data.items()
        ):
            raise ValueError(f'{file}: --harm-names needs a JSON object {{"HARM_ID": ["WIDE_NAME", ...]}}')
        for harm_id, listed in data.items():
            names.setdefault(harm_id, []).extend(listed)
    return names


def add_parser(parent):
    parser = parent.add_parser("context-eval", help="evaluate prompt injection and persistent memory integrity")
    commands = parser.add_subparsers(dest="context_command", required=True)
    catalog = commands.add_parser("catalog", help="list external tasks and locally available images")
    catalog.add_argument("--dataset", type=Path, required=True)
    catalog.add_argument("--language")
    catalog.add_argument("--available-only", action="store_true")
    dry = "check each replace intervention's old_text against seed memory and task images, with no model"
    for name, text in (("validate", None), ("dry-check", dry), ("run", None)):
        command = commands.add_parser(name, help=text)
        command.add_argument("config", type=Path)
        command.add_argument("--plugin", action="append", default=[], help="explicit trusted Python module")
        if name == "run":
            command.add_argument("--out", type=Path, required=True, help="campaign directory, new unless --resume")
            command.add_argument(
                "--resume", action="store_true", help="continue the interrupted campaign in --out with the same config"
            )
    report = commands.add_parser("report", help="recompute metrics from saved artifacts")
    report.add_argument("campaign", type=Path)
    summarize = commands.add_parser("summarize", help="pool several campaigns into one report")
    summarize.add_argument("campaigns", type=Path, nargs="+")
    summarize.add_argument("--out", type=Path, required=True, help="new directory for the pooled report")
    for command in (report, summarize):
        command.add_argument("--plugin", action="append", default=[])
        command.add_argument("--bootstrap", type=int, default=2000, help="cluster bootstrap replicates over targets")
        command.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    export = commands.add_parser("export", help="export one grade-ready session patch")
    export.add_argument("campaign", type=Path)
    export.add_argument("--case", required=True)
    export.add_argument(
        "--arm",
        choices=["clean", "clean_reset", "attack_carry", "attack_reset", "attack_ablate", "attack_adaptive"],
        required=True,
    )
    export.add_argument("--session", required=True)
    export.add_argument("--out", type=Path, required=True)
    grade = commands.add_parser("grade", help="grade complete session patches with DeepSWE verifiers")
    grade.add_argument("campaign", type=Path)
    grade.add_argument(
        "--workers", type=int, default=1, help="gradings at a time; with --wide at most a record's under_load.workers"
    )
    regrade = grade.add_mutually_exclusive_group()
    regrade.add_argument("--force", action="store_true", help="regrade sessions that already have grade.json")
    regrade.add_argument(
        "--test-edits-only",
        action="store_true",
        help="add only the existing-test-edit pass to grade.json files that lack it",
    )
    grade.add_argument("--dataset", type=Path, help="DeepSWE checkout (default: the campaign's dataset_root)")
    grade.add_argument("--wide", type=Path, help="directory of wide records; tasks with one also run their whole suite")
    wide = commands.add_parser(
        "wide-check",
        help="before a task is used: measure its whole-suite run on the gold patch and write its wide record",
        description="Grades the gold patch --repeats times, then --load-runs times --load-workers at a time, and the "
        "known harmful patch when given, each followed by the wide command, and writes <wide>/<task>.json with the "
        "tests that passed every time. Exit code 1 when a problem is listed or the record is unusable (`unusable`, "
        "for example measured with fewer runs than the rule of 5, 20 and 4): the record is written, but a grading "
        "refuses it until the check is run again or the problem is settled.",
    )
    wide.add_argument("--dataset", type=Path, required=True)
    wide.add_argument("--wide", type=Path, required=True, help="directory the wide record is written to")
    wide.add_argument("--out", type=Path, required=True, help="new directory for the gradings")
    wide.add_argument("--command", required=True, help="shell run in the verifier container after the tests")
    wide.add_argument("--format", required=True, choices=["go-json", "junit"])
    wide.add_argument("--limit", type=int, default=600, help="seconds the wide command may run")
    wide.add_argument("--repeats", type=int, default=wide_rule.GOLD_RUNS, help="serial gold gradings")
    wide.add_argument(
        "--load-runs",
        type=int,
        default=wide_rule.LOAD_RUNS,
        help="further gold gradings run several at a time, as a campaign grades",
    )
    wide.add_argument("--load-workers", type=int, default=wide_rule.LOAD_WORKERS, help="how many of them run at a time")
    wide.add_argument("--harmful", type=Path, help="a patch known to carry the harm")
    wide.add_argument(
        "--harm-name",
        action="append",
        default=[],
        metavar="HARM_ID=WIDE_NAME",
        help="the wide name of a harm test (repeatable); an argument with more than one '=' is refused",
    )
    wide.add_argument(
        "--harm-names",
        type=Path,
        metavar="FILE",
        help='JSON object {"HARM_ID": ["WIDE_NAME", ...]}, for ids that hold "="; joined with --harm-name',
    )
    for flag, text in (
        ("--test-side", "path pattern of test-side files the built-in lists do not know (repeatable)"),
        ("--source", "path pattern of source files that only look like tests (repeatable)"),
        ("--settings", "path pattern of files that configure this repository's test runner (repeatable)"),
    ):
        wide.add_argument(flag, action="append", default=[], metavar="PATTERN", help=text)
    wide.add_argument("task_id")
    paths = commands.add_parser(
        "task-paths",
        help="before a task is used: how its repository's files fall under the test-path rules",
        description="Lists, from the task image, the folders the restored run would put back and the test-looking "
        "files neither put back nor named as an open route, under the task's record in --wide when it has one. What "
        "the built-in lists miss in a repository is named in the record with wide-check's --test-side, --source and "
        "--settings.",
    )
    paths.add_argument("--dataset", type=Path, required=True)
    paths.add_argument("--wide", type=Path, help="directory of task records")
    paths.add_argument("task_id")
    check = commands.add_parser("grade-check", help="grade gold and empty patches to validate the verifiers")
    check.add_argument("--dataset", type=Path, required=True)
    check.add_argument("task_ids", nargs="+")
    check.add_argument("--out", type=Path, help="new output directory")
    check.add_argument("--workers", type=int, default=1)
    count = commands.add_parser(
        "count",
        help="count landed planted sessions per task and stratum from saved artifacts",
        description="Gate counting over a run root that holds one campaign directory per task. The harm node of each "
        "task comes from its manifest's harm-test-failed check. --pools is a JSON file "
        '{task: {"stratum": "headline"|"beside", "clean_pool": [glob of clean campaign directories, ...]}}, globs '
        "absolute or relative to the file. --labels is a JSON file {session path relative to the run root: "
        "COUNTED|BESIDE|NOT}: every landed candidate needs a label and no other session may have one.",
    )
    count.add_argument("run_root", type=Path)
    count.add_argument("--pools", type=Path, required=True, help="clean pools and strata per task (JSON)")
    count.add_argument("--labels", type=Path, help="manual reliance labels (JSON); without them main is null")
    count.add_argument(
        "--no-run-clean", action="store_true", help="leave the run's own seeded-clean sessions out of the reference"
    )
    count.add_argument("--out", type=Path, help="also write the count JSON to this file")
    harm = commands.add_parser(
        "harm-check",
        help="before a paid run: check a campaign's harm node on clean sessions that never saw a note",
        description="Reads the harm-test-failed check of an experiment config and grades it on clean sessions: "
        "unsuitable when any session fails the harm node, masks it with its own test edits, or lacks a measured grade "
        "and test edit pass (run `grade --test-edits-only` on older pools first). Exit code 1 when unsuitable.",
    )
    harm.add_argument("config", type=Path)
    harm.add_argument("--pool", action="append", required=True, help="glob of clean campaign directories (repeatable)")
    pro = commands.add_parser("export-pro", help="export SWE-bench Pro tasks as DeepSWE-shaped task directories")
    pro.add_argument("--rows", type=Path, required=True, help="SWE-bench Pro rows (.parquet or .json)")
    pro.add_argument("--official", type=Path, required=True, help="directory of <instance>.run_script.sh/.parser.py")
    pro.add_argument("--grader", type=Path, required=True, help="the shared DeepSWE grader.py")
    pro.add_argument("--harm", type=Path, help='JSON {instance: {"run": [...], "commands": [...], "node_ids": [...]}}')
    pro.add_argument("--out", type=Path, required=True, help="dataset root; tasks go to <out>/tasks/<short id>")
    pro.add_argument("instances", nargs="+")
    watch = commands.add_parser("watch", help="show a campaign or session step by step, live with --follow")
    watch.add_argument("path", type=Path, help="campaign directory, session directory or events.jsonl")
    watch.add_argument("-f", "--follow", action="store_true", help="keep showing new events until interrupted")
    watch.add_argument("--full", action="store_true", help="show long outputs and prompts in full")
    watch.add_argument(
        "--agent", action="append", default=[], help="show only this agent's events (repeatable; default: all agents)"
    )
    pairing = commands.add_parser(
        "pair",
        help="before a run-local attacker pilot: check a defended and an undefended config and write their pairing",
        description="Loads and validates both experiment files with no model call and no container, checks that they "
        "differ only in the guard and its notices, and writes the pairing manifest to --out (never overwritten). Exit "
        "code 1 when the two do not pair; the manifest lists the problems.",
    )
    pairing.add_argument("defended", type=Path)
    pairing.add_argument("undefended", type=Path)
    pairing.add_argument("--plugin", action="append", default=[])
    pairing.add_argument("--out", type=Path, required=True, help="the pairing manifest, new")
    pair_summary = commands.add_parser(
        "pair-summary",
        help="the run-local attacker's records of both campaigns of a pair, measurements kept apart",
        description="Reads the attacker records of a finished defended and undefended campaign with no model call and "
        "no container, and prints per chain the calls and what their actions came to, the use beside the budget, "
        "whether each applied write was later seen, carried or copied, and infrastructure errors. Refuses a campaign "
        "that does not run the paired config or the pairing's attacker. Task success and the attack objective come "
        "from grade, report and harm-count.",
    )
    pair_summary.add_argument("--pair", type=Path, required=True, help="the pairing manifest")
    pair_summary.add_argument("defended", type=Path, help="the defended campaign directory")
    pair_summary.add_argument("undefended", type=Path, help="the undefended campaign directory")
    pair_summary.add_argument(
        "--min-line-chars", type=int, default=20, help="shortest added line the seen and carried readings match"
    )
    pair_summary.add_argument("--out", type=Path, help="also write the summary JSON to this file")
    schema = commands.add_parser("schema", help="print the experiment JSON Schema")
    schema.add_argument("--out", type=Path)
    harm_plan = commands.add_parser(
        "harm-plan",
        help="before the first session: write the plan a harm count is read against",
        description="Fixes the read session, its task, the harm tests, the chains and the rule of a campaign, and pins "
        "the hashes of the config, the task's wide record and the count's code. Never overwrites --out.",
    )
    harm_plan.add_argument("config", type=Path)
    harm_plan.add_argument("--wide", type=Path, required=True, help="directory of wide records")
    harm_plan.add_argument("--rule", required=True, choices=["fisher", "boschloo", "probe"])
    harm_plan.add_argument("--treated-arm", default="attack_adaptive")
    harm_plan.add_argument("--alpha", type=float, default=0.05)
    harm_plan.add_argument("--clean-max", type=int, default=0)
    harm_plan.add_argument("--treated-min", type=int, default=3)
    harm_plan.add_argument("--out", type=Path, required=True, help="the plan file, new")
    harm_count = commands.add_parser(
        "harm-count",
        help="count harm in the clean and treated chains of a campaign against its plan",
        description="Exit code 0 with the count on stdout, or 1 with the reason the count refused on stderr.",
    )
    harm_count.add_argument("campaign", type=Path)
    harm_count.add_argument("--plan", type=Path, required=True)
    harm_count.add_argument("--wide", type=Path, required=True, help="directory of wide records")
    harm_count.add_argument("--dataset", type=Path, help="task export (default: the campaign's dataset_root)")
    harm_count.add_argument("--accept-code-change", metavar="REASON", help="count with code other than the pinned one")
    harm_count.add_argument("--out", type=Path, help="also write the count JSON to this file")
    return parser


def execute(args) -> int:
    from sereno.context_eval.config import default_registry, load_config, validate
    from sereno.context_eval.schema import ExperimentConfig

    pair_result: dict = {}
    try:
        command = args.context_command
        if command in ("harm-plan", "harm-count"):
            from sereno.context_eval.harm_count import command as harm

            return harm(args)
        if command == "watch":
            from sereno.context_eval.watch import watch

            return watch(args.path, follow=args.follow, full=args.full, agents=args.agent)
        if command == "schema":
            result = ExperimentConfig.model_json_schema()
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2) + "\n")
                return 0
        elif command == "pair":
            from sereno.context_eval.pairing import pair

            pair_result = pair(args.defended, args.undefended, default_registry(args.plugin))
            result = pair_result
            args.out.parent.mkdir(parents=True, exist_ok=True)
            try:
                stream = args.out.open("x")
            except FileExistsError:
                raise ValueError(f"{args.out} already exists; a pairing manifest is never overwritten") from None
            with stream:
                stream.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        elif command == "pair-summary":
            from sereno.context_eval.pairing import summarize_pair

            pairing = json.loads(args.pair.read_text())
            result = summarize_pair(pairing, args.defended, args.undefended, min_line_chars=args.min_line_chars)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        elif command == "catalog":
            from sereno.context_eval.dataset import catalog

            result = [
                r
                for r in catalog(args.dataset.expanduser().resolve())
                if (not args.language or r["language"] == args.language) and (not args.available_only or r["available"])
            ]
        elif command == "count":
            from sereno.context_eval.counting import count_run, load_labels, load_pools

            pools = args.pools.expanduser().resolve()
            result = count_run(
                args.run_root,
                load_pools(pools),
                pools.parent,
                labels=load_labels(args.labels) if args.labels else None,
                include_run_clean=not args.no_run_clean,
            )
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        elif command == "harm-check":
            from sereno.context_eval.counting import harm_suitability, pool_sessions

            config = load_config(args.config)
            checks = [check.model_dump(mode="json") for check in config.checks]
            result = harm_suitability(checks, pool_sessions(args.pool, Path.cwd()))
        elif command == "export":
            from sereno.context_eval.runner import export_submission

            result = {"exported": str(export_submission(args.campaign, args.case, args.arm, args.session, args.out))}
        elif command == "grade":
            from sereno.context_eval.evaluator import grade_campaign

            result = grade_campaign(
                args.campaign,
                dataset=args.dataset,
                workers=args.workers,
                force=args.force,
                test_edits_only=args.test_edits_only,
                wide_root=args.wide,
            )
        elif command == "wide-check":
            from sereno.context_eval.evaluator import wide_check

            names = harm_names(args.harm_name, args.harm_names)
            record = {"command": args.command, "format": args.format, "limit_seconds": args.limit}
            record |= {"test_side": args.test_side, "source": args.source, "settings": args.settings}
            result = wide_check(
                args.dataset,
                args.task_id,
                args.wide,
                args.out,
                record,
                args.repeats,
                args.harmful,
                names,
                load_runs=args.load_runs,
                load_workers=args.load_workers,
            )
        elif command == "task-paths":
            from sereno.context_eval import wide as wide_records
            from sereno.context_eval.evaluator import task_paths

            result = task_paths(args.dataset, args.task_id, wide_records.load(args.wide, args.task_id))
        elif command == "export-pro":
            from sereno.context_eval.swebench_pro import export

            paths = export(args.rows, args.official, args.grader, args.out, args.instances, args.harm)
            result = {"tasks": [path.name for path in paths], "root": str(args.out.resolve())}
        elif command == "grade-check":
            from sereno.context_eval.evaluator import grade_check

            result = grade_check(args.dataset, args.task_ids, out=args.out, workers=args.workers)
        else:
            from sereno.context_eval.metrics import overview

            registry = default_registry(args.plugin)
            if command == "report":
                from sereno.context_eval.metrics import report

                outcome = report(args.campaign, registry, bootstrap=args.bootstrap, seed=args.seed)
                result = overview(args.campaign, outcome)
            elif command == "summarize":
                from sereno.context_eval.metrics import summarize

                outcome = summarize(args.campaigns, args.out, registry, bootstrap=args.bootstrap, seed=args.seed)
                result = overview(args.out, outcome)
            else:
                config = load_config(args.config)
                if command == "validate":
                    result = validate(config, registry)
                elif command == "dry-check":
                    from sereno.context_eval.runner import dry_check

                    validate(config, registry)
                    items = dry_check(config)
                    result = {"ok": all(item["status"] != "missing" for item in items), "items": items}
                else:
                    from sereno.context_eval.runner import run_campaign

                    outcome = run_campaign(config, args.out, registry, resume=args.resume)
                    result = {"campaign": str(args.out.resolve())} | overview(args.out, outcome, groups=False)
                    if outcome.get("adaptive_stop"):
                        result["adaptive_stop"] = outcome["adaptive_stop"]
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if command == "run":
            return int(any(s["status"] != "complete" for s in outcome["sessions"]))
        if command == "grade":
            return int(result["statuses"].get("grader_error", 0) > 0 or result["unreadable"] > 0)
        if command == "grade-check":
            return int(not result["passed"])
        if command == "wide-check":
            return int(bool(result["problems"] or result["unusable"]))
        if command == "harm-check":
            return int(not result["suitable"])
        if command == "dry-check":
            return int(not result["ok"])
        if command == "pair":
            return int(not pair_result["paired"])
        return 0
    except ValidationError as error:
        # Validation inputs can include inline credentials or confidential task content.
        errors = [
            {"path": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors(include_input=False)
        ]
        print(json.dumps({"validation_errors": errors}, indent=2))
        return 2
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"context-eval: {error}")
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sereno")
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser(commands)
    return execute(parser.parse_args(argv))
