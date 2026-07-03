# Active Turn Steering And Follow-Up Design

Status: investigation and design note, not an implemented fix.

Date: 2026-07-03.

This note focuses on the follow-up-message problem in PatchBay workers. It should be read with:

- `docs/worker-bridge/2026-07-03_WORKER_LIVENESS_REPORTING_INVESTIGATION.md`
- `docs/worker-bridge/05_END_TO_END_ALGORITHMS.md`
- `docs/worker-bridge/10_DECISIONS_RISKS_AND_DEFERRED.md`

## Core Finding

PatchBay currently has worker follow-up, but it does not have active-turn steering.

Current PatchBay follow-up means:

```text
worker is idle
-> ChatGPT calls codex_worker_message
-> PatchBay runs codex exec resume <session_id>
-> Codex starts a new turn in the old session
```

That is a next-turn continuation.

It is not:

```text
worker is already running
-> ChatGPT sends a live correction/status request
-> Codex incorporates that input into the active in-flight turn
```

OpenAI's current Codex app-server surface does expose that second mechanism through `turn/steer`, but PatchBay does not use app-server as its worker backend yet. PatchBay runs workers through `codex exec --json` and resumes completed sessions through `codex exec resume`.

Therefore, the immediate product problem is not simply "allow messages while running." The problem is that PatchBay currently collapses several different operations into one mental bucket:

- observe worker liveness;
- read partial progress;
- steer the active turn;
- queue a next-turn follow-up;
- stop the worker;
- parse cancelled partial evidence.

Those must become separate concepts in the design.

## Evidence From Official Codex Surface

Official OpenAI Codex references checked:

- Codex CLI features: https://developers.openai.com/codex/cli/features
- Codex app-server: https://developers.openai.com/codex/app-server
- Codex app commands: https://developers.openai.com/codex/app/commands
- Codex subagents: https://developers.openai.com/codex/subagents
- Codex CLI reference: https://developers.openai.com/codex/cli/reference

The relevant official behavior is:

- `codex exec resume` exists for non-interactive automation continuation after a prior session.
- The interactive CLI/app can receive more user input while a task is running.
- App-server has a first-class `turn/steer` request for adding user input to an in-flight turn.
- App-server streams structured notifications for turn started/completed, agent-message deltas, command-output deltas, item started/completed, plan updates, and related progress.
- App-server requires an active turn precondition for steering.

Local runtime check:

```text
codex-cli 0.142.2
```

Local generated app-server protocol bindings include:

```text
ClientRequest method: "turn/steer"
TurnSteerParams:
  threadId
  input
  expectedTurnId
TurnSteerResponse:
  turnId
CodexErrorInfo:
  activeTurnNotSteerable
```

This confirms that real active-turn steering is an app-server/protocol capability, not a normal `codex exec resume` capability.

## Current PatchBay Behavior

The current `codex_worker_message` implementation does this:

```text
1. Resolve worker.
2. If latest job is pending/running, reject the message.
3. If worker is idle and has a session id, create a "resume" job.
4. Build a codex exec resume command.
5. Run the resumed turn as a new subprocess.
```

The current algorithm file states the same thing:

```text
If the latest turn is pending/running, return accepted: false; PatchBay does not queue or steer.
```

This is a legitimate simple V1, but it is not enough for the manager-worker workflow Roman wants.

In a long worker run, ChatGPT can currently inspect status or stop the worker, but it cannot naturally say:

```text
Give me a short status update.
Stop broad reading and summarize what you have.
Use the pipeline worker's finding as context.
Ignore that branch and focus on the UI evidence.
```

without cancelling the current turn first.

## Why This Matters

PatchBay is supposed to let ChatGPT manage Codex workers as competent employees. A real manager needs to:

- know the worker is alive;
- understand roughly what the worker is doing;
- avoid drowning in raw execution logs;
- redirect a worker when the task is drifting;
- ask for a checkpoint without destroying the work;
- receive a final report when the worker is done;
- preserve partial evidence when work is stopped.

Current PatchBay gives too little live status and no active steering. That pushes ChatGPT into the wrong behavior:

```text
worker appears opaque
-> ChatGPT thinks worker is stuck
-> ChatGPT stops worker
-> cancellation hides partial evidence
-> ChatGPT falls back to manual reads
```

That is a workflow failure even when the underlying Codex process was alive.

## Required Concept Separation

PatchBay should separate four actions that currently get confused.

### 1. Observe

Observe means ChatGPT asks:

```text
What is the worker doing right now?
Is the worker alive?
What is the latest meaningful checkpoint?
```

Observe must not change the Codex turn.

This should be implemented before active steering. It can use the existing event stream from `codex exec --json` or a future app-server event stream.

