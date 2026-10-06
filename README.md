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
uv run sereno watch                           # agent view: every run, live
uv run sereno watch --latest                  # open the newest run's transcript
```

Each run writes two files to `runs/agent/<run_id>/`: `events.jsonl`, the
append-only event log with everything that happened, including the exact
request and response of every model call, and `diag.jsonl`, the diagnostic
records (HTTP attempts, retries, errors) tagged with the event they belong to.
The schema is documented in `src/sereno/events.py`.

`sereno watch` is modelled on Claude Code's agent view. The list groups runs
by state (working, failed, stopped, completed); `space` peeks at a run,
`enter` or the right arrow attaches to its transcript, the left arrow goes
back, and `tab` focuses the input that starts a new `sereno run` (paid runs go
through the cost wrapper). In a transcript, `t` shows the model's reasoning,
`ctrl+o` expands tool results, `[` and `]` move between events, `d` shows an
event's raw JSON with its diagnostic records, `f` pauses following and `q`
quits. Wrap paid runs started from the shell in the cost wrapper below to
record them in the ledger.

## Context integrity experiments

`sereno context-eval` runs prompt injection and persistent memory poisoning
experiments with mini-swe-agent on external DeepSWE task images. Each session is
one whole task; a `/memories` directory carries between tasks. It plants content
in repository files, command output and memory files, compares clean, carry,
reset and ablation arms, grades patches with each task's own verifier, and reports exposure,
memory transport, attack success and task success with interval estimates.
See [Context Integrity Evaluation](docs/context-integrity-evaluation.md) for setup,
examples and offline verification. Install its optional dependencies with
`uv sync --extra deepswe`.

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
