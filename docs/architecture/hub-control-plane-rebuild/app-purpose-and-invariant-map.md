# App Purpose And Invariant Map

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

## Relevant Purpose

PatchBay connects a conversational manager to capable local Codex workers.
Single-machine PatchBay already provides the core managerial experience: create
a named worker, talk to the same worker repeatedly, monitor it patiently, inspect
its report or exact evidence when needed, integrate accepted work, and stop or
clean it deliberately.

Hub adds fleet placement and durable task grouping. It must not replace this
experience with machine commands, queue administration, or reduced worker
controls.

## Source-Of-Truth Map

| Fact or state | Owner | Consumers |
| --- | --- | --- |
| Local Codex process, session, turn, and worktree state | Edge `WorkerRuntime` and job runtime | Hub worker projection, ChatGPT worker tools |
| Repository path permission and mutation lock | Edge workspace/path guard and lock manager | Preflight, workers, integration, manager inspection |
| Machine enrollment and current heartbeat | Hub fleet registry | Router, fleet status, group readiness |
| Logical workspace identity and machine-local resolution | Hub workspace registry plus Edge preflight proof | Group placement, reassignment, routed inspection |
| One durable user task | Hub work group | ChatGPT conversations, lanes, workers, operations |
| Worker-to-group/lane membership | Hub fleet worker record, confirmed by Edge identity | Group status, routing, ownership, close checks |
| Operation delivery state | Hub operation broker | Recovery, diagnostics, idempotent retries |
| Operation domain outcome | Edge domain result, normalized by Hub | ChatGPT tool result, group projection |
| Integration readiness and result | Edge integration preview/apply result plus Hub projection | Manager, group close checks |
| Durable owner | Stable connector owner identity | Group visibility and ownership |
| Current ChatGPT conversation | Session participant identity | Current-group convenience and takeover coordination |

## Hard Invariants

### Managerial semantics

- A worker tool must describe and return the worker-domain action it names.
- Queue and transport details must not become the ordinary user workflow.
- ChatGPT can continue the same named worker through natural-language messages.
- Direct read/search/change inspection remains available but exceptional.
- Multi-worker delegation and batch appointment are first-class.

### Group placement

- One non-trivial task becomes one durable work group.
- One normal work group has one immutable machine generation.
- Sibling workers in a normal group stay on that machine.
- Lanes are responsibility labels inside the group, not separate task objects.
- Availability routing chooses only at group creation or explicit successor
  creation; it never interprets task meaning or silently scatters workers.

### State and lifecycle

- Dispatch state, operation outcome, worker state, group readiness, and group
  activity are distinct axes.
- A completed start dispatch does not mean a completed worker.
- A transport-successful domain refusal remains a refusal.
- Group status is derived from actual workers, operations, integrations, and
  machine freshness.
- Group close cannot convert active or uncertain work to idle.
- Closed groups are immutable historical records.

### Routing identity

- Every worker gets a stable fleet reference that permanently identifies its
  owning machine, Edge worker ID, group, and lane.
- Message, inspect, integrate, and stop route through that reference, not the
  group's current machine or a caller-supplied guess.
- Human names remain the primary ChatGPT-facing selector within a group.

### Retry and recovery

- Every mutating operation has an idempotency key and Edge deduplication record.
- A lease cannot be silently replayed after its outcome becomes uncertain.
- Lost result delivery produces `unknown_outcome` until reconciled.
- Stale attempts cannot overwrite terminal outcomes.
- Integration is never duplicated by transport retry.

### Workspace and reassignment

- Groups store a logical workspace reference plus a machine-local resolved path.
- Edge preflight proves path authorization and repository identity.
- Reassignment creates a successor group/machine generation.
- Old workers remain inspectable, messageable, integratable, and cleanable on
  their original machine.
- Live Codex sessions are never described as migrated.

### Ownership and continuity

- Stable owner identity survives MCP transport sessions and ChatGPT conversations.
- Conversation/session identity remains separate for current selection and
  concurrent coordination.
- Private group details are visible to their stable owner; mutation by another
  conversation requires explicit resume/takeover rules.
- All result retrieval applies the same visibility checks as group retrieval.

### Public MCP contract

- Input schemas are strict and validated.
- Output schemas describe the actual semantic result.
- Read-only, destructive, idempotent, and open-world annotations are truthful.
- Tool names, descriptions, and results use the same lifecycle vocabulary.
- The Hub tool catalog is stable for a connector session and does not depend on
  dynamic mode switching.

### Evidence and completion

- No release claim rests only on synthetic command results.
- A real Edge and real Codex worker must prove the full lifecycle.
- Failure injection covers crash/retry/duplicate/result-loss cases.
- A real ChatGPT connector must prove natural tool selection and continuation.

## Current Pattern Classification

| Existing pattern | Classification | Decision |
| --- | --- | --- |
| Edge `ToolHandler` and `WorkerRuntime` | `CANONICAL` | Reuse |
| Edge-local worktrees, path guards, locks, inbox, Pro Requests | `CANONICAL` | Reuse |
| Hub enrollment, heartbeat, availability telemetry | `CANONICAL_WITH_REPAIR` | Preserve and version |
| Work group as durable task and keep-together placement | `CANONICAL_CONCEPT` | Rebuild state semantics |
| Handwritten reduced `HUB_TOOLS` | `FORBIDDEN_TARGET_PATTERN` | Replace with generated canonical registry |
| Queue receipt returned as worker result | `FORBIDDEN` | Replace with domain-result broker |
| JSON whole-file state under concurrent processes | `PROTOTYPE_ONLY` | Migrate to transactional store |
| In-place group machine reassignment | `FORBIDDEN` | Create successor group |
| Synthetic Hub live eval as release proof | `INSUFFICIENT_EVIDENCE` | Keep as smoke test only |

## Non-Goals

- No semantic task classifier or complexity router.
- No automatic cross-machine group distribution.
- No automatic merge queue or cross-machine write reconciliation.
- No direct ChatGPT bash/write/edit surface in default Hub manager mode.
- No reimplementation of Codex sessions, worktrees, or worker reasoning.
- No campaign/channel API until its state and ownership model is independently
  designed and verified.

## Product Decisions Already Resolved

- Groups remain explicit and durable rather than hidden entirely.
- Group creation performs availability-only routing and returns the selection
  explanation.
- Skills, config, self-test, and dynamic tool-mode controls are not part of the
  default Hub manager tool surface.
- Pro Request response storage and dispatch remain separate operations.