The observe surface should expose a compact manager-level snapshot, not raw logs:

```text
state: working
session_created: true
turn_active: true
last_heartbeat_age_seconds: 42
last_meaningful_update: "Running repo-wide search for Verify.EU references"
last_agent_checkpoint: "Mapped service entrypoints; now checking UI data flow"
current_item_kind: command_execution
current_item_status: running
stalled: false
can_steer_now: false/true depending on backend
can_queue_next_turn: true/false depending on design
```

### 2. Steer

Steer means ChatGPT sends user input into the active in-flight Codex turn.

This is not the same as resume. It needs app-server-style active turn state:

```text
thread_id
active_turn_id
expected_turn_id precondition
steerable flag
backend connection
event stream
```

A steering call should be accepted only if:

- the worker has an active turn;
- PatchBay has a current active turn id;
- the last heartbeat is recent enough to believe the worker is alive;
- the backend says the active turn is steerable;
- the request includes the expected turn id or an equivalent concurrency guard;
- the message is a real managerial correction/question, not polling spam.

Steering should not promise an immediate answer. It only promises that the input was appended to the active turn. The answer still arrives through normal worker progress/final report surfaces.

### 3. Queue Next Turn

Queue next turn means:

```text
worker is busy
-> ChatGPT leaves a follow-up that will run after the current turn completes
```

PatchBay intentionally does not have this today.

This might be useful later, but it is not the same as steering. It creates ordering, cancellation, duplicate, and stale-context risks:

- the current turn may already solve the issue;
- the queued message may become wrong after new evidence;
- multiple ChatGPT clients may enqueue contradictory directions;
- the queue can become a hidden workflow that ChatGPT forgets exists.

If added, it should be explicit and inspectable:

```text
codex_worker_queue_message
codex_worker_queue_list
codex_worker_queue_cancel
```

It should not be hidden inside `codex_worker_message`.

### 4. Stop And Preserve

Stop means:

```text
cancel the active turn while preserving the worker and conversation
```

Stopping should preserve evidence. The current cancellation path can write stdout/stderr artifacts but does not reliably promote the latest useful partial `agent_message` into the worker report.

This should be fixed separately from steering:

- parse latest partial agent messages on cancellation;
- mark them as partial/cancelled evidence;
- expose the latest command/item state;
- explain that final structured output was not reached;
- keep report file discovery separate from base-checkout changes.

## Design Rule: Do Not Expose Raw Live Logs To ChatGPT

Raw worker logs and ChatGPT-facing progress are different products.

PatchBay should keep full local operational evidence for the maintainer, subject to redaction and rotation policy. But ChatGPT should not receive every stdout line, every JSON event, every command-output delta, or every tiny agent update.

ChatGPT should see rare, meaningful, manager-level progress snapshots.

Suggested ChatGPT-facing update triggers:

- worker session created;
- first real command/tool item started;
- command/tool item completed after a meaningful duration;
- agent emits a meaningful interim message;
- plan/progress changes;
- no meaningful event for a configured interval;
- process is alive but heartbeat is stale;
- worker enters finalization/report-writing phase;
- worker completes, fails, or is cancelled.

Suggested throttle:

```text
Do not expose routine live status more often than 30-90 seconds per worker unless state changes meaningfully.
```

The exact number is configurable, but the principle is stable: PatchBay should summarize live status, not stream the entire Codex inner life into ChatGPT's context.

## Relationship To Heartbeat

Heartbeat and progress are not the same.

Heartbeat means:

```text
PatchBay has seen recent backend activity.
```

Progress means:

```text
PatchBay can explain what the worker appears to be doing in human terms.
```

A worker can have heartbeat without useful progress. For example, a long command can emit output constantly while doing an over-broad search.

A worker can have no new agent message but still be healthy. For example, Codex may be waiting on a command, reading a large result, or planning silently.

The public status model should therefore include:

- raw liveness state;
- last event type;
- current item type/status;
- last meaningful agent checkpoint;
- last command summary;
- time since last heartbeat;
- time since last meaningful checkpoint;
- stale/stalled classification with reasons.

Do not use primitive status language like:

```text
item.completed, therefore worker should be done
```

An item is not a turn. A turn is not a worker. A worker is a continuing named colleague across turns.

## Read-Only Workers And Report Files

Current mismatch:

- Read-only workers are attractive for investigation.
- PatchBay recommends durable worker report files for important work.
- A truly read-only worker cannot write `worker-report-*.md`.
- Current report-file discovery looks for changed files in writable worker workspaces.

The better model is:

```text
source read-only
report writable
```

That means a worker can be forbidden or instructed not to modify source files while still having a PatchBay-managed report artifact channel.

