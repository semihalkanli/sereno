# Attack layer concept: a thinking-aware adaptive attacker for persistent memory poisoning

This document describes the attack layer end to end: the external attacker that reads the
target agent's repository code and transcript, adapts its private strategy inside a run, and
writes only through the target's persistent memory. It is a design for the experiment, not an
operational tool, and it contains no memory-note text or operator brief.

## 1. Purpose and threat model

The target is a long-horizon coding task chain. Each task is one session, and memory carries
between sessions under the experiment's existing rules. The attack objective is persistent
memory poisoning: the attacker writes a memory note in an exposure session so that a later
session's agent reads it and acts on it. That is different from ordinary prompt injection.
The attacker never edits the task prompt, the harness, tool permissions, model weights, or the
target repository's instructions. Its only effect on the target is the memory surface the
adaptive hook already permits (`src/sereno/context_eval/run_attacker.py:502-541`).

The attacker has two capabilities that earlier versions did not have together:

- it can read the code the target agent is working on, including the current file contents;
- it can read the target agent's transcript, including the target's readable thinking summaries.

The attacker uses those observations to revise its own strategy during the run. The unit of
adaptation is one attack chain; nothing learned in one chain is carried into another case,
repeat, arm, campaign or run. This keeps the measurement a result about an attacker that
adapts within a chain, not about an attacker trained across the whole experiment.

## 2. Architecture

The attacker is a separate external program behind the existing `run_attacker` protocol 1.0.
The harness calls it synchronously before the target agent's model calls and once at the end
of a completed session. One call is one request and one response; the runtime keeps no state
of its own between calls.

```
campaign config (run_attacker)
        |
runner.run_session: advance / finish          runner.py:146-377
        |
request.json: contract + strategy/notebook + observation
        |                                         run_attacker.py:418-463
        v
external attacker runtime: bounded model loop     attacker/agent.py:73-143
  tools: find_files/read_file/search_files        attacker/tools.py:351-433
  tools: list_sessions/read_transcript/search_transcript
  tools: read_memory/stage_action/drop_action
  tools: set_strategy/set_notebook/finish
        |
response.json                                     protocol.py:116-148
        |
harness validates and applies memory actions       run_attacker.py:502-541
        |
target memory -> next session start memory -> target behavior -> grade/harm-count
```

The request (`src/sereno/context_eval/run_attacker.py:418-463`) has three parts:

1. `contract`: call kind (`in_task` or `session_end`), stage (`exposure` or `probe`), whether
   writes are open, the memory root, the limits, the budget and its use so far, and the
   response's keys.
2. `strategy` and `notebook`: the attacker's private, evolving state.
3. `observation`: the session and task, the code index, code views, requested files, the
   visible transcript, earlier sessions of the chain, the carried memory, the previous call's
   result, and the session summary.

The response is one object with `schema_version`, `actions`, optional `strategy`, optional
`notebook`, optional `reads`, optional `writer_usage` and optional `loop_ending`
(`src/sereno/attacker/protocol.py:116-148`).
A configuration, program or provider failure writes no response; the harness records it as an
infrastructure error, not as an attack outcome.

## 3. Observation surfaces

**Target repository code.** At task start the harness records the files Git tracks below
`code_root` (`src/sereno/context_eval/run_attacker.py:183-194`), and that index stays fixed for the
session: a file the target creates later is not readable through the attacker's tools. The
configured `code_paths` are served as structured views, and an indexed file the previous call asked
for is read live on the next call (`src/sereno/context_eval/run_attacker.py:196-225`). A path outside
`code_root`, into `.git`, through a symlink that leads out, outside the task-start index, or over the
read budget is named as denied or queued, never silently cut. Within that fixed index the attacker
reads the code the target is working on, not a frozen copy of the task description.

**Target transcript.** The harness passes the messages the target agent has recorded so far,
including tool calls and results and the guard notices the target saw
(`src/sereno/context_eval/run_attacker.py:106-140`). The request's transcript is the running
session; `completed_sessions` carries earlier sessions of the same chain, and the runtime's
`Transcript` class pages and searches both (`src/sereno/attacker/observation.py:112-303`).
The first user message is a bounded tail; everything cut stays reachable through
`read_transcript` and `search_transcript`.

