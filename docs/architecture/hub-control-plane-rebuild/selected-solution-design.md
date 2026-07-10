# Selected Solution Design

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Status: `READY_FOR_CONFLICT_REVIEW`

## Selected Approach

Rebuild PatchBay Hub as a transport-transparent, authoritative manager control
plane around the existing Edge `ToolHandler` and worker runtime.

The Hub owns fleet placement, durable task groups, cross-session coordination,
operation delivery, worker projections, and result routing. The Edge continues
to own local Codex sessions, processes, worktrees, repository paths, credentials,
locks, artifacts, and integration mechanics.

## Intended Behavior

Before:

```text
manager calls worker-looking Hub tool
-> Hub queues command and returns a receipt
-> manager polls command IDs
-> Hub guesses group/lane state from command history
```

After:

```text
manager calls worker tool in a work group
-> Hub routes to the group's pinned Edge
-> Edge executes the canonical worker action
-> Hub returns the actual semantic result after a bounded wait
-> Hub records authoritative compact state and references
-> opaque operation recovery appears only when delivery is delayed/uncertain
```

What must not change:

- natural-language worker briefs and follow-up messages;
- durable named worker continuity;
- model and reasoning selection;
- peer-worker and artifact context;
- Edge-local path policy, worktrees, repository locks, and integration;
- explicit no-commit integration behavior;
- Pro Request storage versus dispatch separation;
- direct manager inspection as an allowed exception;
- normal single-machine PatchBay behavior.

## Architecture Components

### 1. Canonical capability registry

Create one internal registry for public semantic actions. Each action owns:

- canonical name and Hub name;
- input and output schemas;
- side-effect annotations;
- Edge handler action;
- version/hash;
- target requirements;
- synchronous wait policy;
- payload retention policy;
- idempotency class;
- result normalization rules.

Worker, workspace, and Pro Request Hub descriptors must be derived from their
canonical contracts plus a common routing envelope. They must not be manually
copied into `hub/protocol.py`.

A parity test must fail whenever a canonical manager capability is added or
changed without an explicit Hub mapping or documented omission.

### 2. Stable public manager surface

Expose one stable Hub manager catalog. Do not rely on session-local tool-mode
switching for ordinary operation. Connected Edge versions do not dynamically
remove public tools. Instead, group preflight reports an incompatible Edge and
requires an Edge upgrade or different machine.

The selected catalog contains 31 tools. The exact contract is in
`hub-manager-tool-contract.md`.

### 3. Logical workspace identity

Introduce a stable `workspace_ref` independent of machine-local absolute paths.

A workspace record should contain:

```text
workspace_ref
display_name
repository identity when available (remote URL/hash)
aliases
machine_id
resolved local path (private/authorized projection)
authorization epoch
last preflight revision
```

Group creation selects an eligible machine that advertises the logical
workspace, then Edge preflight proves the local resolution.

### 4. Durable work group

A work group is one durable non-trivial user task. It contains:

```text
work_group_id
stable owner_ref
participant session refs
title and goal
visibility
workspace_ref
machine generation / pinned machine
routing decision and evidence
readiness projection
activity projection
lanes
fleet worker refs
operation refs
integration/disposition summary
created/updated/closed revisions
successor/predecessor links
```

Persistent lifecycle is deliberately small:

```text
open | closed | superseded
```

Derived state is separate:

```text
readiness: pending | ready | failed | machine_unavailable | incompatible_edge
activity: queued | active | idle | uncertain
outcome: complete | partial | abandoned | failed
```

Do not encode every transient worker condition as a group status.

### 5. Lanes

Lanes are responsibility labels inside a group. Starting a worker may create a
lane implicitly. No separate lane tool family is required for V2.

A lane projection contains:

```text
lane_id
human title/role
worker refs
operation refs
derived activity
latest compact reports
pending integration/disposition
```

Lane state is derived from workers and operations, never set merely because a
Hub command finished.

### 6. Fleet worker identity

Every grouped worker receives an immutable `fleet_worker_ref` that binds:

```text
Hub worker reference
machine_id and edge generation
Edge worker_id
work_group_id
lane_id
logical workspace_ref
human name
```

ChatGPT normally addresses workers by human name inside the current group.
Ambiguous operations return candidate references. Follow-up, inspection,
integration, and stop always route through the immutable worker reference, even
after the group has a successor on another machine.

### 7. Authoritative worker projection

Edge sends versioned compact worker events or heartbeat projections containing:

- worker/turn lifecycle and liveness;
- `can_message` and session availability;
- latest report/checkpoint references;
- changed-file summary and integration state;
- cleanup/worktree state;
- work group/lane metadata;
- projection revision and timestamp.

Hub stores compact projections and report/artifact references. Edge remains the
source of truth for local details.

### 8. Operation broker

Replace command-only semantics with three separate state layers.

Dispatch state:

```text
queued -> leased -> result_received
               \-> lease_expired
               \-> cancelled
```

Operation outcome:

```text
pending -> succeeded | refused | needs_confirmation | failed | unknown_outcome
```

Worker state remains Edge-owned:

```text
starting | working | quiet | idle | completed | failed | stopped | lost
```

Each operation must include:

```text
operation_id
idempotency_key/hash
action and target refs
owner/group/lane refs
attempt number
lease token and expiry
payload reference/hash
Edge execution receipt
domain result summary/reference
terminal revision
timestamps and error classification
```

### 9. Semantic result behavior

Hub tools wait a bounded interval for fast Edge operations. When the Edge result
arrives, the tool returns that domain result directly.