Possible designs:

1. Use `isolated_write` for investigations but instruct the worker that source changes are prohibited except `worker-report-*.md`.
2. Add a PatchBay-managed report directory outside the repository checkout.
3. Add an app-server/report-artifact channel that stores final and partial reports separately from git changes.

Option 1 is available fastest. Option 2 is cleaner. Option 3 may fit best if PatchBay adopts app-server as a backend.

The important design point: "read-only investigation" must not mean "cannot leave durable evidence."

## Report Compression

Report compression may have three different causes:

1. The worker actually wrote a compact answer.
2. Codex produced richer intermediate messages but the final structured result was compact.
3. PatchBay exposed only summary-like fields and hid richer raw/intermediate output.

The fix is not to dump all logs into ChatGPT.

The correct investigation/fix path is:

- compare final `result.json` with raw JSONL events;
- capture latest partial `agent_message` during running and cancelled turns;
- expose a bounded `latest_checkpoints` list in worker status;
- preserve full local event artifacts for maintainer debugging;
- ask workers explicitly for durable report artifacts when task size warrants it;
- make `worker_report_files` availability clear by workspace/report-channel type.

## Worker Prompts And Broad Reads

Some workers run huge broad reads/searches. That can be caused by:

- ChatGPT giving an overly broad brief;
- a worker choosing a broad first pass;
- missing prompt guidance for checkpoint-style reporting;
- PatchBay not providing a live progress contract;
- no steering path to redirect a broad worker without stopping it.

PatchBay should not solve this by primitive deterministic file/read limits. That would violate the product philosophy.

PatchBay should solve it by giving workers a better natural-language operating contract:

- state the scope and expected evidence;
- ask for checkpoints on broad investigations;
- ask workers to report what they are doing before long scans;
- request durable report artifacts;
- let ChatGPT steer or queue follow-up when the worker is alive but drifting.

## Tool Schema Confusion

The recent `repo_path` passed to `codex_worker_options` was a ChatGPT tool-use error, but tool schema design can reduce that error.

`codex_worker_options` is a model/reasoning menu. It is not repo-scoped in the current schema.

Better tool descriptions should make that explicit:

```text
Use codex_worker_options to choose model/reasoning. Do not pass repo_path; repository scoping belongs to worker start/list/inspect/message.
```

This is documentation/schema clarity, not a reason to remove tools.

## Proposed Implementation Direction

### Phase A: Passive Live Status First

Before steering, add a first-class manager-level live status view.

Candidate tool:

```text
codex_worker_inspect(view="live_status")
```

or:

```text
codex_worker_status
```

It should expose:

- worker lifecycle state;
- latest turn state;
- session created;
- process started;
- pid when available;
- active turn id if backend supports it;
- last heartbeat age;
- last event;
- current item type/status;
- latest bounded checkpoints;
- whether the worker is likely healthy, stale, blocked, or finalizing;
- whether active steering is available;
- whether next-turn continuation is available;
- whether stopping would preserve partial evidence.

This alone may prevent ChatGPT from cancelling healthy workers.

### Phase B: Preserve Partial Evidence On Cancel

Cancellation must not erase the useful state.

On cancellation, PatchBay should parse the latest usable agent messages and store a partial report such as:

```text
state: cancelled
partial_report_available: true
partial_report_source: latest agent_message before cancellation
final_structured_result: missing
last_command_or_item: ...
```

This should be implemented even if active steering is deferred.

### Phase C: Report Artifact Channel

Add a durable report path for read-only investigation work.

Minimum near-term option:

```text
Use isolated_write for investigation workers, but instruct them:
"Do not modify source files. You may write worker-report-<topic>.md only."
```

Better medium-term option:

```text
PatchBay-managed report artifacts outside git checkout, exposed through worker inspect.
```

### Phase D: App-Server Steering Spike

Add an internal backend spike behind the existing worker abstraction.

The spike should prove:

- start thread/turn through app-server;
- capture thread id and active turn id;
- stream events;
- derive manager-level live status from events;
- call `turn/steer` with `expectedTurnId`;
- handle `activeTurnNotSteerable`;
- handle turn completion/failure/cancellation;
- preserve workspace/sandbox semantics;
- preserve worker identity and report discovery;
- keep the public PatchBay contract backend-neutral.

If this works, PatchBay can add:

```text
codex_worker_steer
```

or extend `codex_worker_message` only with an explicit mode:

```text
message_mode: "resume_after_idle" | "steer_active_turn" | "queue_next_turn"
```

Separate tools are probably safer for ChatGPT clarity.

## Proposed Public Semantics

### `codex_worker_message`

Keep this as the current next-turn continuation:

