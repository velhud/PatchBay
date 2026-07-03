# Worker Liveness And Report Surfacing Investigation

Status: investigation note, not an implemented fix.

Date: 2026-07-03.

Primary evidence source:

- Local copied VM evidence under PatchBay's gitignored `.local/investigations/` workspace.
- VM journal window: 2026-07-03 16:40-17:10 UTC.
- Runtime state copied from `/var/lib/patchbay/runtime`.

This note describes a possible deeper problem in PatchBay's worker bridge: the Codex workers may often be working normally, but PatchBay does not show ChatGPT enough live, manager-usable evidence that they are alive and making progress. ChatGPT then interprets a healthy long-running worker as stuck, stops it, and later reports that worker execution or report retrieval was unreliable.

The issue is important because PatchBay's purpose is long-horizon natural-language worker management. If ChatGPT cannot distinguish "worker is alive and still investigating" from "worker is dead," it will abandon delegation, cancel workers too early, and fall back to manual file reading. That breaks the intended manager-first workflow even when Codex and PatchBay are technically still operating.

## Short Version

The latest RetailMind run did not look like a full PatchBay execution failure.

The relevant workers had:

- Codex process started.
- Codex session created.
- JSON event stream present.
- Real command events present.
- Some intermediate agent messages present.
- No PatchBay service crash in the inspected window.
- Manual `codex_worker_stop` calls near the end.

The failure pattern looks more subtle:

1. ChatGPT starts workers.
2. Workers run broad reads/searches/commands.
3. PatchBay reports only generic `working` state plus sparse event labels.
4. PatchBay does not expose useful live partial reports.
5. `codex_worker_message` rejects follow-up while the latest turn is still running.
6. Read-only workers cannot create discoverable `worker-report-*.md` files.
7. Large stdout artifacts are capped and only written after exit/cancel.
8. ChatGPT sees no final report and no conversational way to ask the worker what is happening.
9. ChatGPT decides workers are stuck or unproductive.
10. ChatGPT stops them and falls back to direct `codex_read_file` / `codex_search_repo`.

So the suspected root problem is not just "worker execution is broken." It may be "worker liveness and intermediate status are not surfaced in the way a ChatGPT manager needs."

## What ChatGPT Reported

The user-provided ChatGPT report said:

- Several Codex workers stayed in `working` after apparent progress events.
- Workers did not surface usable final reports.
- Workers such as `RM UI Surface Complete Pass`, `RM Data Lifecycle Trace`, `RM Evidence Review VerifyEU Pass`, and `RM Final Gap Synthesis` were eventually stopped.
- Some workers showed events like `item.completed` but still appeared as `working`.
- One worker reportedly failed with a tracking mismatch: "Job was marked running, but no live Codex process is tracked."
- Some reports were over-compressed.
- `worker_report_files: []` appeared for several workers.
- ChatGPT switched back to bounded direct inspection because worker delegation became inefficient.

That report is not fake, but the logs suggest it may partly describe ChatGPT's interpretation rather than the underlying runtime reality.

## Runtime Reality From Logs

In the copied runtime state, the main latest RetailMind workers were not missing sessions. They had process IDs and Codex session IDs.

Timestamps below are UTC.

| Worker | Job id | Started | Final state | Last event | Session? | Result file? | Notes |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| `RM UI Surface Complete Pass` | `0507a865-8ae9-4af1-bc08-ec92e4c0bb89` | 16:46:41 | `cancelled` | `item.started` | yes | no | Cancelled at 16:54:59 while a command was in progress. |
| `RM Data Lifecycle Trace` | `7ccba03c-58f3-4c6a-b142-064d419be8a1` | 16:47:03 | `cancelled` | `item.started` | yes | no | Contained several intermediate agent messages and many command events; stdout hit 200 KB cap. |
| `RM Evidence Review VerifyEU Pass` | `1e9ef17d-e54f-4508-b5a0-e6566636c9fc` | 16:47:25 | `cancelled` | `item.started` | yes | no | Very early in a broad search/read command when cancelled. |
| `RM Final Gap Synthesis` | `18e2f11f-1229-40f7-904d-63e3a8040f52` | 16:47:46 | `cancelled` | `item.started` | yes | no | Contained several intermediate agent messages; stdout hit 200 KB cap. |