When it does not arrive in time, return:

```json
{
  "status": "pending",
  "operation_id": "op_...",
  "summary": "The request is accepted by Hub and is waiting for Edge result.",
  "next_poll_seconds": 20,
  "next_action": "patchbay_operation_status"
}
```

Never return `accepted: true` as if the worker operation succeeded merely
because Hub queued it.

### 10. Idempotency and reconciliation

- Mutating actions require or receive an end-to-end idempotency key.
- Edge keeps a bounded durable execution receipt keyed by operation ID and
  idempotency key.
- A duplicate delivery returns the prior receipt/result rather than repeating
  the action.
- Lease expiry does not automatically replay a mutation.
- Hub asks Edge to reconcile operation ID before choosing retry, failure, or
  `unknown_outcome`.
- Terminal results are compare-and-set and cannot be overwritten by stale
  attempts.
- Integration uses a preview token bound to worker revision, base revision,
  patch hash, and target workspace.

### 11. Nonblocking Edge delivery

The Edge control loop must not execute a long `worker_wait` inline while
heartbeats and other manager operations are blocked.

Required behavior:

- heartbeat and command intake remain independent;
- quick control actions can execute concurrently within configured bounds;
- Hub `worker_wait` waits on Hub projection revisions/events rather than routing
  a sleeping command to Edge;
- stop and message remain responsive while another manager waits.

### 12. Transactional durable state

Move Hub coordination state from unlocked whole-file JSON updates to a
transactional store, preferably SQLite for the local/single-Hub deployment.

Minimum tables or equivalent records:

- schema migrations and Hub identity;
- machines and Edge generations;
- workspace projections;
- owners and session participants;
- work groups and lanes;
- fleet workers and worker projection revisions;
- operations, attempts, leases, and receipts;
- integration previews/dispositions;
- enrollment codes and machine credentials metadata;
- append-only events;
- transient payload metadata and expiry.

Use deterministic constraints for exact state boundaries: unique idempotency
keys, foreign keys, terminal-state guards, revisions, and transactions.

### 13. Payload retention

Current docs and storage behavior disagree. V2 must make the contract explicit.

Selected policy:

- durable Hub state stores compact metadata, hashes, summaries, and references;
- briefs, messages, temporary file URLs, full reports, file contents, and diffs
  use private transient payload storage with bounded TTL and acknowledgement;
- Edge remains the durable source for full worker reports/workspaces;
- explicit private runtime evidence may retain full payloads only when enabled
  by operator configuration and documented honestly;
- public/audit logs never include raw payloads or credentials.

This is an operational data-boundary decision, not a restriction on an
operator's private local document policy.

### 14. Ownership and multi-conversation behavior

Separate:

```text
owner_ref: stable connector/operator ownership
conversation_ref: one ChatGPT conversation
transport_ref: one MCP transport session
work_run_ref: convenience window inside a conversation
```

Private groups belong to `owner_ref`. Conversations become participants when
they create or resume a group. `current group` is conversation-specific.

Another conversation under the same owner may discover owned groups but must
explicitly resume one before mutating it. Shared groups remain explicit. Result
visibility and operation recovery use the same authorization checks.

### 15. Group placement and reassignment

Group creation:

1. resolve candidate logical workspaces;
2. filter online compatible Edges with capacity and disk feasibility;
3. apply explicit allow-list/tags if provided;
4. rank by worker load, memory pressure, and CPU pressure;
5. pin one machine generation;
6. run strict Edge preflight;
7. return machine choice and reasons.

No semantic task classification is used.

Reassignment creates a successor group. It does not mutate the original group's
machine identity. Old operations are cancelled if unclaimed where safe; claimed
or uncertain operations remain attached to the old group for reconciliation.

### 16. Strict preflight

Preflight must prove:

- selected Edge protocol/tool contract is compatible;
- logical workspace resolves to an allowed local path;
- repository identity matches the advertised workspace;
- Git repository, branch, HEAD, upstream/ahead-behind, and dirty summary when
  applicable;
- worktree/log disk feasibility;
- current worker capacity and queue policy;
- active/unintegrated workers or worktrees that may conflict;
- repo lock/busy state when cheaply available.

Preflight returns facts and blockers. It does not make semantic decisions about
task complexity or suitability.

## Compatibility And Rollout

- Keep single-machine PatchBay unchanged.
- Version Hub schema and Edge capability contracts.
- Migrate or import V1 machines/groups as legacy records; do not silently reset.
- Require compatible Edge versions for V2 grouped work.
- Keep the existing deployed endpoint unchanged until a replacement passes all
  acceptance gates and a controlled deployment is authorized.
- Preserve a fallback path during first production rollout.

## Acceptance Criteria Seed

- AC-1: all 31 public tools have strict validated input/output schemas and
  truthful annotations.
- AC-2: every old worker/Pro Request/manager-inspection capability is mapped or
  explicitly omitted with a tested reason.
- AC-3: normal worker tools return semantic Edge results, not command receipts.
- AC-4: repeated worker messages preserve session and worktree continuity.
- AC-5: group/lanes reflect real worker activity and cannot close active or
  uncertain work as complete.
- AC-6: retry/result-loss scenarios never duplicate worker start, message,
  stop, or integration.
- AC-7: cross-session owner continuity and participant takeover work.
- AC-8: successor reassignment preserves control of old-machine workers.
- AC-9: one real local and one real two-Edge lifecycle pass end to end.
- AC-10: real ChatGPT selects and uses the tools naturally without manual
  command-ID administration.
