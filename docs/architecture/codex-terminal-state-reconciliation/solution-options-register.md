# Solution Options Register

## Option A: Hard Quiet Or Total Timeout

Kill a worker after a fixed quiet or wall-clock period.

Rejected. Legitimate Codex work may be long and quiet. This would hide the
state bug by destroying valid work and violates PatchBay's operating model.

## Option B: Treat Final-Looking Agent Messages As Completion

Complete when the latest message resembles a report.

Rejected. Agent messages are checkpoints as well as final answers. Text shape
is not a protocol boundary and would cause false completion.

## Option C: Wait Only For Process Exit And Improve Manager Instructions

Keep runtime behavior and tell ChatGPT to wait longer.

Rejected as the full solution. Better patience reduces premature cancellation
but cannot turn a completed Codex session into a truthful PatchBay state.

## Option D: Session-Aware Terminal Observer

After the exact Codex session ID is known, incrementally observe that session's
JSONL for a strict terminal event. Capture the final assistant result, perform
bounded post-completion wrapper cleanup, and finalize the job atomically.

Selected as the primary mechanism.

## Option E: Post-Hoc Stale Reconciler Only

After a job looks stale or PatchBay restarts, inspect its exact session file for
terminal evidence.

Useful but insufficient alone. It repairs abandoned state too late and still
allows managers to cancel already-completed work. Selected as a defensive
secondary mechanism alongside Option D.

## Option F: Depend On A Codex CLI Upgrade Or Rollback

Assume one CLI version will always exit correctly.

Rejected as the architecture. CLI upgrades should be tested, but PatchBay must
model semantic completion independently of wrapper cleanup because they are
separate contracts.

