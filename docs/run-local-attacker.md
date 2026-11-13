# Run-local adaptive attacker

`run_attacker` measures the defense layer on long task chains against a strong but measurable adaptive attacker. An
external writer program reads the code and the agent's recorded transcript, writes into the permitted memory surface,
and improves its own strategy and notebook as the chain goes on. Its interventions stay on the existing memory surface;
model weights, tool permissions and harness code stay fixed. All experimental content comes from the external AI
writer; the harness authors none.

The design behind the attacker, including what it observes and how its private state evolves, is in [Attack layer
concept](attack-layer-concept.md).

## Layout: two paired campaigns

The runner's structure is unchanged: run, case and repeat, arm, session. Every task is one session in a fresh container,
and memory carries between sessions under the existing rules. Defense is chosen per campaign, so a comparison is two
campaigns, one defended (with `guard`) and one undefended (without), each with the existing `clean` and `attack_carry`
arms. The attacker is called only in `attack_carry`, never in `clean`. Nothing but a pairing manifest sits on top; there
is no new chain executor.

```sh
uv run sereno context-eval pair defended.yaml undefended.yaml --out pair.json
```

checks, with no model call and no container, that the two files differ only in `guard` and `notices`: task order,
sessions, start memory, models (by the content of the model config), memory instructions, the attacker's program,
starting strategy, limits and budgets must be the same. Exit code 1 lists the differences. The manifest records both
configs' hashes, which the campaigns' own manifests must later match. This is the dry-run path of a pilot.

Each attack chain learns only within itself. Its state lives on the arm's engine of one case and repeat; nothing crosses
to the other arm, another case or repeat, the other campaign or a new run. The defense's effect on clean tasks is read
from the two campaigns' `clean` results.

## Configuration

Add to an experiment config with `arms: [clean, attack_carry]` and no variants (static interventions, `repair` and the
between-session `adaptive` writer are not combined with it):

```yaml
arms: [clean, attack_carry]
variants:
  default: []
run_attacker:
  command: [/absolute/path/to/writer-command, --live, ...]
  strategy_file: starting-strategy.md   # optional; the writer's starting strategy, frozen into the config
  every_calls: 1                        # call before every n-th model call of the agent
  timeout_seconds: 120                  # one call
  budget:                               # per chain; no defaults, equal in both campaigns
    max_calls: 60
    max_calls_per_session: 20
    max_tokens: 2000000
    max_seconds: 3600
    max_writes: 10
  limits:                               # the adaptive hook's memory rules
    max_files_touched: 3
    max_file_bytes: 20000
  code_root: /app
  code_paths: [/app/src/example.py]     # structured code views sent on every call
  max_reads_per_call: 10
  max_read_bytes: 200000
  max_index_entries: 100000
  max_input_bytes: 4000000
  strategy_max_bytes: 20000
  notebook_max_bytes: 20000
```

The guard is not required and not constrained: the same attacker runs with the guard on and off.

## Protocol

The program is called synchronously from the hook before the agent's model call (every `every_calls`-th one) and once
at the end of each completed session. Two positional paths are appended to `command`: `request.json` and
`response.json`, both in the session's `run_attacker/NNNN/` folder; each call runs in a fresh temporary working
directory.

The request (`schema_version: "1.0"`) has three parts:

1. `contract`, the fixed experiment contract the attacker cannot change: call kind (`in_task` or `session_end`), stage
   (`exposure` or `probe`), whether writes are open, the memory root, the limits, the budget with the use so far, the
   size limits and the response's keys. The writer adds its own frozen system prompt and objective.