Important contrast:

| Worker | Runtime state |
| --- | --- |
| `RetailMind UI Quick Mapper` | `completed`, `last_event=turn.completed`, result file present. |
| `RetailMind Pipeline Capability Mapper` | `completed`, `last_event=turn.completed`, result file present. |
| `RetailMind Docs Reality Mapper` | multiple completed turns with result files. |

The copied state for `RetailMind UI Quick Mapper` does not support the idea that this exact worker was failed at the end of the inspected window. It was completed in the runtime state available locally.

## Tool Usage Pattern During The Latest Run

From the VM audit log for approximately 16:46-16:59 UTC:

| Tool | Count |
| --- | ---: |
| `codex_read_file` | 136 |
| `codex_worker_inspect` | 38 |
| `codex_search_repo` | 32 |
| `codex_worker_list` | 28 |
| `codex_worker_options` | 14 |
| `codex_repo_tree` | 11 |
| `codex_worker_start` | 4 |
| `codex_worker_stop` | 4 |

This is strong behavioral evidence that ChatGPT did try to use workers, but then repeatedly inspected them, did a large amount of manual reading, and eventually stopped them.

That is exactly the failure mode the user is worried about: PatchBay did not keep ChatGPT confidently in manager mode.

## What The Worker Event Streams Showed

The stdout event streams show that workers were doing real work before cancellation.

Examples:

- `RM Data Lifecycle Trace` had `thread.started`, `turn.started`, multiple `item.started` / `item.completed` command events, and several `agent_message` events.
- `RM Final Gap Synthesis` had several partial `agent_message` updates describing current findings.
- Some workers were still in command execution when stopped.
- Several logs were truncated at approximately 200 KB with `...[log truncated to 200000 bytes]`.

This means a worker could be "alive" from the Codex point of view while PatchBay's public report still looks unhelpful.

## Why ChatGPT May Have Thought Workers Failed

### 1. `working` Is Too Opaque

For a running worker, PatchBay's public report can be as little as:

```text
The worker is currently working on the latest instruction.
```

That does not tell ChatGPT whether the worker is:

- reading files normally;
- stuck in a large shell command;
- producing useful intermediate analysis;
- waiting on the model;
- burning time on a bad command;
- about to finish;
- dead but unreconciled.

For a human manager, "still working" for several minutes is not enough. A manager needs a meaningful heartbeat.

### 2. Intermediate Agent Messages Are Not Promoted To The Current Report

Some cancelled workers did emit useful intermediate `agent_message` events.

For example, `RM Final Gap Synthesis` emitted messages such as:

- it had mapped the repo as compact Python service code plus a static web UI;
- it was separating real behavior from roadmap language;
- it had identified Verify.EU as a stronger source-traced sub-workspace than the base retail intake pipeline.

But because the turn had not completed, PatchBay did not expose those intermediate messages as the worker's current report. After cancellation, the public report becomes basically "the turn was stopped," not "here is the latest useful partial report from that stopped turn."

That makes a useful-but-not-finished worker look empty.

### 3. `codex_worker_message` Cannot Interrupt Or Ask For Status

The current worker runtime refuses to message a worker while the latest job is `pending` or `running`.

The user-facing meaning is:

- ChatGPT cannot ask "give me a short status update now."
- ChatGPT cannot ask "stop reading broadly and summarize what you have."
- ChatGPT cannot redirect an over-broad worker without stopping it.
- ChatGPT cannot continue natural-language management during a long turn.

