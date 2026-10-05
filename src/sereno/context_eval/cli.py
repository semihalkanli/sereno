"""CLI for catalogs, validated campaign matrices, offline reports and patch export."""

import argparse
import json
import subprocess
from pathlib import Path

from pydantic import ValidationError


def add_parser(parent):
    parser = parent.add_parser("context-eval", help="evaluate prompt injection and persistent memory integrity")
    commands = parser.add_subparsers(dest="context_command", required=True)
    catalog = commands.add_parser("catalog", help="list external tasks and locally available images")
    catalog.add_argument("--dataset", type=Path, required=True)
    catalog.add_argument("--language")
    catalog.add_argument("--available-only", action="store_true")
    for name in ("validate", "run"):
        command = commands.add_parser(name)
        command.add_argument("config", type=Path)
        command.add_argument("--plugin", action="append", default=[], help="explicit trusted Python module")
        if name == "run":
            command.add_argument("--out", type=Path, required=True, help="new campaign directory")
    report = commands.add_parser("report", help="recompute metrics from saved artifacts")
    report.add_argument("campaign", type=Path)
    report.add_argument("--plugin", action="append", default=[])
    export = commands.add_parser("export", help="export one grade-ready session patch")
    export.add_argument("campaign", type=Path)
    export.add_argument("--case", required=True)
    export.add_argument("--arm", choices=["clean", "attack_carry", "attack_reset"], required=True)
    export.add_argument("--session", required=True)
    export.add_argument("--out", type=Path, required=True)
    schema = commands.add_parser("schema", help="print the experiment JSON Schema")
    schema.add_argument("--out", type=Path)
    return parser


def execute(args) -> int:
    from sereno.context_eval.config import default_registry, load_config, validate
    from sereno.context_eval.schema import ExperimentConfig

    try:
        command = args.context_command
        if command == "schema":
            result = ExperimentConfig.model_json_schema()
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(result, indent=2) + "\n")
                return 0
        elif command == "catalog":
            from sereno.context_eval.dataset import catalog

            result = [
                r
                for r in catalog(args.dataset.expanduser().resolve())
                if (not args.language or r["language"] == args.language) and (not args.available_only or r["available"])
            ]
        elif command == "export":
            from sereno.context_eval.runner import export_submission

            result = {"exported": str(export_submission(args.campaign, args.case, args.arm, args.session, args.out))}
        else:
            registry = default_registry(args.plugin)
            if command == "report":
                from sereno.context_eval.metrics import report

                result = report(args.campaign, registry)
            else:
                config = load_config(args.config)
                if command == "validate":
                    result = validate(config, registry)
                else:
                    from sereno.context_eval.runner import run_campaign

                    result = run_campaign(config, args.out, registry)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if command == "run":
            return int(any(s["status"] != "complete" for s in result["sessions"]))
        return 0
    except ModuleNotFoundError as error:
        print(f"Missing dependency {error.name}; install the project extra with: uv sync --extra deepswe")
        return 2
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
