"""The program the hook runs:

    python -m sereno.writer --config writer.json --config-sha256 H --code-sha256 H request.json response.json

The two hashes come from `python -m sereno.writer hashes --config writer.json`. They are command words, so the campaign
manifest records them and a writer whose configuration or code has changed refuses to run. `audit CAMPAIGN` checks what
reached the provider after a run.

Exit status 0 means a response was written: a ready candidate, a deliberate no_change, or a candidate the harness will
record as rejected. Any other status means the writer could not do its work and wrote no response (decision 405): 2 the
configuration or campaign limits, 3 the request, state or input, 4 the provider call, 5 an answer the hook contract
cannot carry, 1 anything unexpected. The reason goes to stderr as one line; the hook keeps it beside the step.
"""

import argparse
import json
import sys
from pathlib import Path

from sereno.writer.audit import audit_campaign
from sereno.writer.config import check_identity, load_config
from sereno.writer.errors import WriterError
from sereno.writer.provider import Deadline
from sereno.writer.step import run_step


def run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="writer", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="writer.json")
    parser.add_argument("--config-sha256", required=True, help="config_sha256 from `hashes`; a mismatch is refused")
    parser.add_argument("--code-sha256", required=True, help="code_sha256 from `hashes`; a mismatch is refused")
    parser.add_argument("request", type=Path)
    parser.add_argument("response", type=Path)
    return parser


def hashes(argv: list[str]) -> int:
    """Print the two hashes and the command words that carry them, for building the campaign's adaptive command."""
    parser = argparse.ArgumentParser(prog="writer hashes")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        identity = load_config(args.config).identity()
    except WriterError as error:
        print(f"writer: {error.code}: {error}", file=sys.stderr)
        return error.exit_code
    words = ["--config", str(args.config.resolve())]
    words += ["--config-sha256", identity["config_sha256"], "--code-sha256", identity["code_sha256"]]
    print(json.dumps(identity | {"command_words": words}, indent=2))
    return 0


def audit(argv: list[str]) -> int:
    """Check what reached the provider against what was allowed, over a finished campaign directory."""
    parser = argparse.ArgumentParser(prog="writer audit")
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--dataset", type=Path, help="the dataset root; default: the campaign manifest's")
    parser.add_argument("--out", type=Path, help="also write the report here")
    args = parser.parse_args(argv)
    try:
        result = audit_campaign(args.campaign, args.dataset)
    except WriterError as error:
        print(f"writer: {error.code}: {error}", file=sys.stderr)
        return error.exit_code
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    print(json.dumps(result["summary"]))
    return 1 if result["summary"]["violations"] or result["summary"]["unresolved"] else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hashes":
        return hashes(argv[1:])
    if argv and argv[0] == "audit":
        return audit(argv[1:])
    args = run_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        check_identity(config, args.config_sha256, args.code_sha256)
        # The deadline starts once the configuration is read, so building the view counts against it.
        summary = run_step(config, args.request, args.response, Deadline(config.deadline_seconds))
    except WriterError as error:
        print(f"writer: {error.code}: {error}", file=sys.stderr)
        return error.exit_code
    except Exception as error:
        print(f"writer: internal_error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