The code currently says this is intentional because PatchBay does not add a message queue. That may be technically simple, but it is a bad fit for the desired manager-worker model.

In the intended PatchBay philosophy, ChatGPT should manage workers through natural-language follow-up. A worker that cannot receive any managerial direction while working feels dead or unmanageable.

### 4. Read-Only Workers Cannot Produce Discoverable Report Files

PatchBay's `worker_report_files` discovery is based on changed files named like `worker-report*.md` or `worker-report*.txt`.

But for `read_only` workers:

- PatchBay returns `worker_report_files: []`.
- The worker cannot write a durable report file into a read-only workspace.

Therefore, the recommended pattern "ask workers for durable report files" does not work for read-only investigation workers unless the worker uses an isolated writable workspace.

This is a major mismatch because many broad investigation tasks are naturally started as `read_only`.

### 5. Job Logs Are Written After Exit Or Cancellation

PatchBay collects stdout/stderr incrementally in memory, updates state from events, and writes the process artifacts only after the process exits or is cancelled.

That means while a worker is running:

- ChatGPT can inspect high-level state.
- PatchBay can update `last_event`.
- But ChatGPT does not get a robust live report artifact to inspect.

For long-running workers, this is not enough.

### 6. Large Output Capping Makes Evidence Hard To Recover

Several stdout logs reached the configured 200 KB cap.

When a JSONL event log is capped mid-line, later parsing of the copied log can fail on truncated JSON. More importantly, ChatGPT loses visibility into what happened near the end of the stream.

This does not necessarily break the running process, because PatchBay originally observed events incrementally. But it makes postmortem and user-visible report recovery worse.

### 7. Cancellation Path Does Not Parse Partial Result

In the current execution flow, if a job is cancelled after Codex exits, PatchBay writes stdout/stderr artifacts and returns without parsing a result.

That means:

- partial `agent_message` events are not turned into a result;
- `result.json` is not written;
- `report` later says the latest turn was stopped;
- useful partial evidence is hidden inside logs rather than surfaced as the worker's latest partial report.

This is probably one of the biggest concrete reasons ChatGPT thinks "no usable report returned."

## Confirmed Issues

These are supported directly by logs and code reading.

### Confirmed: Workers Were Stopped By ChatGPT

The relevant `RM ...` workers ended with `codex_worker_stop` calls. PatchBay logged cancellation and process exit after cancellation.

This means the lack of final reports is partly self-inflicted by cancellation.

### Confirmed: Workers Had Sessions

The four main stopped workers all had Codex session IDs in durable state.

This was not the older failure mode where a process starts but no session appears.

### Confirmed: Some Workers Emitted Partial Agent Messages

At least `RM Data Lifecycle Trace` and `RM Final Gap Synthesis` emitted partial `agent_message` events before being cancelled.

PatchBay did not promote these partial messages into durable visible worker reports.

### Confirmed: Read-Only Report Files Are Impossible Under Current Discovery

Current report-file discovery returns `[]` for read-only workers, and only reports changed worker-created files for writable workspaces.

Therefore, `worker_report_files: []` is not always a worker failure. Sometimes it is a predictable consequence of read-only mode.

### Confirmed: ChatGPT Fell Back To Heavy Manual Reading

The audit log shows 136 `codex_read_file` calls and 32 `codex_search_repo` calls in the latest window.

This shows the worker-first posture broke down operationally.

### Confirmed: Some Logs Hit The 200 KB Cap

Several stdout logs are exactly or approximately 200 KB and end with the truncation marker.

That makes debugging and late-stage report recovery weaker.

## Not Yet Confirmed

These remain theories or require deeper validation.

### Not Confirmed: The Workers Were Truly Stuck

The evidence does not prove that the latest workers were dead.

They may have been:

- still reading large files;
- running broad searches;
- preparing final reasoning;
- stuck in a long command;
- slow due to model behavior;
- or genuinely hung.

PatchBay's current public surface does not make that distinction clear enough.

