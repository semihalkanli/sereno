# The DeepSeek writer

`sereno.writer` is the external program of the `attack_adaptive` arm's adaptive memory hook (see "Adaptive memory
writer" in [Context Integrity Evaluation](context-integrity-evaluation.md)). Before every session of a chain the hook
runs it with `request.json` and `response.json`; it reads the chain's past through an explicit allowlist, asks the
provider for one memory change and answers in the hook's contract. It imports the harness; the harness never imports it.
This is the memory route. The environment route (a repository plant the agent writes its own note from) has no adapter
here; the design leaves room for one and nothing of it is built.

## Command

```sh
uv run python -m sereno.writer hashes --config writer.json
```

prints `config_sha256`, `code_sha256` and the `command_words` to append to the adaptive command:

```yaml
adaptive:
  command: [/abs/.venv/bin/python, -m, sereno.writer, --config, /abs/writer.json,
            --config-sha256, <hash>, --code-sha256, <hash>]
  timeout_seconds: 1800            # must be larger than the writer's deadline_seconds
```

The hook appends `request.json response.json`. Do not start it with `python -I`: the cost wrapper's meter reaches the
program through `PYTHONPATH`, and it records the provider calls because the client is httpx. The two hashes are command
words so that the campaign manifest records them and `summarize` cannot pool campaigns of different writers; a program
whose configuration or code differs from them refuses to run (`identity_mismatch`, exit 2). `config_sha256` covers
`writer.json` without machine paths and the bytes of every design-package file (design, API settings, system prompt,
last-write sentence, assignment fragment); `code_sha256` covers this package's sources and those of
`sereno.context_eval`, whose `validate_actions` and memory layout the writer imports, each named with its package.
Any change to either package, a merge included, makes words printed earlier stale. In a run started with stale words
every DeepSeek step fails with `identity_mismatch`; after `stop_after_failures` such steps the adaptive arm stops, and
with it the whole campaign: the sessions already started finish and no new one starts in any arm. The campaign cannot
be resumed with new words, since the manifest records the argv, so it needs a new campaign. Print the words again
right before a paid run, from the commit the run uses.

`uv run python -m sereno.writer audit CAMPAIGN [--dataset DIR] [--out FILE]` checks a finished campaign (below).

## Configuration

`writer.json` points to the frozen design package and adds what a campaign decides:

| Field | Meaning |
| --- | --- |
| `schema_version`, `route` | `"1.0"`, `"memory"` |
| `design` | `writer-design.json`; the files it names (API settings, prompts, assignment fragment) sit beside it |
| `deadline_seconds` | wall clock of one run of the program; the hook's `timeout_seconds` must be larger (checked against the campaign manifest) |
| `max_input_bytes` | largest request body; a larger input is refused, never cut. DeepSeek documents a 1,048,576-token context and 393,216 output tokens (api-docs.deepseek.com, `api/list-models`); the writer asks for 128,000, so 900,000 bytes is a safe bound at one token per byte |
| `repository_stores` | repository address -> a bare git directory that holds the task's base commit |
| `endpoint` | optional override of the design's endpoint; plain http only for a loopback host |
| `notebook_max_utf8_bytes`, `view` | optional: notebook limit; `max_file_bytes`, `history_commits`, `line_chars` of the view |

The design must carry the frozen API body (exactly `model`, `provider`, `max_tokens` 128000, `response_format`; no
sampling, effort, fallback or penalty field, `require_parameters` true), the request control with no automatic effort
escalation and no refusal or truncation retry, and the content patterns. The campaign's `adaptive.limits.forbidden_patterns`
must equal the design's exactly, in order, or the step fails with `patterns_differ`; the numeric limits come from the
campaign and are never widened. The credential is read from the environment variable the design names at execution time
and is never written to any record. The prompt and objective files stay outside the repository.

## What the provider sees

