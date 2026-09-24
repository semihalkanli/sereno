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