### Not Confirmed: `RetailMind UI Quick Mapper` Failed

The copied durable state says `RetailMind UI Quick Mapper` completed successfully.

ChatGPT may have referred to an earlier transient state, a different worker, a stale list item, or its own interpretation. This needs exact repro before treating that specific worker as a tracking-mismatch bug.

### Not Confirmed: PatchBay Lost A Live Process In The Latest Window

The specific "Job was marked running, but no live Codex process is tracked" error exists in code and has happened in prior sessions. But in the latest copied RetailMind evidence, the main stopped `RM ...` workers had tracked PIDs and sessions before cancellation.

The latest incident is more clearly a liveness/reporting/management problem than a missing-process tracking problem.

## Deeper Diagnosis

PatchBay currently treats a worker turn mostly like a batch job:

- start Codex;
- stream events internally;
- wait for process completion;
- parse final result;
- expose report.

But the user wants PatchBay to support long-lived natural-language employees:

- start workers;
- let them work for a long time;
- see what they are doing;
- ask for status;
- redirect when needed;
- receive partial deliverables;
- keep confidence that work is alive;
- continue worker conversations without killing them.

Those are different product models.

For short jobs, batch semantics are fine. For long manager-worker sessions, batch semantics create false failure perception.

## Why This Matters For Long-Term Work

PatchBay's highest-value use case is not two-minute worker runs. It is long-running multi-worker investigation, implementation, review, and synthesis.

In that world, "no final report yet" must not look like "worker failed."

A long worker may need 10, 20, 40, or more minutes. During that time ChatGPT needs manager-grade liveness:

- latest command or action;
- whether output is flowing;
- latest partial conclusion;
- last meaningful agent message;
- elapsed time in current command;
- whether the worker is waiting on model reasoning or shell execution;
- whether the heartbeat is stale;
- whether there is enough evidence to wait;
- whether the manager can ask for a status summary without cancelling.

Without that, ChatGPT will keep doing the wrong thing:

- over-inspecting;
- assuming stuck state;
- cancelling workers;
- doing manual file reads;
- blaming PatchBay;
- failing the manager-first workflow.

## What PatchBay Should Probably Change

This section is not the final implementation plan. It is a discussion target.

### 1. Expose Live Worker Activity, Not Just State

`codex_worker_inspect` should show a manager-useful live view while a worker is running.

Possible fields:

- `live_status`: `starting`, `model_reasoning`, `command_running`, `command_completed_recently`, `waiting_for_next_event`, `stalled_suspected`, `completed`, `cancelled`, `failed`.
- `current_event_type`.
- `current_item_type`.
- `current_command_preview`.
- `current_command_started_at`.
- `seconds_since_last_event`.
- `seconds_since_last_stdout`.
- `latest_agent_message_preview`.
- `latest_agent_message_at`.
- `event_counts`.
- `stdout_bytes_seen`.
- `stderr_bytes_seen`.
- `result_extraction_status`.
- `suggested_manager_action`: wait, inspect later, ask status if supported, stop only if user confirms, etc.

This should not be a deterministic prompt classifier. It is runtime telemetry from actual events.

### 2. Promote Partial Agent Messages

When Codex emits an `agent_message` before final completion, PatchBay should store it separately from the final result.

Possible fields:

- `latest_partial_report`.
- `partial_reports`.
- `partial_report_count`.
- `latest_partial_report_at`.

For a running worker, `report` should not be only "currently working" if a partial report exists.

For a cancelled worker, `report` should say:

```text
The latest turn was stopped. Last partial report before cancellation:
...
```

That one change would make stopped workers much more useful.

### 3. Write Live Event Artifacts Incrementally

PatchBay should write a live JSONL event log or rolling status file while the process runs, not only after process exit.

This would support:

- inspection while running;
- postmortem even after crash;
- better recovery after process/server interruption;
- less reliance on in-memory event state.

