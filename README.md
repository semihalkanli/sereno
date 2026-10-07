# Sereno

Sereno studies how prompt injection and persistent memory poisoning affect
coding agents on long-horizon work, and builds a harness layer that lets them
finish their tasks reliably despite these attacks.

The experiments run on DeepSWE tasks: each session is one whole task, and a
`/memories` directory carries from one task to the next.

## Setup

```sh
uv sync
cp .env.example .env   # then fill in OPENROUTER_API_KEY
```

The DeepSWE checkout lives outside the repository at `~/sereno-deepswe/deep-swe`,
and its task images in Docker's image store.

## Context integrity experiments

`sereno context-eval` runs prompt injection and persistent memory poisoning
experiments with mini-swe-agent on DeepSWE task images. It plants content in
repository files, command output and memory files, compares clean, carry, reset
and ablation arms with clean controls, can repair the poisoned memory through
the user's `AGENT.md`, grades patches with each task's own verifier, and reports
exposure, memory transport, attack success, adoption, task success and recovery
with interval estimates. See
[Context Integrity Evaluation](docs/context-integrity-evaluation.md) for setup,
examples and offline verification.

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
