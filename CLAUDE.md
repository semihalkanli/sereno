# Sereno

- Platform: WSL (Linux). This is AI/ML work; do not build or run it on native Windows.
- Environment: uv only. `uv sync`, `uv add`, `uv run`. The `.venv` stays in the repository directory and `uv.lock` is committed.
- Python 3.12.
- Project plan and decision log live outside the repository in `~/agent-learning/` (`PROJECT_PLAN.txt`, `PROJECT_DECISION_LOG.md`).
- API keys go in `.env`, which is ignored by git.
