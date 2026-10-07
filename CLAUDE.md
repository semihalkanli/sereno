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
- Experiments run on DeepSWE: the dataset checkout lives outside the repository at `~/sereno-deepswe/deep-swe` and its task images in Docker's image store.
- The earlier personal-world benchmark (chains, simulated apps, the chain runner and viewer) is archived on branch `archive/personal-worlds` and is not developed on `main`.
