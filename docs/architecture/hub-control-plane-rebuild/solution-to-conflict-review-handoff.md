# Solution To Conflict Review Handoff

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Recommended status: `READY_FOR_CONFLICT_REVIEW`

## Selected Solution Summary

Rebuild Hub as a transactional operation and worker-projection control plane
that routes the canonical worker/workspace/Pro Request contracts through durable
work groups and immutable machine/worker references.

## Likely Changed Areas

| Area | Change | Risk |
| --- | --- | --- |
| Hub protocol/tool registry | Generated canonical descriptors and semantic outputs | High |
| Hub runtime | Operations, projections, ownership, groups, routing | High |
| Hub store | SQLite schema, migrations, events, retention | High |
| Edge runner | Version negotiation, receipts, projections, nonblocking control | High |
| Hub server/context | Stable owner/session participation and result waits | High |
| Worker descriptors/runtime adapters | Canonical mapping and stable fleet refs | Medium |
| Workspace context | Logical identities and strict preflight | Medium |
| Pro Requests | Machine-qualified routing and claim revisions | Medium |
| Tests/evals/docs/config | Broad updates after implementation | High |

## Shared Surfaces And Conflicts

### Public tool registry

Conflicts with any simultaneous change to worker schemas, aliases, tool modes,
annotations, output schemas, or ChatGPT instructions. Freeze the canonical
registry interface before parallel implementation.

### RequestContext and ownership

Conflicts with multi-chat ownership, work-run scoping, Pro Request ownership,
artifact takeover, and worker takeover changes. Stable owner and participant
semantics must be decided once.

### Worker state and status

Conflicts with heartbeat/liveness, report capture, steering/queued messages,
worker list scopes, stop confirmation, and restart recovery. Hub must project
the existing semantics rather than invent a second lifecycle.

### Integration

Conflicts with dirty-base policy, accepted untracked files, preview hash/base
revision checks, repository locks, and worktree cleanup. Preview-token behavior
must wrap the existing integration contract.

### Logging and payload retention

Conflicts with runtime evidence changes. Public audit logs, compact durable Hub
state, transient payloads, and optional private full evidence need explicit
separation.

### Deployment

Conflicts with current enrolled Edges and public Hub endpoint. V1 compatibility,
state migration, fallback path, and mixed-version behavior need one rollout plan.

## Dependencies And Sequencing

Must happen first:

1. conflict review and final contract freeze;
2. canonical capability registry;
3. transactional state schema and migration;
4. operation/receipt protocol;
5. stable identity and logical workspace model;
6. worker projection;
7. tool families and group lifecycle;
8. tests, docs, and deployment.

Safe parallel work after interfaces freeze:

- descriptor generation tests;
- state migration fixtures;
- Edge receipt protocol;
- workspace identity/preflight;
- ChatGPT golden-prompt evaluator;
- docs current-vs-target cleanup.

Must not run independently:

- group close before worker projection;
- worker tools before operation result semantics;
- reassign before logical workspaces and immutable worker refs;
- deployment before migration and mixed-version gates;
- instructions before exact tools exist.

## High-Risk Flags

- [x] persistent state lifecycle
- [x] public MCP contract
- [x] concurrency/retry/idempotency
- [x] ownership/visibility boundary
- [x] deployment/runtime configuration
- [x] performance/resource behavior
- [x] documentation truth

## Required Reviewer Lanes

- state-machine and operation semantics;
- MCP schema/tool-selection contract;
- worker parity and group lifecycle;
- concurrency/idempotency/recovery;
- ownership/visibility;
- migration/deployment;
- live-evidence design.

## Open Assumptions For Review

| Assumption | Why it matters | Required treatment |
| --- | --- | --- |
| SQLite is sufficient for one Hub process | Sets storage implementation | Confirm process topology and locking needs |
| Edge can persist bounded operation receipts | Required for deduplication | Confirm runtime path and retention |
| Hub may transiently carry full prompts/results | Required to route work | Confirm TTL/evidence policy and document honestly |
| Work group remains explicit | User workflow and continuity | Preserve unless product decision changes |
| Pro Requests stay in default manager surface | Reverse manager-worker communication | Confirm tool-count/selection impact in real ChatGPT eval |

## Handoff Decision

Proceed to cross-solution conflict review. Do not begin source implementation
until shared interfaces and sequencing conflicts are resolved and converted into
bounded WorkPackets.
