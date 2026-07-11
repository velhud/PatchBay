# Root-Cause Evidence Brief

## Observed Failure

In a real Hub-managed parallel run, four workers completed normally while three
were reported as quiet or stale and were later stopped by the manager. Private
runtime evidence showed that all three affected Codex sessions had already:

1. emitted their final assistant answer;
2. written a terminal `event_msg` whose payload type was `task_complete`;
3. stopped producing meaningful work output.

The associated CLI wrapper processes nevertheless remained alive. PatchBay
continued to expose those jobs as running because its semantic lifecycle is
currently coupled to process lifetime.

## Code Path

`src/patchbay/jobs/executor.py` currently:

1. starts Codex and records the process;
2. reads stdout and stderr incrementally;
3. observes stdout protocol events such as `thread.started`, item events, and
   `turn.completed`;
4. waits in `_communicate_with_progress()` until `process.wait()` finishes;
5. only then parses the result and transitions the job to completed.

`reconcile_stale_running_jobs()` cannot repair this case while the wrapper PID
or executor task remains live. A live transport is treated as a live semantic
turn.

`JobManager.update_job_state()` also permits terminal states to be overwritten
without a compare-and-set terminal guard, leaving completion/cancellation races
underspecified.

## Root Cause

PatchBay conflates two distinct facts:

- **semantic completion:** Codex has finished the requested turn and recorded a
  terminal protocol event;
- **transport cleanup:** the CLI wrapper process has exited and all pipes have
  closed.

Normally these occur together, so the defect remained hidden. When the wrapper
lingers, PatchBay has no second authoritative completion observer and therefore
misreports completed work as running.

## Contributing Conditions

- The authoritative session stream is not monitored after session creation.
- Result capture depends primarily on stdout and process exit.
- Cancellation can win after Codex completion but before PatchBay observes it.
- Restart reconciliation checks process tracking, not terminal session evidence.
- Status heuristics can label the worker stale even when the unobserved terminal
  event already exists.

## What This Is Not

- It is not evidence that Codex reasoning was stuck.
- It is not primarily a broad `rg` or repository-size issue.
- It is not fixed by shorter prompts, lower concurrency, or hard worker
  timeouts.
- It is not a Hub routing failure; Hub displayed the Edge state it received.