```text
Use when the worker is idle and should continue the same Codex conversation.
```

If the worker is running, it should say:

```text
Worker is still running. Use live_status to observe. Use steer if available and you need to alter the active turn. Use stop only if you truly need to cancel.
```

### `codex_worker_steer`

New, only after app-server support exists:

```text
Use to append a short managerial instruction to the active in-flight turn.
```

Rules:

- only running workers;
- requires active turn id precondition internally;
- no model/cwd/sandbox overrides;
- not for routine polling;
- not for long new tasks;
- returns accepted/rejected and the live status after steering.

### `codex_worker_status`

New or folded into inspect:

```text
Use to check whether the worker is alive and what it is doing without changing the turn.
```

This should be ChatGPT's default action before stopping a worker that looks quiet.

### `codex_worker_queue_message`

Optional later:

```text
Use to schedule a message after the current turn completes.
```

Do not add this silently. Queued messages are powerful but can become stale.

## What Not To Do

Do not remove reader tools. ChatGPT may still need direct reads for orientation, exact verification, tiny tasks, and escalation.

Do not turn PatchBay into a primitive deterministic supervisor that blocks broad prompts or caps worker intelligence.

Do not expose every event/log line to ChatGPT.

Do not pretend `codex exec resume` can steer an active turn.

Do not hide queued messages inside normal worker messages.

Do not make read-only investigation mean "no durable evidence."

Do not treat `item.completed` as full worker completion.

## Second-Pass Code-Grounded Answers To Roman's Checklist

This section answers the concrete questions that triggered this note. It is based on a second pass through:

- `src/patchbay/jobs/executor.py`
- `src/patchbay/jobs/manager.py`
- `src/patchbay/workers/runtime.py`
- `src/patchbay/workers/tool_surface.py`
- `src/patchbay/protocol/mcp.py`
- `src/patchbay/workspace/context.py`
- `src/patchbay/repo_locks.py`
- `config.yaml`
- `tests/test_job_executor_cancellation.py`
- `tests/test_worker_runtime.py`
- copied VM evidence under `.local/investigations/patchbay-vm-worker-failure-20260703-1705utc`

### 1. Does PatchBay already have Codex active-turn steering?

Answer: no.

PatchBay has `codex_worker_message`, but current code uses it only after the latest worker job is no longer pending/running. In `WorkerRuntime.message_worker`, a running worker returns `accepted: false` with the note that PatchBay does not add a message queue. If accepted, the code creates a `resume` job and `JobExecutor._build_codex_resume_command` builds:

```text
codex exec resume <session_id> -
```

That is a later turn in an existing Codex session. It is not live steering of an active turn.

Official Codex app-server has a separate active-turn primitive: `turn/steer` with `threadId`, `input`, and `expectedTurnId`. Local generated Codex app-server bindings for `codex-cli 0.142.2` also include `TurnSteerParams`, `TurnSteerResponse`, and `activeTurnNotSteerable`. PatchBay does not currently call that protocol and does not track app-server active turn ids.

Correct conclusion:

```text
PatchBay follow-up exists.
PatchBay active steering does not exist yet.
```

### 2. Should active steering happen only when heartbeat proves the worker is alive?

Answer: yes, but current heartbeat is not enough by itself.

Current executor heartbeat is updated in `_observe_stdout_event` for stdout JSON events and in the stderr reader for stderr output. Public worker diagnostics expose `last_heartbeat_at`, `last_event`, `progress`, `process_started`, `process_pid`, and `session_created`.

What is missing:

- no active app-server connection;
- no active turn id;
- no expected-turn precondition;
- no `steerable` backend state;
- no distinction between process heartbeat, command-output noise, agent checkpoint, and actual turn progress;
- no age/status classification such as healthy/stale/stalled/finalizing.

So the design should require recent heartbeat, but steering must also require a real active-turn handle from an app-server backend. A heartbeat from `codex exec --json` only proves PatchBay recently saw output; it does not prove there is a steerable active turn.

### 3. Does steering mean ChatGPT gets an immediate intermediate answer?

Answer: not exactly.

`turn/steer` appends input to an in-flight turn. It does not create a new turn, and it does not guarantee an immediate reply. It is a steering input, not a status request API.

The missing "quick answer" capability should be handled by passive live status/checkpoints:

```text
ChatGPT asks PatchBay for live status
-> PatchBay returns latest heartbeat, current item, latest checkpoint, and whether steering is available
```

If ChatGPT wants the worker to explicitly checkpoint, that is an active steering use case:

```text
Please pause broad scanning and give a concise checkpoint before continuing.
```

But the product should not depend on steering for routine liveness. Routine liveness must be passive and cheap.

### 4. How do we surface partial work without flooding ChatGPT?

