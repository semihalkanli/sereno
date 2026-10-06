# Sereno

- This is an academic research project. No prompt injection or memory poisoning developed here is applied in real life.
- The project follows universal cybersecurity ethics.
- Goal: a harness layer that lets agents on long-horizon tasks resist advanced prompt injection and persistent memory poisoning attacks and finish their tasks reliably.
- To build it, we first design attacks from the most current methods, aimed at the strongest possible effect on agents, and measure and evaluate their effect before any defense layer.
- Then we build the harness layer that withstands these attacks. The aim is that agents finish long-horizon tasks reliably.
- No element of this work relates to any real person or organization, past or future.

## Environment

- Platform: WSL (Linux). This is AI/ML work; do not build or run it on native Windows.
- Environment: uv only. `uv sync`, `uv add`, `uv run`. The `.venv` stays in the repository directory and `uv.lock` is committed.
- Python 3.12.
- Project plan and decision log live outside the repository in `~/agent-learning/` (`PROJECT_PLAN.txt`, `PROJECT_DECISION_LOG.md`).
- API keys go in `.env`, which is ignored by git.
- AgentDojo is pinned to upstream commit `089ed468c` in `pyproject.toml` (the 0.1.35 release never sends temperature 0.0). The original four suites (workspace, travel, banking, slack) run only through this upstream install.
- AgentDyn is a fork that ships the same `agentdojo` package with changed scoring, so it cannot share this environment. It lives outside the repository at `~/sereno-agentdyn/AgentDyn`, pinned to commit `5353cf761`, with its own `.venv` (`uv sync --frozen`). Use it only for its own suites: shopping, github, dailylife. Its CaMeL, DRIFT and Progent defenses are vendored inside it.