The existing bounded/redacted artifact policy can still apply, but live worker status should not disappear until process exit.

### 4. Distinguish Healthy Busy From Stalled

PatchBay should not pretend to know semantic progress without evidence, but it can distinguish runtime conditions:

- events are arriving;
- command started recently;
- command has produced output recently;
- no event for N seconds but process still alive;
- process exited but result not parsed;
- process is missing;
- session never appeared;
- stdout cap was reached;
- stderr exists.

The important part is not to force a timeout on long work. The important part is to show the manager what is happening.

### 5. Support Status Requests Or Queued Manager Messages

This needs careful design, because Codex CLI may not support interrupting an active `codex exec` turn.

Options:

- Add a manager-side message queue that is delivered automatically after the current turn completes.
- Add a `codex_worker_request_status` tool that does not message Codex but returns PatchBay live telemetry.
- Add a "stop after current command and summarize" mode only if Codex/CLI can support it safely.
- Add a "cancel and immediately resume with status request" workflow as an explicit management operation.

Current behavior, where the only real options are wait or stop, is too primitive for long worker management.

### 6. Make Durable Reports Compatible With Investigation Workers

For investigation workers, read-only mode prevents durable report files.

Possible approaches:

- Default broad investigation workers to `isolated_write` even if they are instructed not to modify source files.
- Add a PatchBay-managed report artifact channel separate from git changes.
- Allow read-only workers to write to a dedicated PatchBay report directory outside the repo checkout.
- Make `worker_report_files` clarify: "No report files because this is read_only; use final/partial report instead."

The best design may be a first-class PatchBay worker artifact store, not changed-file detection only.

### 7. Make Cancellation Preserve Evidence

When a worker is cancelled, PatchBay should parse all captured stdout and extract:

- session id;
- last partial agent message;
- command history summary;
- latest completed command;
- current command at cancellation;
- stdout/stderr byte counts;
- whether output was truncated;
- a structured "cancelled with partial evidence" report.

Cancellation should not turn a useful partial worker into an empty stopped worker.

### 8. Improve ChatGPT-Facing Language

PatchBay should explicitly tell ChatGPT:

- `working` does not mean failed.
- Long-running workers may take time.
- Do not stop a worker just because no final report exists yet.
- Inspect `latest_turn` and live telemetry.
- If progress is visible, wait or use a status-specific tool.
- Stop only when the user wants cancellation, the worker is clearly wrong, or telemetry indicates a real stuck condition.

But this instruction alone is not enough. The tool output itself must make the correct behavior obvious.

## Suggested Discussion Prompt For ChatGPT Pro

Use this note to ask ChatGPT Pro something like:

```text
PatchBay may not be failing as much as it looks. The logs show workers had processes, sessions, JSON events, and partial messages, then ChatGPT stopped them because it saw no final report and weak live status. Please analyze whether your prior "worker unreliable" diagnosis may have confused "not enough visible liveness/reporting" with "worker execution failed." What live telemetry, partial-report surfacing, and manager actions would let you confidently keep workers running instead of cancelling them?
```

The key question is:

```text
What would you need to see from PatchBay while a worker is still running to know that it is healthy, useful, and worth waiting for?
```

## Provisional Conclusion

There is a real chance that much of the latest "worker failure" was not true worker failure.

The more likely core failure is:

```text
PatchBay does not currently make long-running worker liveness, partial progress, and intermediate reports visible enough for ChatGPT to manage workers confidently.
```

This still needs fixing in PatchBay, but the fix is different from "make Codex start correctly." It is about manager-grade observability and conversation control:

- live telemetry;
- partial report extraction;
- better cancelled-worker evidence preservation;
- report artifacts that work for investigation workers;
- clearer healthy-busy vs stalled states;
- fewer situations where ChatGPT's only available response is to stop a worker.

This is a critical worker-bridge issue because long-term PatchBay work depends on trust. ChatGPT must be able to let workers work.