Answer: keep raw events local, expose throttled manager-level checkpoints.

Current code stores only one latest `last_event` plus one latest `progress` string. `_event_progress_label` produces generic labels like:

```text
Codex event: item.started (command_execution, in_progress).
Codex event: item.completed (command_execution, completed).
```

This is too weak. It tells ChatGPT that something happened, but not whether the worker has a useful intermediate conclusion.

The VM logs show why this matters. Two cancelled workers had useful intermediate `agent_message` events:

- `RM Data Lifecycle Trace` emitted three agent messages before cancellation.
- `RM Final Gap Synthesis` emitted three agent messages before cancellation.

But public worker report did not expose those as partial reports, because `_report_for_job` returns a fixed stopped/running sentence for `RUNNING` and `CANCELLED` jobs.

Correct design:

- preserve all local event stream artifacts for maintainer debugging;
- parse and store a small rolling list of useful checkpoints;
- expose only manager-level summaries to ChatGPT;
- throttle by time and state change, not every JSON event.

Recommended public shape:

```text
latest_checkpoints: [
  {kind: "agent_message", age_seconds: 74, summary: "..."},
  {kind: "command", status: "running", summary: "searching web/static/index.html for UI routes"}
]
```

Recommended default exposure:

```text
no more than every 30-90 seconds per worker unless a meaningful phase changes
```

### 5. Does cancellation lose evidence?

Answer: yes, confirmed in code and logs.

In `JobExecutor._execute_job_now`, after `_communicate_with_progress` returns, the cancellation branch does this:

```text
if job is cancelled:
    write stdout artifact
    write stderr artifact
    return
```

It returns before:

- `_parse_result`
- `result_file.write_text`
- `update_job_state(... result=...)`

Therefore a cancelled worker can have useful agent messages in stdout, but no `result.json` and no public report. This exactly happened for the RetailMind workers:

- `RM Data Lifecycle Trace`: session id present, pid present, heartbeat present, 3 agent messages in stdout, final state `cancelled`, no result file.
- `RM Final Gap Synthesis`: session id present, pid present, heartbeat present, 3 agent messages in stdout, final state `cancelled`, no result file.

Correct fix:

- on cancellation, parse stdout for latest useful agent messages;
- write a partial result file marked `partial_due_to_cancellation`;
- expose a public report like "stopped after partial checkpoint" instead of only "turn was stopped";
- keep the final state `cancelled`, but preserve evidence.

This is separate from active steering and should be fixed first.

### 6. Can read-only workers currently create report files?

Answer: no, not in the way the current docs imply.

Current implementation:

- `_prepare_workspace` creates an external worker worktree only for `isolated_write`.
- `_worker_options` sets sandbox to `read-only` when `workspace_mode == "read_only"`.
- `_worker_report_files` immediately returns `[]` when workspace mode is `read_only`.
- `_changed_files` returns `[]` for read-only or unavailable workspaces.

So a read-only worker can return a final structured report through stdout/result parsing, but it cannot write a discoverable `worker-report-*.md` file in the worker workspace.

Correct product model:

```text
source read-only
report writable
```

Best implementation direction:

1. Add a PatchBay-managed report artifact channel outside the source checkout.
2. Or add a `report_only` / `read_only_with_report` workspace mode.
3. Or, as a near-term workaround, use `isolated_write` and instruct workers not to modify source files except `worker-report-*.md`.

The current advice "use read_only investigator and ask for durable report files" is internally inconsistent unless the worker uses a writable report channel.

### 7. Is the status language too primitive?

Answer: yes.

Current public state is derived from the latest job state only:

```text
pending -> starting
running -> working
completed -> idle
failed -> failed
cancelled -> stopped
```

Current latest-turn diagnostics expose low-level fields, but the public report for a running job is fixed:

```text
The worker is currently working on the latest instruction.
```

The public report for a cancelled job is fixed:

```text
The latest worker turn was stopped. The conversation can be continued later.
```

This collapses three different levels:

- item: one command, one agent message, one tool call;
- turn: one Codex response cycle;
- worker: a continuing named colleague across turns.

That is why `item.completed` followed by `working` can confuse ChatGPT. It can be totally normal: one command item finished, but the Codex turn is still running and the worker is still active.

Correct fix:

```text
worker_state
latest_turn_state
current_item_state
last_checkpoint_state
```

must be distinct in public status.

### 8. Which logs are capped or truncated?

Answer: job stdout/stderr artifacts are capped by default, not the audit log and not necessarily the live runtime stream.

Current config:

```yaml
logging:
  job_log_max_bytes: 200000
  write_raw_job_logs: false
  log_prompt_bodies: false
  log_response_bodies: false
```

