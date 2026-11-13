"""The program the harness runs for one run-attacker call.

    python -m sereno.attacker --live --config attacker.yaml request.json response.json

`--check` validates the configuration, the request and the runtime bounds without a model call or a credential, and
prints the hashes a campaign should record. `--live` runs the bounded tool loop and writes the protocol response
atomically. Exit status 0 means a response was written; 2 the configuration or a contract bound, 3 the request or
arguments, 4 the provider call, 5 the response's form, and 1 anything unexpected. A failure writes no response, so the
harness records it as infrastructure rather than as an attack outcome.
"""

import argparse
import json
import sys
from pathlib import Path

from sereno.attacker.agent import run_agent
from sereno.attacker.config import load_config
from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import AttackerError, InputError
from sereno.attacker.protocol import check_contract, read_request, write_response
from sereno.attacker.providers import build_provider


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="sereno.attacker",
        description="Serve one run_attacker call with a model-agnostic tool loop.",
    )
    command.add_argument("--config", type=Path, required=True, help="the runtime configuration")
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="call the provider and write the response")
    mode.add_argument("--check", action="store_true", help="validate the configuration and request; no model call")
    command.add_argument("request", type=Path, help="the harness's request.json")
    command.add_argument("response", type=Path, nargs="?", help="the response.json to write in live mode")
    return command


def check_summary(config, request: dict) -> dict:
    contract = request["contract"]
    observation = request["observation"]
    session = observation.get("session") if isinstance(observation.get("session"), dict) else {}
    index = observation.get("code_index") if isinstance(observation.get("code_index"), dict) else {}
    transcript = observation.get("transcript")
    return {
        "status": "ok",
        "provider": config.settings.provider.kind,
        "model": config.settings.provider.model,
        "config_sha256": config.config_sha256,
        "prompt_sha256": config.prompt_sha256,
        "contract": {
            "kind": contract["kind"],
            "stage": contract["stage"],
            "writes_open": contract["writes_open"],
        },
        "observation": {
            "session": session.get("id"),
            "transcript_messages": len(transcript) if isinstance(transcript, list) else 0,
            "code_index": index.get("status"),
        },
    }


def run_summary(config, result) -> dict:
    return {
        "status": "ok",
        "steps": result.steps,
        "finish": result.finish,
        "calls": len(result.completions),
        "actions": len(result.response.get("actions") or []),
        "reads": len(result.response.get("reads") or []),
        "config_sha256": config.config_sha256,
        "prompt_sha256": config.prompt_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config(args.config)
        request = read_request(args.request)
        check_contract(config, request)
        if args.check:
            print(json.dumps(check_summary(config, request), indent=2, sort_keys=True))
            return 0
        if args.response is None:
            raise InputError("a live call needs the response path", "arguments")
        deadline = Deadline(config.settings.deadline_seconds)
        provider = build_provider(config, deadline)
        try:
            result = run_agent(config, request, provider, deadline)
        finally:
            provider.close()
        write_response(args.response, result.response)
        print(json.dumps(run_summary(config, result), sort_keys=True))
        return 0
    except AttackerError as error:
        print(f"attacker: {error.code}: {error}", file=sys.stderr)
        return error.exit_code
    except Exception as error:
        print(f"attacker: internal_error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