2. `strategy` (`version`, `text`, `sha256`) and `notebook`, the attacker's evolving, private state.
3. `observation`: the session and task, the repository's file index at task start (`git ls-files` below `code_root`, as
   absolute paths), the `code_paths` views, the files the previous call asked for, the agent's visible transcript, the
   earlier sessions of the same chain (visible transcripts and standard session summaries), the memory files that load
   for the project (never the user's file), the paths the attacker created, the result of its previous call and, in the
   session-end call, the session summary.

The transcript holds the recorded messages and tool interactions: role, content, tool calls and results, plus the target
agent's readable thinking summaries in a `thinking` field. Signatures, redacted blocks and provider metadata are not
included; a redacted block with no readable text is named as redacted, not decoded. Guard notices the agent saw are part
of the transcript and are marked with `notice_id`. Hidden guard decisions, grader code and outputs of other chains are
never given.

The response is one object: `schema_version: "1.0"`, `actions` (the hook's write and delete actions; empty to wait),
optional `strategy` (left out keeps the current one), optional `notebook`, optional `reads` (paths below `code_root`,
served with the next call) and optional `writer_usage` (the provider's usage and cost).

## Thinking summaries and self-evolution

The attacker reads the target's thinking summaries, not just its messages. They show what the target believed, which
parts of the task it treated as settled, and whether a guard notice changed its plan. Signatures, redacted blocks and
provider metadata never reach the request.

Self-evolution here means bounded strategy adaptation inside one chain. The private strategy and notebook travel with
every call and, through the session checkpoint, into the next session of the same chain. The harness versions the
strategy and hashes it, and a valid update is kept even when the call's actions were rejected or not applied. The
session-end call is the reflection point: it sees the completed session and its summary, and it may revise the strategy
for the next session, though it cannot write memory. Nothing learned in one chain crosses to another case, repeat, arm
or campaign. There is no weight training, no fine-tuning and no task-specific code; a claim in the strategy or notebook
is not a measured success.

## What happens to an answer

- Actions apply only in an `in_task` call of an exposure session, within `max_writes`. They go through the adaptive
  hook's path, ownership, append-only, size, content and memory-limit rules; the user's `~/.claude/CLAUDE.md` is out of
  reach. Any violation rejects every action of the call and leaves memory unchanged.
- In a probe session and in the session-end call no action is applied (`not_applied`): the attacker may observe and
  update its private state but cannot change the defended memory. This separates a later effect of carried memory from
  re-injection.
- Applied writes land in the files and go through the live guard like any write it sees. They become visible to the
  agent only through later real memory reads and the next session's memory loading; messages already prepared for the
  current model call are not rewritten.
- A valid strategy or notebook update is kept whatever happened to the actions, and the next call's `previous_result`
  says what the actions came to (`applied`, `no_change`, `no_action`, `rejected` with violations, or `not_applied` with
  the reason). Strategy and notebook never reach the agent's memory.
- A call starts only while every budget bound has room, so realized use can pass a bound by at most the one call that
  crossed it; a skipped call is counted and logged once per session and reason.

Code access is read-only. A path outside `code_root`, into `.git`, through a symlink that leads out, or beyond the
per-call read budget is named as denied. A file or index over its size limit is named with its size and never cut; a
request over `max_input_bytes` is an infrastructure error, never cut.

## Records, resume and errors

Each call leaves `request.json`, `response.json`, `record.json` (kind, stage, step, strategy version and hashes before
and after, request and response hashes, action result and violations, changes by hash and size, reported tokens and
cost, seconds) and the program's stdout and stderr, plus a `run_attacker` event. A completed session also writes
`run_attacker_state.json`, the chain's checkpoint, after the session-end call; an attack session counts as complete
only with it. A resume starts each chain from its last completed session's checkpoint, so nothing a half-finished
session learned carries into the new attempt.

A program, configuration or provider problem (exit status, timeout, an answer not in the protocol's form, an input
over the limit) is not an attack failure: the session ends invalid with `result.json`'s `infrastructure_error`, the
call's record holds `status: infrastructure_error`, and a resume runs the session again from the last checkpoint.

## Measurement

The measurements stay separate:

1. a write applied, and whether its added lines later reached the agent;
2. carry into later sessions' start memory, and the agent copying them into its own memory writes;
3. the attack objective, read by the existing `grade`, `report` and `harm-count` per campaign;
4. task success, and the defense's effect on clean tasks from both campaigns' `clean` arms;
5. probe sessions after writes close, and recovery, again from the existing grading;
6. attacker cost and use beside the equal bounds, and infrastructure errors.

```sh
uv run sereno context-eval pair-summary --pair pair.json DEFENDED_CAMPAIGN UNDEFENDED_CAMPAIGN [--min-line-chars 20]
```

reports 1, 2 and 6 per chain from the records, and refuses a directory with no campaign manifest and a campaign that
does not run the paired config or the pairing's attacker (its settings, starting strategy and program hashes). Its
readings of "seen", "carried" and "copied" match added lines of at least `--min-line-chars` characters verbatim and are
stated in its output. No outcome is derived from what the attacker or the agent says: a success claim in a transcript or notebook
is never a measured success.

Independently evolving attackers need not write the same content in the two campaigns, so the result is named
**defense effectiveness under equal-budget adaptive attack**, never a direct comparison of the same attack. The
independent unit for uncertainty is the chain (one target and repeat); the sessions of a chain are not independent
samples.

## Offline demo

`examples/context-eval/run-attacker-defended.yaml` and `run-attacker-undefended.yaml` run the whole path with no model
and no API key: the scripted agent replays fixed commands on the `fastapi-implicit-head-options` image, and the attacker
is `tests/run_attacker_stub.py`, a neutral stub that writes one inert marker line to a fixed memory file in the first
exposure call, keeps a call counter in its notebook and reports zero usage. The two files differ only in the guard,
which runs its deterministic rules only (the judge rules 3 and 4 off, no notices). From the repository root, one
campaign at a time:

```sh
uv run sereno context-eval pair examples/context-eval/run-attacker-defended.yaml \
  examples/context-eval/run-attacker-undefended.yaml --out runs/context-eval/run-attacker-demo/pair.json
uv run sereno context-eval run examples/context-eval/run-attacker-defended.yaml \
  --out runs/context-eval/run-attacker-demo/defended
uv run sereno context-eval run examples/context-eval/run-attacker-undefended.yaml \
  --out runs/context-eval/run-attacker-demo/undefended
uv run sereno context-eval pair-summary --pair runs/context-eval/run-attacker-demo/pair.json \
  runs/context-eval/run-attacker-demo/defended runs/context-eval/run-attacker-demo/undefended
uv run python examples/context-eval/run_attacker_check.py runs/context-eval/run-attacker-demo/pair.json \
  runs/context-eval/run-attacker-demo/defended runs/context-eval/run-attacker-demo/undefended
```

`pair` refuses an existing manifest, so remove the demo directory before a second pass. Each campaign runs four
sessions, two in `clean` and two in `attack_carry`, each in its own container. Expected: both campaigns pair and complete
with no infrastructure error; each attack chain makes seven calls (three in-task and one session-end in the exposure,
two and one in the probe), one applied write that the agent reads in the same session, that is carried into the probe's
start memory and seen there, and no write in the probe; `run_attacker_check.py` prints the full table and exits 1 on any
mismatch. `SERENO_DOCKER_TESTS=1 uv run pytest tests/test_context_eval_examples.py -k run_attacker` runs the same
sequence.

The demo validates the plumbing only. The marker is inert, so no guard rule fires: the defended campaign differs from
the undefended one only by the guard's own records (`guard.json`, no `guard_finding`), and the demo shows no defense
effect.

## The DeepSeek writer in live mode

`python -m sereno.writer --live --config writer.json --config-sha256 H --code-sha256 H` serves this protocol with the
frozen design's system prompt, objective, provider and limits; the hashes come from `python -m sereno.writer hashes
--config writer.json`, and `run_attacker.timeout_seconds` must be larger than the writer's `deadline_seconds`. Each
call makes one author call and at most the design's structural repairs; a harness rule is never repaired there, the
harness rejects and the next request reports it. The adapter keeps no state of its own, and its provider usage is
returned in `writer_usage`. Calls using paid models go through the existing experiment cost wrapper; enabling or
testing this feature offline starts no paid run.

## The model-agnostic attacker runtime

`src/sereno/attacker` is a second implementation of the same protocol: a tool-using agent loop whose model is a
configuration choice, not a property of the harness. It is stateless between calls, reads only the request and the
bounded tools, and turns its answer into the same `actions`/`strategy`/`notebook`/`reads`/`writer_usage` response. The
DeepSeek writer remains the single-call baseline; this runtime is the adaptive, multi-step path.

### Configuration

The configuration has three parts: the operator's brief, the provider block, and the runtime bounds. The brief is a
file the operator supplies; the harness authors none.

```yaml
schema_version: "1.0"
prompt_file: attacker-brief.md       # operator-supplied; record its hash with the campaign
objective: ""                        # optional, appended to the brief
provider:
  kind: openai-compatible            # or anthropic
  model: deepseek-chat
  base_url: https://api.deepseek.com/chat/completions
  credential_env: DEEPSEEK_API_KEY   # a variable name, never the key itself
  request: {}                        # merged into every request body
agent:
  max_steps: 24
  max_tool_calls_per_step: 8
  max_tool_output_bytes: 40000
  observation_max_bytes: 150000
  observation_file_bytes: 30000
  observation_paths: 200
  transcript_page_messages: 40
  transcript_page_bytes: 30000
  search_limit: 40
  max_queued_reads: 24
deadline_seconds: 900                # below run_attacker.timeout_seconds
```

Another model of the same API is a change to `model` and the provider's `request` block. The runtime sets no model
parameters itself: `provider.request` passes through unchanged, so every parameter stays at the provider's default. An
Anthropic provider needs `provider.request.max_tokens`, because the Messages API has no default for it. Another API is
one adapter module under `src/sereno/attacker/providers/`: a class with `name` and `complete(turns, tools, call)`, added
to `build_provider`. The loop and tools never read a provider's body.

### Running it

```sh
# validate the configuration and request, print the hashes; no model call, no credential needed
/absolute/sereno/.venv/bin/python -m sereno.attacker --check --config /absolute/attacker.yaml request.json

# serve one call
/absolute/sereno/.venv/bin/python -m sereno.attacker --live --config /absolute/attacker.yaml request.json response.json
```

`run_attacker.command` must be absolute and use the project's virtual environment, because the harness runs the
program in a fresh temporary working directory. `run_attacker.timeout_seconds` must be larger than the runtime's
`deadline_seconds`; `--check` refuses the pair otherwise. Each response's `writer_usage` records the provider, model,
call count, token use, cost, finish reason, and the configuration and prompt hashes, so a campaign can audit the brief
that produced it.

### The loop and its tools

The loop starts with the operator's brief, a fixed runtime contract, and the request's bounded observation envelope.
The model drives the same `run_attacker` request through tools:

- `list_sessions`, `read_transcript`, `search_transcript`: the running agent's visible transcript and the chain's
  earlier sessions, paged and searchable. Each message carries the target's readable thinking summary in a `thinking`
  field, so the summaries are included in the request and are searchable as well; signatures and provider metadata are
  not.
- `find_files`, `read_file`, `search_files`: the repository's file index and the code views. A path outside the
  current call's views is queued and served with the next harness call; a path outside `code_root`, in `.git`, or
  over the read limit is named as denied.
- `read_memory`: the carried memory files and the paths the attacker owns.
- `stage_action`, `drop_action`, `list_staged_actions`: the memory actions. The harness validates and applies them
  after the call, only in an exposure session's in-task call, within the existing write budget and memory rules.
- `set_strategy`, `set_notebook`: the private state that comes back with the next request.
- `finish`: ends the call. Without it, the call ends at `agent.max_steps` and returns what was staged; a tool call
  over `agent.max_tool_calls_per_step` is a denied result, not an unpaired tool call.

The response is checked before it is written: `schema_version`, actions' shape, strategy and notebook sizes, reads,
and `writer_usage`. A program, configuration or provider failure writes no response and exits non-zero, so the harness
records infrastructure rather than an attack outcome.

## Out of scope in this version

The attacker changing its own harness code, training across runs, attacks on repository or tool output, a critic
agent or separate attack search, and running together with the between-session `adaptive` writer.
