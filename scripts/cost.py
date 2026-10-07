"""Record what each run costs on OpenRouter.

    uv run scripts/cost.py run --label <label> -- <command> [args...]
    uv run scripts/cost.py report [--label <label>]

`run` executes the command with the hook in `scripts/cost_hook` loaded, which
logs the usage OpenRouter returns with every completion. When the command
exits, the calls are summed and one row is appended to the ledger
(`runs/cost/ledger.jsonl` by default). The wrapped command may use any Python
environment.

Per-call costs are the source of truth because OpenRouter's account total lags
by minutes. `report` prints the ledger and compares its sum with that account
total so a gap (calls the hook could not see, such as streamed responses) shows
up.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
HOOK_DIR = Path(__file__).resolve().parent / "cost_hook"
DEFAULT_LEDGER = REPO / "runs" / "cost" / "ledger.jsonl"
OPENROUTER = "https://openrouter.ai/api/v1"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def git_state() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def openrouter_get(path: str) -> dict | None:
    load_dotenv(REPO / ".env")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    request = urllib.request.Request(f"{OPENROUTER}{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["data"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None


def summarize_calls(calls_path: Path) -> dict:
    calls = read_jsonl(calls_path) if calls_path.exists() else []
    billed = [c for c in calls if c["cost"] is not None]
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "providers": set()})
    for call in billed:
        entry = by_model[call["model"]]
        entry["calls"] += 1
        entry["cost_usd"] += call["cost"]
        entry["providers"].add(call["provider"])
    return {
        "calls": len(billed),
        "failed_calls": len(calls) - len(billed),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in billed),
        "completion_tokens": sum(c["completion_tokens"] or 0 for c in billed),
        "cost_usd": round(sum(c["cost"] for c in billed), 8),
        "by_model": {
            model: {**entry, "cost_usd": round(entry["cost_usd"], 8), "providers": sorted(map(str, entry["providers"]))}
            for model, entry in by_model.items()
        },
    }


def cmd_run(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        sys.exit("cost.py run: no command given")
    started = datetime.now(UTC)
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}_{args.label}"
    calls_path = args.ledger.parent / "calls" / f"{run_id}.jsonl"
    calls_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if env.get("VIRTUAL_ENV") == sys.prefix:
        del env["VIRTUAL_ENV"]
    env["SERENO_COST_CALLS"] = str(calls_path)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(HOOK_DIR), env.get("PYTHONPATH")) if p)
    credits_before = openrouter_get("/credits")
    t0 = time.monotonic()
    try:
        exit_code = subprocess.run(command, env=env).returncode
    except KeyboardInterrupt:
        exit_code = -signal.SIGINT

    row = {
        "run_id": run_id,
        "label": args.label,
        "command": command,
        "cwd": os.getcwd(),
        "git": git_state(),
        "started": started.isoformat(),
        "duration_s": round(time.monotonic() - t0, 1),
        "exit_code": exit_code,
        "account_usage_before": credits_before["total_usage"] if credits_before else None,
        "calls_file": str(calls_path.relative_to(args.ledger.parent)),
        **summarize_calls(calls_path),
    }
    with args.ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(
        f"[cost] {run_id}: {row['calls']} calls ({row['failed_calls']} failed), "
        f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, USD {row['cost_usd']:.6f}",
        file=sys.stderr,
    )
    return exit_code


def cmd_report(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.ledger) if args.ledger.exists() else []
    if args.label:
        rows = [r for r in rows if r["label"] == args.label]
    print(f"{'run':<44} {'calls':>6} {'failed':>6} {'tokens in':>10} {'tokens out':>10} {'USD':>10}")
    for r in rows:
        print(
            f"{r['run_id']:<44} {r['calls']:>6} {r['failed_calls']:>6} "
            f"{r['prompt_tokens']:>10} {r['completion_tokens']:>10} {r['cost_usd']:>10.6f}"
        )
    total = sum(r["cost_usd"] for r in rows)
    print(f"{'total':<44} {sum(r['calls'] for r in rows):>6} {'':>6} {'':>10} {'':>10} {total:>10.6f}")

    credits = openrouter_get("/credits")
    key = openrouter_get("/key")
    if credits:
        print(f"\naccount: used USD {credits['total_usage']:.4f} of {credits['total_credits']:.2f} credits")
        baseline = next((r["account_usage_before"] for r in rows if r["account_usage_before"] is not None), None)
        if baseline is not None and not args.label:
            since = credits["total_usage"] - baseline
            print(f"account usage since first ledger run: USD {since:.6f} (ledger: {total:.6f}; the account lags)")
    if key and key.get("limit") is not None:
        print(f"key limit: USD {key['limit']} ({key.get('limit_reset')}), remaining USD {key['limit_remaining']:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run a command and record its OpenRouter cost")
    run.add_argument("--label", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    report = sub.add_parser("report", help="print the ledger and the account balance")
    report.add_argument("--label")
    args = parser.parse_args()
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    return cmd_run(args) if args.action == "run" else cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
