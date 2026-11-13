"""Record what each run costs on OpenRouter, the Anthropic API and the opencode Go API.

    uv run scripts/cost.py run --label <label> -- <command> [args...]
    uv run scripts/cost.py report [--label <label>]

`run` executes the command with the hook in `scripts/cost_hook` loaded and the
repository's `.env` in its environment (a variable already set wins). The hook
logs the usage OpenRouter, Anthropic or opencode returns with every completion (for
Anthropic, priced from the hook's table). When the command
exits, the calls are summed and one row is appended to the ledger
(`runs/cost/ledger.jsonl` by default). The wrapped command may use any Python
environment.

Cash spending and the opencode subscription allowance stay apart: `calls` and
`cost_usd` are OpenRouter and Anthropic cash, `allowance_calls` and `allowance_usd`
are the charges the Go API reports against the allowance, and an opencode call is
never unpriced. Per-call costs are the source of truth because OpenRouter's account
total lags by minutes. `report` prints the ledger and compares the cash sum with that
account total so a gap (calls the hook could not see, such as streamed responses)
shows up; Anthropic spending has no such check here, since its usage report needs an
Admin API key, and the allowance has no dollar total to compare with.
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
    """The run's calls, with cash spending (OpenRouter, Anthropic) kept apart from the opencode subscription allowance:
    the allowance is a charge against the subscription, never a dollar figure to add to cash."""
    calls = read_jsonl(calls_path) if calls_path.exists() else []
    allowance_rows = [c for c in calls if c.get("billing") == "allowance"]
    cash = [c for c in calls if c.get("billing") != "allowance"]
    billed = [c for c in cash if c.get("cost") is not None]
    unpriced = [c for c in cash if c.get("unpriced")]
    allowance_billed = [c for c in allowance_rows if c.get("cost") is not None]
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "providers": set()})
    for call in billed:
        entry = by_model[call["model"]]
        entry["calls"] += 1
        entry["cost_usd"] += call["cost"]
        entry["providers"].add(call["provider"])
    allowance_by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "allowance_usd": 0.0, "sources": set()})
    for call in allowance_billed:
        entry = allowance_by_model[call["model"]]
        entry["calls"] += 1
        entry["allowance_usd"] += call["cost"]
        entry["sources"].add(call.get("billing_source"))
    return {
        "calls": len(billed),
        "failed_calls": len(calls) - len(billed) - len(unpriced) - len(allowance_billed),
        "unpriced_calls": len(unpriced),
        "unpriced_models": sorted({str(c["model"]) for c in unpriced}),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in billed),
        "completion_tokens": sum(c["completion_tokens"] or 0 for c in billed),
        "cost_usd": round(sum(c["cost"] for c in billed), 8),
        "allowance_calls": len(allowance_billed),
        "allowance_usd": round(sum(c["cost"] for c in allowance_billed), 8),
        "allowance_unreported_calls": sum(1 for c in allowance_billed if not c.get("allowance_reported", True)),
        "allowance_prompt_tokens": sum(c["prompt_tokens"] or 0 for c in allowance_billed),
        "allowance_completion_tokens": sum(c["completion_tokens"] or 0 for c in allowance_billed),
        "by_model": {
            model: {**entry, "cost_usd": round(entry["cost_usd"], 8), "providers": sorted(map(str, entry["providers"]))}
            for model, entry in by_model.items()
        },
        "allowance_by_model": {
            model: {
                **entry,
                "allowance_usd": round(entry["allowance_usd"], 8),
                "sources": sorted(str(source) for source in entry["sources"]),
            }
            for model, entry in allowance_by_model.items()
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
        f"[cost] {run_id}: {row['calls']} cash calls ({row['failed_calls']} failed), "
        f"{row['prompt_tokens']}+{row['completion_tokens']} tokens, USD {row['cost_usd']:.6f}",
        file=sys.stderr,
    )
    if row["allowance_calls"]:
        print(
            f"[cost] {row['allowance_calls']} opencode calls, {row['allowance_prompt_tokens']}+"
            f"{row['allowance_completion_tokens']} tokens, charged USD {row['allowance_usd']:.6f} against the "
            f"subscription allowance (not cash)",
            file=sys.stderr,
        )
    if row["unpriced_calls"]:
        print(
            f"[cost] {row['unpriced_calls']} calls to models without a price are not in the total: "
            f"{', '.join(row['unpriced_models'])}; add them to ANTHROPIC_PRICES in scripts/cost_hook/sitecustomize.py",
            file=sys.stderr,
        )
    return exit_code


def cmd_report(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.ledger) if args.ledger.exists() else []
    if args.label:
        rows = [r for r in rows if r["label"] == args.label]
    header = (
        f"{'run':<44} {'calls':>6} {'failed':>6} {'tokens in':>10} {'tokens out':>10} {'USD':>10} "
        f"{'allow calls':>11} {'allow USD':>10}"
    )
    print(header)
    for r in rows:
        print(
            f"{r['run_id']:<44} {r['calls']:>6} {r['failed_calls']:>6} "
            f"{r['prompt_tokens']:>10} {r['completion_tokens']:>10} {r['cost_usd']:>10.6f} "
            f"{r.get('allowance_calls', 0):>11} {r.get('allowance_usd', 0.0):>10.6f}"
        )
    total = sum(r["cost_usd"] for r in rows)
    allowance_total = sum(r.get("allowance_usd", 0.0) for r in rows)
    print(
        f"{'total':<44} {sum(r['calls'] for r in rows):>6} {'':>6} {'':>10} {'':>10} {total:>10.6f} "
        f"{sum(r.get('allowance_calls', 0) for r in rows):>11} {allowance_total:>10.6f}"
    )

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
    run = sub.add_parser("run", help="run a command and record its model API cost")
    run.add_argument("--label", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    report = sub.add_parser("report", help="print the ledger and the account balance")
    report.add_argument("--label")
    args = parser.parse_args()
    load_dotenv(REPO / ".env")
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    return cmd_run(args) if args.action == "run" else cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
