# Sereno

- Platform: WSL (Linux). This is AI/ML work; do not build or run it on native Windows.
- Environment: uv only. `uv sync`, `uv add`, `uv run`. The `.venv` stays in the repository directory and `uv.lock` is committed.
- Python 3.12.
- Project plan and decision log live outside the repository in `~/agent-learning/` (`PROJECT_PLAN.txt`, `PROJECT_DECISION_LOG.md`).
- API keys go in `.env`, which is ignored by git.
- AgentDojo is pinned to upstream commit `089ed468c` in `pyproject.toml` (the 0.1.35 release never sends temperature 0.0). The original four suites (workspace, travel, banking, slack) run only through this upstream install.
- AgentDyn is a fork that ships the same `agentdojo` package with changed scoring, so it cannot share this environment. It lives outside the repository at `~/sereno-agentdyn/AgentDyn`, pinned to commit `5353cf761`, with its own `.venv` (`uv sync --frozen`). Use it only for its own suites: shopping, github, dailylife. Its CaMeL, DRIFT and Progent defenses are vendored inside it.
