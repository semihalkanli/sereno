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
uv run sereno
```

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
