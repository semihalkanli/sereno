# Sereno

Sereno is a command-line agent for long, multi-session tasks. It is built to
finish the work reliably while resisting prompt injection and poisoned memory
that carries over from one session to the next.

The repository also holds a small benchmark of long tasks and attacks used to
measure it. Both are at an early stage.

## Setup

```sh
uv sync
cp .env.example .env   # then fill in OPENROUTER_API_KEY
```

## Running a scenario

A scenario is one person's world (mail, calendar, files), a request from that
person and a grader of deterministic state checks over the final world. The
first scenario, `kickoff`, is a single multi-step session used to check the
agent loop.

```sh
uv run sereno run kickoff --scripted          # replay the correct solution, no API calls
uv run sereno run kickoff --model glm53 --watch   # paid run on OpenRouter, live viewer
uv run sereno watch --latest                  # open the viewer on the newest event log
```

Each run writes an append-only event log to `runs/agent/<run_id>/events.jsonl`;
the schema is documented in `src/sereno/events.py`. In the viewer, `w` shows
the world, `t` the model's reasoning, `ctrl+o` expands tool results, `f`
pauses following and `q` quits. Wrap paid runs in the cost wrapper below to
record them in the ledger.

## Development

```sh
uv run pytest
uv run ruff format . && uv run ruff check .
```

## Cost tracking

Every paid run goes through the cost wrapper, which records the usage OpenRouter
returns with each completion and appends one row per run to
`runs/cost/ledger.jsonl`:

```sh
uv run scripts/cost.py run --label <label> -- <command> [args...]
uv run scripts/cost.py report
```

The wrapped command can use any Python environment, including the separate
AgentDyn checkout (`uv run --project ~/sereno-agentdyn/AgentDyn ...`).
