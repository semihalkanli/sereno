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
  every_calls: 1                        # call before the first model call, then every n-th call after it
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
  max_input_bytes: 40000000
  strategy_max_bytes: 20000
  notebook_max_bytes: 20000
```

The guard is not required and not constrained: the same attacker runs with the guard on and off.

## Protocol

The program is called synchronously from the hook before the agent's model calls: the first call of a session always,
then call 1 + n, 1 + 2n, ... for `every_calls: n`, and once at the end of each completed session. Two positional paths
are appended to `command`: `request.json` and `response.json`, both in the session's `run_attacker/NNNN/` folder; each
call runs in a fresh temporary working directory.

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
- In-task calls use at most `max_calls_per_session - 1` calls and leave one remaining chain call for the current
  session's end. The end evaluation can use the full session and chain call caps. With a session cap of one, only
  the end evaluation runs. This reserves no calls for future sessions. Token, cost and elapsed-time usage cannot be
  reserved by call count: the shared token and time bounds may still prevent an end evaluation, and provider cost
  is reported rather than independently capped by this protocol.

Code access is read-only. A path outside `code_root`, into `.git`, through a symlink that leads out, or beyond the
per-call read budget is named as denied. A file or index over its size limit is named with its size and never cut; a
request over `max_input_bytes` is an infrastructure error, never cut. The error records the actual request bytes and
the configured limit. Each request is serialized once; its recorded size and SHA-256 describe the exact bytes written
atomically to `request.json`. Full earlier transcripts remain in every request and in the checkpoint. Code views are
read again on every call so they include the agent's latest edits.

The default input limit is 40,000,000 bytes. A metadata-only inventory on 2026-11-13 found 1,711 recorded `traj.json`
files under `runs/` (1,833,713,649 bytes total); the largest was 5,310,305 bytes. The three largest distinct sizes were
5,310,305, 4,542,723 and 4,502,818 bytes (14,355,846 combined). The three additional transcripts under
`~/agent-learning/evidence/` were 2,887, 2,770 and 2,770 bytes. No transcript contents were opened for this measurement.
Using three copies of the largest observed transcript gives 15,930,915 bytes for a final three-session request's
transcripts. Allowing another 2,000,000 bytes for the default ten requested code files and metadata gives an estimated
17,930,915 bytes, with over 2x headroom at the default limit. This is an observed-workload estimate, not a bound:
configured code views, large file indexes, JSON escaping or larger future transcripts can require an explicit higher
limit (up to 50,000,000 bytes).

## Records, resume and errors

Each call leaves `request.json`, `response.json`, `record.json` (kind, stage, step, strategy version and hashes before
and after, request and response hashes, action result and violations, changes by hash and size, reported tokens and
cost, seconds) and the program's stdout and stderr, plus a `run_attacker` event. A completed session also writes
`run_attacker_state.json`, the chain's checkpoint, after the session-end call; an attack session counts as complete
only with it. A resume starts each chain from its last completed session's checkpoint, so nothing a half-finished
session learned carries into the new attempt.

Existing accepted-call audits also record `loop_ending` and `applied_attack_actions` beside the `actions` count.
The optional protocol 1.0 response field `loop_ending` reports control flow: `plain_text`, `empty_reply`, `finish`,
`max_steps`, `max_tokens` or `declined`. The loop's own control flow is unchanged: a reply with no tool call gets the
runtime contract's nudge and the call continues while steps remain. `plain_text` reports a stop at a bound the loop
already has (the step or the token limit) whose last reply carried text and no tool call; `empty_reply` is the same
stop with no text. A call that ends through the `finish` tool reports `finish`. No text is classified as a decline.
The existing explicit-decline resampling policy is unchanged. This follows the existing optional-field extension
pattern rather than bumping the protocol version: older programs may omit the field, and the runtime omits it when an
older request does not advertise it. A missing ending is recorded as unknown, without inference from reply text or
older usage fields.

The call's `applied_attack_actions` counts actions that actually changed memory, excluding rejected, closed-write
and no-change actions. Each run-local attack session records the accumulated count in `result.json` and its
checkpoint metadata. A successful write remains counted in the session total if subsequent memory capture fails.
If rollback fails and the resulting application count cannot be established, the session records `null`, rather
than zero. Application failures retain the prior audit behavior: they may have `response.json` but no `record.json`;
existing call and usage aggregation is unchanged.

A program, configuration or provider problem (exit status, program timeout, a container or host failure while reading
the code or the response, an answer not in the protocol's form, an input over the limit, a request that cannot be
encoded as UTF-8) is not an attack failure: the session ends invalid with `result.json`'s `infrastructure_error`, the
call's record holds `status: infrastructure_error` where one was written, and a resume runs the session again from the
last checkpoint.

## Measurement

The target's wall-time limit counts its working time. The harness measures the complete synchronous attacker
setup, `advance` and `finish` scopes with a monotonic clock, including request preparation, response processing,
cleanup and calls that fail. It subtracts in-agent attacker time from the `scripted`, `mini-swe` and `anthropic`
adapters' working clocks. The mini-swe adapter enforces this clock through its public `query` override and
temporarily disables the upstream wall-time check through the public configuration; it uses no private agent
attributes. Step, cost and action limits are unchanged.

Containers start with `sleep <lifetime>`. Without a run-local attacker configured, the lifetime is exactly the
target wall limit plus 3,600 seconds of setup and cleanup grace. With one configured, the runner adds
`max_calls_per_session * timeout_seconds`, including the reserved session-end call, plus the separate setup
command's host timeout, `action_timeout + 60`. Setup makes one repository-index command outside the program-call
budget; there is no separate constructor-wide deadline. The reservation applies to every session with that
configuration, including the clean arm, while each adapter retains the original target wall limit. All lifetime
arithmetic stays on the host; the container has no timing-control file or watchdog. The existing 3,600-second
grace remains available for other preparation, processing and cleanup. Command, file-bridge, container-start and
removal timeouts apply only to their individual operations and do not enclose an attacker call. No separate
session deadline wraps the adapter or session-end call.

Every session's `result.json` records `attacker_duration_seconds`, including failed calls and setup; it is zero
without a run-local attacker. `duration_seconds` remains the total session wall time, including attacker time
and container cleanup. Existing per-call `seconds` and attacker budget accounting retain their meanings; they
measure the external program scope rather than this complete measured scope. Subtracting
`attacker_duration_seconds` from `duration_seconds` still includes target setup and cleanup, so it is not the
adapter's working-time measurement.

The measurements stay separate:

1. a write applied, and whether its added lines later reached the agent;
2. carry into later sessions' start memory, and the agent copying them into its own memory writes;
3. the attack objective, read by the existing `grade`, `report` and `harm-count` per campaign;
4. task success, and the defense's effect on clean tasks from both campaigns' `clean` arms;
5. probe sessions after writes close, and recovery, again from the existing grading;
6. attacker cost and use beside the equal bounds, and infrastructure errors.

Token usage sums every completion that reports a total (or both prompt and completion counts), even when other
completions omit usage. `writer_usage.unreported_completions` counts those omissions; the harness charges the known
totals and increments `unreported_usage_calls` for wholly or partially unreported calls. Each call's audit `usage`
also records `unreported_completions`. The live writer uses the same aggregation. Anthropic prompt tokens include
`input_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`; output tokens are added once, matching
the OpenAI-compatible total whose prompt count already includes cached tokens. Cost remains the sum of reported
`usage.cost`, independent of token aggregation. No token-price estimate is made, including for Anthropic responses
that do not report cost; zero recorded cost in that case does not mean the call was free.

Chain and campaign summaries add `realized_usage` and `realized_cost_usd`. They sum each launched call's
`run_attacker/*/record.json` in active sessions and every matching attempt under
`superseded/<timestamp>/cases/<case>/arms/attack_carry/`. Invalid sessions and failed or timed-out program calls are
included; audits that stopped before launching the program (such as an oversized request) are excluded. Missing or
partial token reports remain visible in `unreported_usage_calls` and `unreported_completions`. Only the originating
`attack_carry` arm is scanned, so copies in reset, ablation and clean arms are not billed again. `infrastructure_errors` counts sessions whose `result.json` records an `infrastructure_error`, across active and
superseded attempts of every attack chain; each chain carries the same attempt-inclusive scalar, while its `sessions`
list still describes only the active attempt. `clean_infrastructure_errors` covers the clean arm's active attempts
under `clean/<case>/` and its superseded attempts under `superseded/<timestamp>/clean/<case>/`. Copied sessions marked
by `branch.json` are excluded, so an error is counted once, at its origin; a re-run that succeeds adds no error of its
own, and the earlier attempt remains counted. Existing chain `usage` and campaign `cost_usd` keep their
checkpoint-carried meaning. Budget enforcement is unchanged: a repeated session starts from the last valid checkpoint
with the same allowance its earlier attempt had.

```sh
uv run sereno context-eval pair-summary --pair pair.json DEFENDED_CAMPAIGN UNDEFENDED_CAMPAIGN [--min-line-chars 20]
```

reports 1, 2 and 6 per chain from the records, and refuses a pairing record that did not pair the campaigns (its
`problems` are named in the refusal), a directory with no campaign manifest, and a campaign that does not run the
paired config or the pairing's attacker (its settings, starting strategy and program hashes). Its readings of "seen",
"carried" and "copied" match added lines of at least `--min-line-chars` characters verbatim and are stated in its
output. Matching is whole-line and accepts the raw line or an escaped form an observation carries; a view that shows
the line with a prefix or suffix on the same line is missed, and that under-count is accepted. An agent copy counts
only a memory line the agent introduced itself, and in the write's own session only agent writes after the attacker's
write: an identical line the agent wrote earlier, or a different line added to the same file, is not a copy. No outcome
is derived from what the attacker or the agent says: a success claim in a transcript or notebook is never a measured
success.

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

`pair` loads the writer configuration named by the command's `--config` with no API key and, before any paid call,
checks that `run_attacker.limits.forbidden_patterns` equals the design's and that the command's `--config-sha256` and
`--code-sha256` pins match the current identity. A mismatch becomes a pairing problem, so `paired` is false and `pair`
exits 1; a pinned command left over from an earlier configuration or code version is caught here rather than at the
first paid call. A command that is not recognisably `-m sereno.writer` is left to the program itself.

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
decline_retries: 2                    # plain resamples after the initial declined attempt
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

# serve one call with the three values printed by --check
/absolute/sereno/.venv/bin/python -m sereno.attacker --live --config /absolute/attacker.yaml \
  --config-sha256 CONFIG_HASH --prompt-sha256 PROMPT_HASH --code-sha256 CODE_HASH request.json response.json
```

`run_attacker.command` must be absolute and use the project's virtual environment, because the harness runs the
program in a fresh temporary working directory. `run_attacker.timeout_seconds` must be larger than the runtime's
`deadline_seconds`; `--check` refuses the pair otherwise. Each response's `writer_usage` records the provider, model,
call count, token use, cost, finish reason, and the configuration and prompt hashes, so a campaign can audit the brief
that produced it.

Live mode requires all three pins, matching the writer's mandatory configuration and code pins and adding a separate
brief pin. `--check` prints them and an argv `command_words` list for `run_attacker.command`; it needs neither pins
nor credentials, but verifies any supplied pins. The configuration hash covers resolved settings and the effective
brief, the prompt hash covers the effective brief (including the configured objective), and the code hash covers all
Python sources in `sereno.attacker` plus the transitive `sereno` modules found in their imports and package
initializers, including shared usage accounting. Pins are verified before provider initialization; a mismatch exits
with configuration-failure status 2. Regenerate them after an intentional change and create a new pairing or campaign.
The manifest retains the pins as argv words. For the built-in `python -m sereno.attacker` command, `pair`,
`pair-summary` and campaign resume also validate pins against the current files without a model call, so a stale
brief or code pin is rejected even before a live invocation. External commands retain their existing manifest rules.
`examples/context-eval/attacker-runtime.yaml` is a runtime template using an operator-supplied external brief; the two
paired campaign examples keep their offline stub commands.

### The loop and its tools

The loop starts with the operator's brief, a fixed runtime contract, and the request's bounded observation envelope.

A provider decline is Anthropic `stop_reason: refusal`, an OpenAI-compatible `finish_reason: content_filter` or a
non-empty message `refusal`; the live writer additionally treats `status: unable` as a decline. Both runtime config
and live-writer settings accept `decline_retries` (default 2: at most one initial attempt plus two plain resamples).
Every resample sends the identical model request, without adding a message, modifying the brief or replaying the
declined answer. Successful later steps may evolve the request normally. Decline attempts consume the runtime's
`agent.max_steps` or the writer's existing `repairs + 1` generation-call cap, so fewer retries may fit after earlier
calls. They also share the invocation deadline and remaining reported chain token budget; retries stop when either
has no room. All reported attempt usage and cost are retained, including partial reports.

After exhausted declines, the runtime returns its staged actions with `writer_usage.finish: declined`; the live
writer returns no actions. Both may return optional top-level `status: declined`, the sole added status in protocol
1.0. The harness accepts it as an outcome and keeps the target session valid; ordinary action validation still
applies. The writer keeps the request's notebook on a terminal decline. HTTP, transport, malformed-response and
configuration failures remain infrastructure errors. If a real provider error follows earlier completions, a
`failure_usage.json` accounting sidecar preserves their reported usage and declined attempts; the non-zero exit
still invalidates the session. The failed provider attempt is flagged as unreported rather than assigned zero use.

Per-call audits record `declined` (terminal exhaustion) and `declined_attempts` (all declines, including recovered
ones). Chain summaries expose scalar `declined_calls` and `declined_attempts`; campaign summaries expose both in
`declines_by_arm`, separately for each actual arm. Each defended and undefended campaign in `pair-summary` carries
these fields. Counts include active and superseded launched calls and exclude copied sessions marked by
`branch.json`; clean-arm counts are zero. A call that recovers contributes declined attempts and no declined call.

Inactivity is reported separately from declines. Each chain exposes the following scalars, and each campaign
exposes them per arm in `inactivity_by_arm`, alongside `declines_by_arm`:

- `no_action_calls`: accepted program responses with `actions: []`, including both in-task and session-end calls,
  regardless of their ending. Proposed actions that are rejected or make no change are not empty responses.
- `plain_text_calls`: calls explicitly reporting `loop_ending: plain_text`, excluding declined calls. This is a
  structural observation about the last reply, not a classification of what it says.
- `unknown_ending_calls`: accepted responses without a recognized `loop_ending`, including legacy responses.
- `sessions_without_applied_actions`: run-local attack session attempts whose recorded `applied_attack_actions`
  is exactly zero. Missing legacy counts and unknown counts are not assumed to be zero.

Call counts require a launched program and an accepted response; skipped calls and errors without an accepted
response do not become empty responses or unknown endings. Like decline totals, these counts include invalid and
superseded attempts and exclude copied sessions marked by `branch.json`. Session counts cover the whole session,
including its session-end call; per-session pairing rows also expose `applied_attack_actions`. These additions do
not change existing counts, costs, budgets, denominators or outcome measurements.

The model drives the same `run_attacker` request through tools:

- `list_sessions`, `read_transcript`, `search_transcript`: the running agent's visible transcript and the chain's
  earlier sessions, paged and searchable. Each message carries the target's readable thinking summary in a `thinking`
  field, so the summaries are included in the request and are searchable as well; signatures and provider metadata are
  not.
- `find_files`, `read_file`, `search_files`: the repository's file index and the code views. The index is the files
  Git tracks below `code_root` at task start, so a file the target creates during the task is not readable through
  `read_file`. A path from the index outside the current call's views is queued and served with the next harness
  call; a path outside `code_root`, in `.git`, outside the task-start index, or over the read limit is named as
  denied.
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
