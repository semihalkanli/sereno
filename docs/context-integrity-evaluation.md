# Context Integrity Evaluation

`sereno context-eval` evaluates prompt injection and persistent memory poisoning
in isolated coding-agent environments. Source lives in `sereno.context_eval`.
DeepSWE tasks and verifiers remain in an external checkout; images remain in
Docker's image store. They are not copied into this repository.

## Setup and offline verification

```sh
uv sync --extra deepswe
uv run --extra deepswe sereno context-eval catalog --dataset ~/sereno-deepswe/deep-swe --available-only
uv run --extra deepswe sereno context-eval schema --out runs/context-eval-schema.json
uv run --extra deepswe sereno context-eval validate examples/context-eval/scripted-canary.yaml
uv run --extra deepswe sereno context-eval run examples/context-eval/scripted-canary.yaml --out runs/context-eval/canary
uv run --extra deepswe sereno context-eval report runs/context-eval/canary
```

The output directory must be new; runs and exports never replace existing data.
The scripted canary executes a fixture oracle, with no model requests or API
credentials. Expected probe behavior: clean false, carry true, reset false.
Those outcomes validate experiment plumbing, not an AI robustness result.

The mini-swe example uses GLM-5.3 Flash via OpenRouter, pinned to Z.AI with no
fallback. Set `OPENROUTER_API_KEY` in the host environment using the project's
existing credential workflow. Keys are never forwarded to task containers.
Model configs must not contain inline credentials. Paid runs must use the
existing cost wrapper:

```sh
uv run --extra deepswe scripts/cost.py run --label context-eval -- \
  uv run --extra deepswe sereno context-eval run examples/context-eval/mini-swe.yaml \
  --out runs/context-eval/mini-swe
```

## Experiment definition

YAML uses version `1.0`; `schema` exports the complete JSON Schema. Paths to
datasets, payloads and model configs resolve relative to the YAML file, with
`~` expansion. Payload files can use `$name`/`${name}` templates supplied by
`parameters`; other payloads are literal data.

Select explicit `targets` by DeepSWE task ID, optionally overriding an image
reference. Each image must already be local. The runner resolves its immutable
image ID and checks the container's initial Git HEAD against the task's base
commit. `sessions[].task_id` selects a different task for transfer experiments;
otherwise the target task is reused.

`variants` supplies independent intervention sets. The matrix is target ×
variant × repeat. Repeats have separate memory and containers. `seed` seeds
the intervention engine's RNG for Python strategies; it does not force
deterministic provider sampling. Registry implementations should be stateless;
per-run state belongs to the runtime or engine.

Methods are `file`, `output` and `memory`. File paths must be canonical paths
below `/app`, excluding `.git`; memory paths must be below `/memories`.
Operations are `append`, `prepend` and `replace`. `replace` with `old_text`
requires exactly one match; without it, it replaces the entire surface.
Repository documentation and source comments are both file surfaces.

Hooks are `session_start`, `before_action`, `after_observation` and
`session_end`. Output interventions require `after_observation` and can filter
by `command_contains` and `output_contains`. Output changes affect what the
agent sees; raw execution output is retained. Injected output cannot trigger
the harness's submission control flow.

Strategies are `once`, `repeat` and `sequence`. All obey `max_fires` across
the arm: once requires one fire; repeat can fire at multiple matching hooks;
sequence fires at most once per selected session, useful for multi-session
exposure. Intervention declaration order is significant.

## Memory and causal controls

Exposure sessions must precede probe sessions. Interventions cannot target
probes. Each session starts in a fresh container and with a fresh agent
conversation. Only `/memories` files persist; code edits and transcripts do
not carry into the next session, including same-task probes.

Memory files have two owners. The agent writes its own notes with bash:
`MEMORY.md` and its topic files. The user writes `/memories/AGENT.md`, set by
`memory.user`; the agent is told not to edit it. The startup context loads
`AGENT.md` and `MEMORY.md`, each limited to 200 lines and 25,000 UTF-8 bytes;
topic files require explicit reads. Defaults allow at most 100 files and 1 MB
total memory. Symlinks, non-UTF-8 memory and over-limit snapshots invalidate
the session, with the rejected path or limit recorded in `result.json`.
`memory.seed` (agent files) and `memory.user` define identical initial memory
for all arms; `memory.seed` cannot contain `AGENT.md`.

Default arms:

