# Design Decision Record

Decision ID: `DDR-CODEX-TERMINAL-001`

Status: `ACCEPTED_FOR_IMPLEMENTATION_PENDING_USER_START`

## Decision

PatchBay will define Codex turn completion from authoritative terminal protocol
evidence associated with the exact session, not solely from CLI wrapper exit.
It will monitor the exact session JSONL after session creation, preserve the
final result, perform bounded post-completion wrapper cleanup, and commit one
atomic terminal job decision. A defensive reconciler will recover the same
condition after restart.

## Why

Real runtime evidence proves that Codex may finish and write `task_complete`
while its wrapper remains alive. The current process-coupled lifecycle reports
completed work as stale and induces unnecessary cancellation.

## Consequences

- Worker status becomes truthful sooner.
- Wrapper cleanup is explicit and observable.
- JobManager terminal transitions require stronger concurrency semantics.
- Session format compatibility needs focused fixtures and graceful fallback.
- No public Hub workflow or tool-count change is needed.

## Rejected Shortcuts

- hard timeouts;
- completion inferred from quiet time;
- completion inferred from natural-language message shape;
- manager instructions as the only fix;
- relying on one Codex CLI version to exit correctly.

