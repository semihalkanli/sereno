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

## Orchestration and subagents

Built from Anthropic's official documentation as read on 2026-10-07 (Claude Code and Claude Platform docs on subagents, agent teams, model configuration, effort, costs and multi-model strategies). The same rule lives in the user's global `~/.claude/CLAUDE.md`. Re-read the docs when a new model ships or a Claude Code update changes model aliases or effort defaults, not on every task.

- The session's model and effort, as the user set them, are the orchestrator. Never change them on the user's behalf. Planning, integration, judgment calls, and the final verdict stay in the main session; check each subagent's evidence before accepting its result.
- Delegate only when it pays: output that would flood the main context (search, logs, test output, doc fetching), genuinely independent and sizeable parallel tracks, work that needs restricted tools, or a fresh-context review of a finished diff. Work directly on anything finishable in a handful of tool calls, on sequential phases of the same work, and on coupled or same-file changes. Split by context, not by role: the agent that writes a feature also writes its tests.
- Verification of your own work stays in the main loop; never spawn subagents to double-check routine steps. The one exception is a fresh-context reviewer for a finished diff or deliverable on long or unattended work, told to report only correctness and requirement gaps.
- Prefer one subagent over several. Launch parallel ones in a single message. Brief each one completely the first time: objective, output format, tools and sources, boundaries.
- Pass `model` and `effort` explicitly on every Agent call; this rule is the explicit instruction the Agent tool's `effort` parameter requires. Never rely on defaults: the Claude Code and API defaults differ.
- Model per subagent (Claude Code aliases on the Anthropic API; a family alias that matches the session resolves to the session's exact model):
  - `haiku`: lookups, search and summarize, reading logs and test output, "where is X defined", classification and extraction with checkable output. Never code edits.
  - `sonnet`: a well-scoped task with a clear spec and a way to check it, such as a bug fix, feature iteration, or a repeated investigation, review, or draft.
  - `opus`: code edits in the repository, judgment-heavy or long-horizon work, and measurement-critical code. Mechanical edits across many files stay on `opus` at `low`.
  - `fable`: the hardest long-horizon work, a long unsupervised run, a problem with no existing pattern, or one that Opus at `xhigh` failed the same way twice.
  - A worker above the session's own tier is never chosen alone: propose it to the user with the reason and let the user decide.
- Effort per subagent: `low` for mechanical or known-pattern edits and short lookups; `medium` for well-scoped building from a clear brief, and the default when unsure; `high` for review, bug-finding, edge cases, measurement code, strict instruction following, and any long brief on `haiku` or `sonnet` (at `low` they skip searches and checks); `xhigh` only for autonomous runs over about 30 minutes; `max` only with a measured gain.
- Diagnose before escalating: a skipped file, an unrun test, or an early stop means more effort; full context, a real attempt, and still wrong means a stronger model. Raise effort first.
- While `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, never pass `name` without also passing `isolation` on the call. A named call without it becomes a teammate, which takes the lead's effort and ignores the per-call choice.
