# Solution Design Intake Checklist

## Scope

- Repair false-running and false-stale worker state after Codex has actually
  completed a turn.
- Preserve final worker reports before cleaning up a lingering CLI wrapper.
- Reconcile the same condition after a PatchBay restart.
- Keep Hub and single-machine worker behavior consistent.

## Evidence Available

- Current executor, manager, worker projection, and cancellation code.
- Existing executor artifact and cancellation tests.
- Private operator evidence from a real seven-worker Hub run.
- Exact Codex session JSONL for the affected sessions.
- Current Codex CLI behavior on deployed Edges.

## Confirmed Facts

- Three workers recorded a final assistant message and `task_complete` in their
  exact Codex session files.
- Their CLI wrapper processes remained alive for many minutes afterward.
- PatchBay's executor waits for `process.wait()` before result parsing and the
  completed transition.
- Stale reconciliation treats a live tracked process as proof that the job is
  still running.
- Existing stdout parsing understands `turn.completed`, but production code
  does not observe session JSONL `task_complete`.
- No arbitrary maximum duration is desired for legitimate Codex work.

## Exclusions

- No change to model selection, machine routing, groups, lanes, or public tool
  count.
- No attempt to classify task complexity or infer completion from silence.
- No deployment while production worker activity is in progress.
- No private logs, prompts, machine identities, or session identifiers in this
  public repository.

## Readiness

The issue was sufficiently evidenced for implementation. The local repair and
verification are complete, but release and deployment remain explicitly gated
on operator approval and a fresh runtime-activity check.