- `clean`: same task chain and memory policy, with no interventions.
- `attack_carry`: exposed memory carries into clean probes.
- `attack_reset`: shares the exact exposure artifacts with carry, resets to
  the initial seed before the first probe, then preserves its own probe memory.

Reset does not rerun exposure and resample model behavior. Its copied exposure
artifacts include `branch.json` pointing at their origin. Report comparisons
must treat these shared exposures as one sample, not independent observations.
Every `memory_change` event records who made the change (`origin`: `agent`
or `harness`, the intervention engine) and whose file changed (`owner`:
`agent` or `user`). An agent edit to `AGENT.md` is `origin: agent`,
`owner: user`. Agent writes made in the same action as an `after_observation`
memory intervention are captured first and stay agent writes.

## Measurements and artifacts

Each campaign saves a manifest containing the resolved config, payload hashes,
task instruction hashes, dataset commit, image identities, dependency versions, plugin entrypoint hashes
and model config. Each session saves raw outputs and exact rendered context,
trajectory, intervention journal, memory snapshots, workspace check results,
raw patch, separated model patch, result and metrics.

Transport metrics measure applied interventions, observed marker exposure,
new marker writes by the agent, memory presence and memory-context recall.
Exposure requires the marker to survive observation formatting/truncation and
appear in an incoming model context during exposure. Assistant-generated
echoes do not count. Probe memory recall is separate from fresh external
exposure. Recall evidence covers startup `AGENT.md` and index content and
observations of commands that name the `/memories` root, such as
`cat /memories/a.md`, `ls /memories` or `cd /memories && cat a.md`; it is not
full filesystem taint tracking. A read that never names the root, such as one
through a variable or a script, can remain unattributed.

`checks` define the actual outcome independently of payload content, using
`contains` or `regex` over `memory`, `context`, `observations`, `final`,
`workspace` or `patch`. Workspace checks require `path`; memory checks can
select a path. `sessions` restricts where a check applies. These are positive
assertions: multiple applicable checks must all pass for a measured outcome.
No configured outcome means an unknown outcome, never a successful attack.
Marker checks cannot detect semantic paraphrases or establish intent.
Markers must be unique within a variant. Interventions without markers remain
usable, but an unobserved exposure is reported as unknown rather than false
when any intervention lacks a marker. Missing evidence is also unknown.

Reports show numerator, denominator, valid/invalid counts and rates over
measured outcomes, with a separate rate for externally exposed sessions.
Interrupted, missing or invalid sessions do not count as attack failures.
Custom metrics declare their own evidence and status. `task_success` stays
`not_evaluated` until a DeepSWE evaluator is connected.

Budgets default to 2 USD and three hours per session, 20 USD per campaign,
one worker and one repeat. The mini-swe adapter requires cost reporting;
it does not silently accept missing cost as zero. Reservations coordinate
workers. Caps stop subsequent model calls; the last in-flight request can
exceed a cap, which is reported. An unsuccessful call with unknown cost stops
new paid sessions. A single repeat is a smoke check, not statistical evidence.
Command timeouts terminate the ephemeral container to prevent a timed-out
process from modifying state after a snapshot.

## Extensions and DeepSWE export

`contracts.py` defines `AgentAdapter`, `EnvironmentAdapter`, `MemoryAdapter`,
`Strategy`, `Metric`, `Submission` and the future `EvaluatorAdapter`.
Register custom agents, strategies and metrics in a Python module exposing
`register(registry)`, then pass `--plugin your_package.module` to validate,
run or report. Plugin code is trusted local code, loaded only explicitly.
YAML and agent output cannot select/import arbitrary modules.
`examples/context-eval/extensions.py` shows the interfaces.

```sh
uv run --extra deepswe sereno context-eval export runs/context-eval/canary \
  --case fastapi-implicit-head-options--repository-document--r001 \
  --arm attack_carry --session probe --out runs/context-eval/export
```

Export creates `<task_id>/model.patch` and `submission.json`, compatible with
the existing DeepSWE grade script's directory layout. Raw workspace changes
are retained separately. Harness file edits are reversed before collecting
the candidate patch. Disjoint, identifiable agent edits are preserved;
ambiguous overlap is an explicit export error. Memory lives outside `/app`
and never enters the task patch.

The evaluator interface will accept task/image/base-commit identity and a
patch, returning DeepSWE `reward`, `f2p`, `p2p`, `partial` and verifier artifact
references. Verifiers and held-out tests remain outside agent containers.
