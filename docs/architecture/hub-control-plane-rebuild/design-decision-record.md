# Design Decision Record

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Date: 2026-07-09

Decision status: `IMPLEMENTED_VERIFIED_DEPLOYED`

## Context

PatchBay's single-machine worker surface supports a natural manager-to-worker
lifecycle. Hub V1 added machine enrollment, availability routing, work groups,
and command delivery, but exposed a reduced public surface and treated transport
records as if they represented worker operations and group truth.

Repeated prompting and local field additions cannot fix that boundary.

## Decision

Rebuild the Hub public control layer and durable state model as a transparent
adapter around the canonical Edge worker/workspace/Pro Request contracts.

Keep:

- existing single-machine runtime;
- Edge `ToolHandler`, workers, worktrees, locks, path guards, inbox, Pro Requests;
- enrollment, heartbeats, machine telemetry, availability-only placement;
- explicit durable work groups and lanes;
- one-group/one-machine default.

Replace:

- handwritten reduced Hub tool schemas;
- queue-receipt worker semantics;
- command-only group projection;
- unreconciled leases and ineffective idempotency metadata;
- session-first ownership;
- in-place machine reassignment;
- unlocked whole-file JSON state;
- synthetic routing smoke as release proof.

## Rationale

The selected design repairs the root causes at their owners:

- canonical registry prevents schema drift;
- operation broker owns delivery/retry truth;
- Edge worker runtime owns local worker truth;
- Hub worker projections own fleet-level status;
- stable owner and participant sessions own continuity;
- logical workspace identities own cross-machine resolution;
- successor groups preserve immutable machine ownership;
- transactional state owns lifecycle and concurrency;
- real live scenarios own release evidence.

## Consequences

Positive:

- complete natural manager workflow through one Hub connector;
- real worker parity without duplicating worker implementation;
- safer retries and explicit uncertainty;
- accurate group status and close behavior;
- cross-conversation continuity;
- stable foundation for later multi-conversation coordination.

Accepted costs:

- larger staged implementation;
- versioned state migration;
- Edge capability/protocol upgrade;
- comprehensive new integration and failure tests;
- temporary V1/V2 compatibility documentation.

Risks not accepted:

- adding tool labels without semantics;
- returning queue receipts as domain success;
- silent cross-machine failover/scatter;
- duplicate mutation on retry;
- closing active workers as complete;
- dynamic tool catalogs that disappear from ChatGPT;
- claiming readiness from synthetic results.

## Required Next Step

- [x] cross-solution-conflict-review completed
- [x] blocking contract ambiguities resolved in `resolved-contract-addendum.md`
- [x] WorkPacket planning after conflict review
- [x] implementation
- [x] standard verification
- [x] local production-shaped live evaluation
- [x] authenticated public-tunnel acceptance with real Edge/Codex workers
- [x] release/deployment

## Residual Verification Boundary

- external Codex authentication and subscription quota remain provider/account
  dependencies rather than Hub guarantees;
- every deployment must rerun the public connector scenario because local and
  synthetic tests cannot prove the actual ChatGPT-visible catalog;
- deployment-specific evidence remains private and is not stored in this public
  repository.

## Artifact Links

- `solution-design-intake-checklist.md`
- `root-cause-evidence-brief.md`
- `app-purpose-and-invariant-map.md`
- `affected-surface-and-ripple-map.md`
- `solution-options-register.md`
- `option-comparison-matrix.md`
- `selected-solution-design.md`
- `hub-manager-tool-contract.md`
- `implementation-verification-plan.md`
- `solution-to-conflict-review-handoff.md`