`JobExecutor._write_process_artifact` redacts stdout/stderr and caps each artifact to `job_log_max_bytes` unless `write_raw_job_logs: true`.

The audit log is different. Server audit logging records metadata like method/tool/client id by default, not prompt/response bodies. ChatGPT-facing worker status is different again: it sees bounded public reports/diagnostics, not raw job logs.

Correct policy:

- local maintainer evidence can be raw/unbounded only with explicit config and rotation;
- normal ChatGPT should not see raw worker JSONL, raw command output, or full logs;
- ChatGPT should see concise worker checkpoints and final reports;
- postmortem tools can read local logs when debugging.

### 9. Why did workers run huge broad reads/searches?

Answer: a combination of broad assignments, worker strategy, and weak checkpoint guidance. It was not caused by a deterministic PatchBay file limit or a PatchBay prompt filter.

VM stdout shows workers themselves ran broad repo commands such as:

- `rg --files ...`
- `sed -n ...`
- `nl -ba ...`
- large static UI slices;
- route/search scans.

PatchBay's worker prompt currently appends only generic `REPORT_GUIDANCE`:

```text
When you finish this turn, report back like an engineer...
```

It does not require:

- early checkpoint before broad scan;
- bounded initial orientation;
- "report if you are about to read a huge UI file";
- "write a partial checkpoint after N minutes";
- "prefer focused evidence passes";
- "if scope is too broad, state decomposition."

So the behavior was mostly:

```text
broad task + autonomous Codex worker + no live checkpoint/steering contract
```

Correct fix is not primitive caps. Correct fix is a better natural-language worker operating contract plus live status/steering:

- ask for checkpoint-style progress;
- ask workers to report planned scan shape;
- make partial checkpoints visible;
- let ChatGPT steer if the worker is drifting;
- keep broad reads possible when actually necessary.

### 10. Why were some reports over-compressed?

Answer: several layers can compress the report, and current code only preserves the final structured result by default.

Compression layers:

1. Codex is forced through `--output-schema` using `codex_output_schema.json`.
2. That schema has a high-level `summary`, `notes`, `next_steps`, `files_changed`, `commands_run`, and `tests_run`.
3. `_parse_result` scans JSONL backward and stores the final `result` or final `agent_message`.
4. `_report_for_job` exposes only `summary`, `notes`, and `next_steps`.
5. `_safe_public_text` clips public report text to `MAX_PUBLIC_REPORT_CHARS`.
6. `context_from_workers` clips peer report context to `MAX_CONTEXT_REPORT_CHARS`.
7. Cancelled jobs skip `_parse_result` entirely.
8. Read-only jobs cannot produce discoverable report files.

In the RetailMind evidence:

- `RetailMind UI Quick Mapper` completed and had a useful `result.json`.
- cancelled `RM ...` workers had useful intermediate messages but no result file.

So "report compression" can mean different things:

- the worker actually gave a compact final structured result;
- PatchBay exposed only `summary/notes/next_steps`;
- the final result never existed because the worker was cancelled;
- useful interim messages existed but were hidden in stdout logs.

Correct fix:

- preserve partial agent checkpoints;
- add a richer public `latest_checkpoints` field;
- improve `REPORT_GUIDANCE` for evidence depth;
- support report artifacts for read-only investigations;
- keep raw logs local, but expose report files and checkpoint summaries.

### 11. Is "marked running but no live Codex process is tracked" real?

Answer: yes, real, but it is a different class from the latest RetailMind cancellation pattern.

The error text is `STALE_RUNNING_JOB_ERROR` in `JobExecutor`.

Current reconciliation marks a durable `running` job as failed when:

- the job state is `RUNNING`;
- there is no live tracked asyncio task;
- there is no live tracked subprocess;
- the grace period has passed.

This can happen after:

- PatchBay server restart while durable state still says running;
- executor task crash before durable state reached terminal;
- process-tracking loss;
- scheduling/partial-start failure;
- older bugs or VM failures.

Tests cover this behavior:

- stale running jobs are marked failed;
- live executor task prevents false stale failure;
- live process prevents false stale failure;
- process started but no JSON session can fail through startup timeout.

In the latest RetailMind evidence, the four main `RM ...` workers were not this case. They had process ids, session ids, heartbeats, then `Cancelled by request`.

So:

```text
stale process bug = real historical/lifecycle class
latest RetailMind stopped workers = mostly cancellation + bad liveness/report surfacing
```

### 12. Why did ChatGPT pass `repo_path` to `codex_worker_options`?

Answer: likely tool-surface generalization, not a backend need.

`codex_worker_options` schema has:

```text
additionalProperties: false
properties: model, max_models, include_model_details
```