**Thinking summaries.** The target agent runs with thinking enabled and its recorded messages
carry the provider's thinking blocks. `visible_transcript` exposes the readable thinking text
in a `thinking` field on each visible message, taking it from the provider's `reasoning` or
`reasoning_content` field or from thinking content blocks; signatures, redacted data,
`thinking_blocks` and provider metadata are not carried. The runtime's `render_message` shows
the field as `[thinking] ...`, so it appears in the first envelope, in paged transcripts and
in search results (`src/sereno/attacker/observation.py:68-90`). A redacted block with no
readable text is named as redacted, not decoded.

**Memory and earlier sessions.** The request carries the memory files that load for the
project and the paths the attacker owns (`src/sereno/context_eval/run_attacker.py:418-463`).
Earlier sessions arrive as short cards plus their transcripts, so the attacker can compare
what it intended with what the target did in the preceding task. The previous call's result
says whether its actions were applied, rejected, not applied, or made no change; that is the
feedback the attacker uses to revise its strategy.

## 4. Thinking and self-evolution

Self-evolution here means bounded strategy adaptation inside one chain. It does not mean
training model weights, fine-tuning, a task-specific script, or a policy that is reused across
runs.

- Each call may replace `strategy` and `notebook`. The harness versions the strategy, hashes
  it, and carries it to the next call; a valid update is kept even when the call's memory
  actions were rejected or not applied (`src/sereno/context_eval/run_attacker.py:280-416`).
- The session-end call sees the completed session's transcript and summary. It is the
  reflection point: the attacker can revise the strategy for the next session of the chain.
  It can observe, but it cannot write memory at that point.
- The next session's first call carries the previous strategy, the notebook, the earlier
  sessions and the previous result, so the attacker can adapt to the target's observed
  behavior rather than start from the same seed every time.
- Thinking summaries are the strongest signal for this adaptation: they show what the target
  believed, which parts of the task it considered settled, and whether a guard notice changed
  its plan. The attacker still has to prove an effect through the memory surface; a claim in
  its own strategy or notebook is never a measured success.

The design keeps the attacker model-agnostic. The loop and tools never read a provider's body
(`src/sereno/attacker/agent.py:73-143`); the provider is a configuration choice.

## 5. Action space and persistence

The only target-side effect is a memory action: write a file below the project's memory root,
or delete a file the attacker created. Actions are staged during the call and applied by the
harness after the call, and only when the call is an `in_task` call of an exposure session
(`src/sereno/context_eval/run_attacker.py:502-541`). A probe session and the session-end call
never apply actions. The harness checks path canonicality, ownership, append-only rules, file
size, content rules, the memory limits and the write budget; any violation rejects the whole
call's actions and leaves memory unchanged.

Persistence is therefore a property of the experiment, not of a single message. An applied
write lands in the memory files and becomes visible to the target through later real memory
reads and the next session's memory loading. Messages already prepared for the current model
call are not rewritten. The later sessions are where carry, copy and harm are read; a write
that never reaches a later session is not persistence.

## 6. Provider architecture

DeepSeek is a provider configuration, not a code path. `ProviderSettings` selects
`openai-compatible` or `anthropic`, the model, the base URL, the credential environment
variable and provider-specific request fields (`src/sereno/attacker/config.py:47-62`). A
DeepSeek configuration uses:

```yaml
provider:
  kind: openai-compatible
  model: deepseek-chat
  base_url: https://api.deepseek.com/chat/completions
  credential_env: DEEPSEEK_API_KEY
```

Another model of the same API is a change to `model` and the `request` block. The runtime
sets no model parameters of its own: the `request` block is passed through unchanged, so every
parameter stays at the provider's default, except the `max_tokens` an Anthropic provider must
set (`src/sereno/attacker/config.py:162-175`). Another API is one adapter module under
`src/sereno/attacker/providers/` implementing `Provider` and added to `build_provider`
(`src/sereno/attacker/providers/base.py:89-96`,
`src/sereno/attacker/providers/__init__.py:19-32`). The operator supplies the brief; the
runtime writes no attack text. The configuration and prompt hashes are recorded with the
response's `writer_usage` (`src/sereno/attacker/protocol.py:97-113`).

## 7. Budgets and measurement

The harness bounds one call by a timeout below the harness's own deadline, and a chain by
maximum calls, calls per session, tokens, seconds and writes. Reads, read bytes, index
entries, observation bytes, tool-output bytes and transcript paging have their own limits
(`src/sereno/context_eval/run_attacker.py:418-463`). A call starts only while every budget
bound has room; realized use can pass a bound by at most the one call that crossed it, and a
skipped call is recorded once per session and reason.

