# Application Purpose And Invariant Map

## Relevant Product Purpose

PatchBay lets a manager communicate naturally with durable Codex workers. Its
job is to expose truthful worker state and preserve useful results without
forcing the manager to interpret process internals.

## Authority Order

1. Exact Codex terminal protocol evidence for the known session.
2. PatchBay's durable normalized terminal decision.
3. Process exit and exit code as transport evidence.
4. Quiet/stale heuristics as advisory status only.

Hub must consume the Edge worker projection. It must not independently guess
whether Codex completed.

## Hard Invariants

- Legitimate Codex turns may run indefinitely; no new task-duration timeout.
- Silence, a final-looking sentence, or an intermediate agent message is not a
  completion signal.
- Only evidence tied to the exact known session may complete a job.
- `task_complete` and supported stdout terminal events are semantic completion.
- A terminal job decision is idempotent and cannot be casually overwritten.
- A final report is captured before a lingering wrapper is terminated.
- Cancellation before observed completion remains cancellation.
- Completion observed and durably committed before cancellation makes stop a
  terminal no-op.
- Late terminal evidence after cancellation is retained diagnostically but does
  not silently reopen the job.
- Raw session transcripts, private paths, and prompts are not exposed publicly.
- Existing normal process-exit behavior remains supported.
- The repair must work in single-machine and Hub/Edge modes without changing
  the manager workflow.

## Philosophy Alignment

This repair uses deterministic code only at an exact mechanical boundary:
protocol event observation, lifecycle persistence, and process cleanup. It does
not classify task meaning, constrain model reasoning, or replace managerial and
worker intelligence with heuristics.