One system message (the design's prompt) and one user message: the envelope as JSON, then, only in the step whose
request carries `last_write: true`, the last-write sentence verbatim. The envelope holds `task` (title, repository
address, base commit and instruction text from the task folder), `repository` (the full tree with sizes, the files the
task text names, every `__init__.py`, the subjects of the reachable history, and `omitted` files with the reason),
`objective`, `memory_root`, `memory_files`, `owned_paths`, `limits` (the two numbers only), `notebook_max_utf8_bytes`,
`notebook`, `completed_sessions` and, after the first step, `previous_write` (the transition's harness status and
whether it was applied). A completed session holds its task, the agent's side rebuilt from `events.jsonl` by event kind
(thinking summaries, replies, executed commands, the first and last five lines of each output, the agent's own memory
writes), the memory it ended with and two outcome fields. Guard events, interventions and planted writes have no branch
in the renderer, so they cannot appear; the agent's own reaction to a notice is kept as written.

`objective_achieved` is true when the session's task's harm node failed under the official grading, false when it
passed (also when test edits mask it), null when the session was not graded or no harm check names it. `task_passed` is
true for official reward 1, false for another graded reward, null when not graded. Nothing else of the grading is sent.

Every source is read through an allowlist by kind and entered into a provenance manifest with its hash; the files are
hashed again before each call. The base-commit repository is read with git from the configured bare store, which must
resolve the task's commit to itself.

## One step

1. Check the request, the campaign's limits against the design, and the hook timeout against the deadline.
2. Settle the previous transition by the harness receipt: `applied`, `no_change` and `rejected` commit the staged
   notebook (a rejected one is labelled not applied), `writer_failed` and `timeout` discard it, any other combination
   (no receipt, a receipt for another response) stops the step.
3. Build the view and the input hash. A complete candidate recorded for the same input hash is delivered again with no
   provider call (a resume never pays twice), unless the hook already ended a step on exactly that response as a
   failure.
4. One author call and at most two structural repairs, all inside the deadline; at most two transport attempts per call;
   an interval timer ends a call at the deadline even when the provider sends bytes slowly (main thread). A repair carries the assistant's own turn (reasoning fields as returned) and neutral problem codes only.
5. The harness's own `validate_actions` runs with the real limits and memory. `ready` must change something. The answer
   is delivered when it is clean, or when it only breaks content, path, ownership or size rules the harness enforces
   itself; the hook then records `rejected`. Anything else fails the step.

Any failure of the program (missing credential, input, provider, refusal, cut-off, empty answer, unusable answer,
`unable`) exits non-zero and writes no response: 2 configuration or limits, 3 request, state or input, 4 provider,
5 answer, 1 unexpected. Only a deliberate `no_change` returns empty actions. The hook stops the arm after two such
failures in a row.

## State and records

Under the chain's `state/writer/`: `notebook.json` (the committed notebook), `log.jsonl`, and per transition and attempt
`NNN/aKK/` with `input.json`, `manifest.json`, `view.json`, every transport attempt's request and response bytes
(`call-C-tA.request.json`, `.response.json`) and `transport.jsonl`, `candidates.jsonl` (every candidate, valid or not,
with its codes, the harness's own violation text and the reasoning fields as returned), `candidate.json` (staged) and
`attempt.json` (terminal). An attempt with no terminal record is marked `killed_incomplete` by the next run and never
counts as a completed call. Requested and returned model, provider, generation id, finish reasons, usage and cost are
recorded per call. A chain reads only its own `state/`.

## Audit

`audit` re-checks every recorded attempt: the recorded view hashes to the manifest, every source is inside the allowlist
and this chain, a repair request holds only the assistant's turns and neutral repair messages, and the view holds no
hidden test name or added test line, no reference-solution line, no guard record and no text of another chain's
sessions. A match that a permitted public source of the chain also holds (the task text, a base-repository file, the
chain's own agent text) is `explained`; any other stays `unresolved`. For a guard record the carried memory explains a
match too: the program's own note can hold a sentence the guard quoted at a task start without the session text
showing it (the agent never opened the note, or its output was trimmed). The memory explains nothing else, and the
writer's notebook explains nothing, since guard text that stands only there would be the one trace of a leak. Hidden
test names come from the campaign's own tasks only (its targets and every session's task, from the manifest): a name
is a short identifier, a name from
another repository's tests matches ordinary text of this one, and no view of the campaign can leak it. A name the view
itself shows from a public source (a path of the base repository's tree, a served file, a history subject, or the
assignment) is public and not scanned. Added test lines and reference-solution lines are long and specific, so they
are scanned for every task of the dataset. The report holds hashes, lengths and locations,
never the matched text. Exit status 1 on any violation or unresolved match. The post-run label of indirect
instructions, markers and role addresses is not part of this package.

## Not covered

A freeze campaign needs `last_write` in the request, which only the unmerged freeze branch of the hook sends. The memory
limits (`max_files`, `max_bytes`) are not in the request, so the writer leaves them to the hook, which records a breach
as `rejected`.