Synchronous attacker setup and calls have their own session total, `attacker_duration_seconds`.
The target adapters subtract in-agent attacker time from their wall-time checks. Containers
retain `sleep` as PID 1; the host reserves the configured maximum program-call time per
session plus the separately bounded setup command when an attacker is configured.
`duration_seconds` retains total session wall time. Per-call seconds and attacker budget
accounting keep their existing external-program scope.

The program reports its loop ending as optional metadata in protocol 1.0. The loop's own control
flow is unchanged: a reply without tool calls is nudged and the call continues while steps remain.
When the loop stops at a bound it already has and the last reply carried no tool call, the ending
is reported as `plain_text`, or as `empty_reply` when the reply was empty, without text
classification.
Per-call audits retain the ending and the number of actually applied actions; session results
retain the applied-action total, with unknown counts left unknown. Pairing adds `no_action_calls`,
`plain_text_calls`, `unknown_ending_calls` and `sessions_without_applied_actions` per chain and
per campaign arm in `inactivity_by_arm`, separately from declines. Missing ending metadata is
unknown. Existing counts and outcome measurements do not change
(`src/sereno/context_eval/pairing.py:165-189`).

The measurement is a paired campaign. The defended and undefended configurations differ only
in the guard and its notices; the attacker's settings, starting strategy, program, task order,
sessions, start memory and budgets must match. The readings stay separate:

1. a write was applied, and whether its added lines later reached the target agent;
2. the write was carried into a later session's start memory, and the target copied it into
   its own memory writes;
3. the attack objective, read by the existing grade, report and harm-count per campaign;
4. task success, and the defense's effect on the clean arms of both campaigns;
5. probe sessions after writes close, and recovery;
6. attacker cost and use beside the equal bounds, and infrastructure errors.

No outcome is derived from what the attacker or the target says. A success claim in a
transcript or notebook is not a measured success. The independent unit for uncertainty is the
chain, not the individual session.

## 8. Boundaries and out of scope

- The attacker never changes harness code, tool permissions, the target prompt, model weights,
  or the target repository's instruction files.
- The attacker never trains across runs and never carries state across chains. The strategy and
  notebook live on one chain's engine and are checkpointed per completed session.
- Attacks on repository content or tool output, a critic or separate attack search, and
  combination with the between-session adaptive writer are out of scope in this version.
- No paid model call is part of this design. The validation is model-free: the full test
  suite, a loopback provider test and an offline campaign with the scripted agent.
- The attacker's thinking summaries are the target's own reasoning text, not hidden guard
  decisions, grader outputs or another chain's data. Guard notices are visible because the
  target saw them; hidden guard records are not.

## 9. File map

| Path | Role |
| --- | --- |
| `src/sereno/context_eval/run_attacker.py` | Harness side: request, transcript, code reads, action application, budget and checkpoint. |
| `src/sereno/context_eval/runner.py` | `run_session` wires the attacker into the campaign before model calls and at session end (`src/sereno/context_eval/runner.py:146-377`). |
| `src/sereno/attacker/agent.py` | Bounded tool loop and the runtime contract. |
| `src/sereno/attacker/observation.py` | Bounded envelope, transcript paging/search and thinking rendering. |
| `src/sereno/attacker/tools.py` | The tool catalog and the per-call toolbox. |
| `src/sereno/attacker/protocol.py` | Request validation and response form. |
| `src/sereno/attacker/config.py` | The operator brief, provider block, loop bounds and hashes. |
| `src/sereno/attacker/providers/` | `openai-compatible` and `anthropic` adapters behind one interface. |
| `docs/run-local-attacker.md` | Usage, protocol, measurement and the runtime's tool list. |
| `tests/test_context_eval_run_attacker.py` | Harness-side transcript and action tests. |
| `tests/test_attacker_tools.py` | Runtime-side transcript and tool tests. |

The design's required behavior is the `thinking` field described in section 3.
`visible_transcript` (`src/sereno/context_eval/run_attacker.py:106-140`) produces it in every
provider spelling, and its tests (`tests/test_context_eval_run_attacker.py:215-283`) pin the
top-level `reasoning` shape, `reasoning_content`, joined thinking blocks and the redacted
marker.