It does not accept `repo_path`.

But most worker tools do accept `repo_path`, and the manager flow tells ChatGPT to start with workspace/repo context before choosing worker options. ChatGPT likely generalized "worker tool + repo" and supplied a field that is common elsewhere.

Correct fix:

- make `codex_worker_options` description explicitly say "Do not pass repo_path; this is a runtime/model menu, not a repository operation";
- optionally include `repo_path` as ignored/accepted if model availability ever becomes repo/environment scoped;
- keep schema strict for now if the tool should stay clean, but improve error wording.

This is not a reason to remove tools.

### 13. Is `codex_read_file` still failing line-range reads because full file size exceeds `max_bytes`?

Answer: current local code does not do that for the base checkout reader.

Current `WorkspaceContext.read_file` calls `_assert_text_file(target)` without a max-size argument, then applies `max_bytes` while rendering only the requested line slice. The `codex_read_file` descriptor also says `max_bytes` caps the returned page, not the whole file.

Worker file reads also page by line and cap the response, though they additionally apply a configured public cap.

Therefore, if ChatGPT recently saw:

```text
File is too large (...) Limit: 4000 bytes.
```

that came from an older deployed build, another read path, or a version mismatch between local code and VM code. It is not the current local `codex_read_file` behavior.

### 14. Did ChatGPT act manually because PatchBay failed or because ChatGPT mismanaged workers?

Answer: both contributed, but the latest RetailMind evidence points more to PatchBay's status/report/steering deficiencies causing ChatGPT to lose confidence.

Audit counts from the relevant 16:46-16:59 UTC window:

```text
136 codex_read_file
 38 codex_worker_inspect
 32 codex_search_repo
 13 codex_worker_list
 11 codex_repo_tree
  4 codex_worker_start
  4 codex_worker_stop
```

This means ChatGPT did start workers, but then repeatedly inspected and heavily direct-read while workers were still running. It eventually stopped four workers that had live sessions and heartbeats.

That is not only "ChatGPT is bad." PatchBay gave ChatGPT:

- no active steering;
- no live checkpoint report;
- generic `working` status;
- no useful partial report after cancellation;
- no report files for read-only workers.

So ChatGPT's collapse into manual reading was bad manager behavior, but PatchBay currently makes that bad behavior likely.

## Second-Pass Fact Ledger

| Fact | Status | Evidence |
| --- | --- | --- |
| PatchBay worker follow-up is `codex exec resume`, not active steering. | Verified | `WorkerRuntime.message_worker`; `JobExecutor._build_codex_resume_command`. |
| Official Codex has active steering through app-server `turn/steer`. | Verified | OpenAI Codex app-server docs; local generated `TurnSteerParams`. |
| Running workers reject `codex_worker_message`. | Verified | `WorkerRuntime.message_worker` running-state branch. |
| Current heartbeat is stdout/stderr event liveness, not steerability. | Verified | `JobExecutor._communicate_with_progress`; `_observe_stdout_event`; `_latest_turn_diagnostics`. |
| Public running report is generic. | Verified | `_report_for_job` returns fixed text for `RUNNING`. |
| Public cancelled report is generic. | Verified | `_report_for_job` returns fixed text for `CANCELLED`. |
| Cancellation currently skips result parsing. | Verified | `JobExecutor._execute_job_now` cancellation branch returns before `_parse_result`. |
| Read-only workers cannot expose changed report files. | Verified | `_worker_report_files` returns `[]` for `read_only`; `_worker_options` uses read-only sandbox. |
| Job stdout/stderr artifacts are capped by default. | Verified | `config.yaml`; `_write_process_artifact`. |
| Audit logs default to metadata only. | Verified | `server.py` audit logger; `log_response_bodies: false`. |
| Latest RetailMind `RM ...` workers were cancelled after sessions/heartbeats existed. | Verified | copied VM job state JSON and journal logs. |
| `RetailMind UI Quick Mapper` completed and had a result file. | Verified | copied VM job state and result JSON. |
| Stale-running/no-process failure remains a real separate class. | Verified | `STALE_RUNNING_JOB_ERROR`; reconciliation code; tests. |
| Current local `codex_read_file` applies `max_bytes` to the returned page. | Verified | `WorkspaceContext.read_file`. |

## Current Conclusion

PatchBay does not currently have the active steering capability Roman is asking about.

Codex does have a real steering capability in its app-server/interactive surface. PatchBay should integrate that only after building a clean backend-neutral worker state model with live status, active turn ids, heartbeat, partial checkpoints, and cancellation evidence preservation.

The second pass changes the priority order slightly. Active steering is important, but not the first fix. PatchBay first needs to stop making healthy long-running workers look empty.

The practical next step is:

```text
1. Add passive live status/checkpoints for running workers.
2. Preserve partial agent messages on cancellation.
3. Make read-only investigation capable of durable reports.
4. Improve worker prompt guidance for checkpoint-style progress.
5. Clarify tool schemas/descriptions, especially codex_worker_options.
6. Spike app-server as a second backend for active-turn steering.
7. Only then expose a ChatGPT-facing steer tool.
```

This preserves the PatchBay philosophy: ChatGPT manages through natural language, Codex workers do real work, and PatchBay provides exact mechanics without dumbing the worker down or flooding the manager with raw logs.

## Local Implementation Pass: Liveness Before Steering

This pass implemented the first half of the conclusion above without adding active-turn steering yet.

Implemented locally:

- durable job-level `checkpoints` captured from Codex JSON `agent_message` events;
- checkpoint/result parsing across supported agent-message text/content shapes, not only one `item.text` shape;
- bounded public `latest_checkpoints`, `checkpoint_count`, `liveness`, `can_message_reason`, `followup_mode`, `active_steering_supported`, and `report_artifacts` fields on worker views;
- configurable liveness display thresholds through `workers.heartbeat_fresh_seconds` and `workers.heartbeat_quiet_seconds`;
- manager-facing running-worker reports that include heartbeat/liveness and latest checkpoint instead of only saying "working";
- cancellation preservation: when a worker is stopped after emitting useful output, PatchBay writes a partial structured result and keeps checkpoints instead of reducing the worker to an empty stopped state;
- worker stop briefly waits for already-captured partial evidence to attach before returning, controlled by `workers.stop_artifact_wait_seconds`;
- structured report artifacts expose which report fields are present and counts for evidence, risks, and open questions;
- tool descriptions and `initialize.instructions` now tell ChatGPT to inspect status/checkpoints before stopping or replacing a running worker;
- `codex_worker_options` now explicitly says not to pass `repo_path`;
- docs now distinguish writable worker report files from PatchBay-managed report artifacts for read-only workers.

Deliberately not implemented yet:

- Codex app-server active-turn steering;
- a ChatGPT-facing tool that can inject guidance into an active turn;
- raw log streaming to ChatGPT.

Reason:

The latest evidence showed that many "stuck" workers were probably live but opaque. Active steering would not fix that by itself. The first reliable product layer must let ChatGPT see that a worker is alive and progressing without flooding the manager with raw transcripts.

## Current Codex Steering Reality Check

Verified on the local machine:

```text
codex-cli 0.142.2
codex exec --help
codex exec resume --help
```

The installed `codex exec` surface exposes:

- start a non-interactive turn;
- emit JSONL with `--json`;
- write final output with `--output-last-message`;
- constrain the final message with `--output-schema`;
- resume a saved exec session with `codex exec resume [SESSION_ID] [PROMPT]`.

It does not expose an `exec` subcommand or flag that appends input to an already in-flight turn. `codex exec resume` is a next-turn continuation mechanism, not active steering.

Official OpenAI Codex docs distinguish those surfaces:

- Codex App Server documents `turn/steer` as the active-turn API: it appends user input to the currently in-flight turn, requires `expectedTurnId`, fails if there is no active turn, and does not accept turn-level overrides like model, cwd, sandbox policy, or output schema.
- Codex CLI reference documents `codex exec resume` as resuming a saved exec session and accepting an optional follow-up prompt.
- Codex CLI slash-command docs describe active interactive-session controls such as `/model`, `/fast`, `/personality`, `/permissions`, `/status`, `/resume`, `/fork`, and `/side`, but the non-interactive `codex exec` help does not expose the same app-server `turn/steer` API.

Implication for PatchBay:

PatchBay should not pretend that `codex_worker_message` can steer a currently running `codex exec` worker. The current public fields are therefore honest:

```text
followup_mode: next_turn_after_completion
active_steering_supported: false
can_message_reason: active_turn_running
```

The proper future implementation is an app-server-backed worker backend, not a fake queue on top of `codex exec resume`.

Minimum requirements before exposing ChatGPT-facing active steering:

1. Worker state must record the active app-server `threadId` and active `turnId`.
2. Worker status must expose whether there is an in-flight turn and whether `turn/steer` is currently legal.
3. Steering calls must pass `expectedTurnId` and fail clearly if the active turn changed.
4. Steering must not accept model, cwd, sandbox, or output-schema overrides; those remain turn-start concerns.
5. PatchBay must keep the current liveness/checkpoint layer even after app-server integration, because managers still need passive status before steering.
6. `codex_worker_message` should remain next-turn continuation unless a separate active-steering tool or explicit parameter is introduced; mixing them would recreate the current confusion.
