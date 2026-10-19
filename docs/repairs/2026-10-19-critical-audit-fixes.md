# Critical audit fixes — 2026-10-19

This change set repairs the highest-impact correctness findings from the public
PatchBay audit at base revision `678aa3ff93dda5cbfd2c517234504170d4cd3db7`.

## Worker delivery and review

- Integration is now derived from a complete Git candidate tree using a private
  temporary index. Committed, staged, unstaged, untracked, deleted, binary,
  executable, symlink, Unicode/spaced-path, and no-final-newline changes are
  represented by Git instead of hand-built text patches.
- Successful integration records a durable private worker snapshot. A follow-up
  turn is diffed from the last delivered snapshot, so old work is not replayed
  and historical acceptance cannot certify a new candidate.
- Cleanup recomputes the exact post-integration delta and fails closed when the
  state cannot be proven. Undelivered work therefore requires explicit discard.
- Explicit `read_only` workers cannot inherit danger-full-access bypass flags.
- Worker and base-checkout source pagination preserves actual line coordinates
  and never silently clips or skips an oversized line.

## Pro Request and Hub state

- Display reads remain bounded, but dispatch reads the complete response that
  was already accepted by the configured storage limit.
- `target=new_worker` dispatch can explicitly pass `model` and
  `reasoning_effort`, allowing a Luna implementation lane to be pinned.
- Pro Request read/check/write mutations are serialized across processes and a
  durable shared projection revision invalidates quiet out-of-process changes.
- Truncated Edge pages no longer emit deletion tombstones for requests merely
  displaced from the page.
- Historical operation results cannot overwrite a newer Hub request revision.

## Process lifecycle

- The supervisor still blocks TERM/INT across the fork/tracker race, but the
  target unblocks those signals immediately before `exec`, restoring graceful
  cancellation semantics.

## Compatibility structure

Large original modules are retained as `*_legacy.py` and imported by thin
compatibility subclasses. This keeps public imports and existing behavior while
isolating the corrected contracts for review and rollback. The companion
regression test uses disposable Git repositories and no model inference.
