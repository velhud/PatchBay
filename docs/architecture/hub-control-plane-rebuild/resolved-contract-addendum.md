# Resolved Contract Addendum

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Status: `CONFLICTS_RESOLVED_FOR_WORKPACKET_BUILDING`

This addendum closes the blocking ambiguities found by the six-lane conflict
review. It is normative where it differs from earlier wording in this pack.

## 1. Trust And Identity Boundary

PatchBay Hub remains a single-operator coordination system, not a multi-tenant
authorization service.

- HTTP authentication protects access to the Hub.
- One persisted `principal_ref` represents the operator trust domain and is not
  derived from an ephemeral MCP connection or raw token hash.
- Token rotation retains the same principal record.
- `conversation_ref`, `transport_ref`, and `work_run_ref` are coordination
  identities beneath that principal.
- Multiple people sharing one connector credential are the same security
  principal. PatchBay does not claim otherwise.
- Private groups are default visibility filters inside that trust domain, not a
  security boundary. Explicit resume/takeover is a coordination and audit act.
- Result-bearing operations still enforce principal, participant, group, and
  target consistency so one conversation cannot mutate another conversation's
  work accidentally.

## 2. Canonical Public Result Vocabulary

Every public tool returns:

```json
{
  "status": "ok|pending|partial|blocked|failed|not_found",
  "result": {},
  "operation": {},
  "warnings": [],
  "next_actions": []
}
```

Rules:

- `ok`: requested domain action completed successfully.
- `pending`: accepted but the domain result is not yet known.
- `partial`: a compound action has item-level successes and non-successes.
- `blocked`: domain action did not run or complete because a recoverable
  precondition, confirmation, ownership, capacity, or conflict gate stopped it.
- `failed`: execution attempted and failed, or an unrecoverable contract error
  occurred.
- `not_found`: target does not exist in the caller's visible coordination scope.
- Internal `refused` is normalized to public `blocked`.
- `needs_confirmation`, `repo_busy`, `capacity_blocked`, and active-turn message
  refusal are `blocked` with structured reason fields.
- `unknown_outcome` is an internal nonterminal reconciliation state and is
  exposed as public `pending`, never as success or failure.
- The exceptional `patchbay_operation_status` uses the same envelope.
- Singular top-level `operation_id` and `next_action` forms are forbidden.

## 3. Operation And Attempt State Machines

Operation states:

```text
created -> payload_ready -> dispatchable -> running
running -> reconciling -> succeeded|blocked|failed|cancelled
running -> succeeded|blocked|failed|cancelled
running -> outcome_unknown -> reconciling
outcome_unknown -> succeeded|blocked|failed|cancelled
```

`succeeded`, `blocked`, `failed`, and `cancelled` are terminal. Late equivalent
receipts may confirm a terminal result but cannot change it. A conflicting late
receipt is retained as an audit conflict and never overwrites the terminal
result.

Attempt states:

```text
offered -> claimed -> executing -> effect_recorded -> result_ready -> acknowledged
claimed|executing -> lease_expired -> reconciling
reconciling -> result_ready|retryable|manual_recovery
```

Every claim receives an immutable `attempt_id` and fencing token. Result and
renewal calls compare machine generation, operation, attempt, and fencing token.
Stale attempts cannot write terminal state.

## 4. Idempotency

- Every mutating public call requires an `idempotency_key` in the machine
  contract, including each item in batch start.
- ChatGPT-facing descriptors describe it as an opaque stable retry key. The
  initialize instructions tell ChatGPT to generate one before the first call
  and reuse it after interruption.
- The Hub scopes a key by `principal_ref + tool + logical target` and stores a
  canonical semantic payload hash.
- Same key and same hash returns the existing operation/result.
- Same key and different hash returns `blocked` with
  `idempotency_payload_conflict`.
- Read-only calls do not require keys.
- Internal child operations derive deterministic keys from the parent operation
  and child item identifier.

This contract does not pretend that an entirely new key after an unknowable
client-side response loss can be deduplicated. The client instruction and MCP
request wrapper must retain the chosen key across continuation.

## 5. Compound Operations

Batch start, group create plus preflight, successor reassignment, group close
dispositions, and Pro Request dispatch use parent/child operations.

- Parent status is `ok` only when every required child succeeds.
- Parent status is `partial` when item outcomes differ.
- Each child has a stable item ID, idempotency key, target, and result.
- Retrying the parent resumes unfinished children and returns prior terminal
  child results without duplicating them.
- Compensation is explicit; successful children are never silently undone.

## 6. Worker State Axes

Hub does not flatten Edge worker truth into one ambiguous enum.

- `worker_state`: durable worker identity state (`available`, `stopped`,
  `workspace_missing`).
- `turn_state`: current/latest turn (`none`, `queued`, `starting`, `working`,
  `completed`, `failed`, `cancelled`).
- `liveness`: observation (`starting`, `active`, `quiet`, `stale`, `lost`,
  `terminal`).
- `integration_state`: `not_applicable`, `no_changes`, `not_integrated`,
  `applied_to_checkout`, `discarded`, `uncertain`.
- `review_disposition`: `unreviewed`, `accepted`, `rejected`, `not_required`.

Group and lane summaries derive from these axes and operation state. They never
infer worker completion from command transport completion.

## 7. Messaging And Worker Names

- V2 preserves the existing next-turn continuation model.
- Messaging an active turn returns `blocked` with `active_turn_in_progress`.
- Active steering and queued follow-up are not promised by V2.
- Worker names remain unique per logical repository projection, matching the
  Edge runtime. Group-relative selection is preferred, but duplicate names in
  the same repository still require explicit `auto_suffix=true` or a different
  name.

## 8. Logical Workspaces, Edge Generations, And Fleet Worker References

Logical workspace identity and machine projection are separate records:

```text
workspace_ref -> repository identity
workspace_projection_ref -> workspace_ref + machine_id + edge_generation + local path
fleet_worker_ref -> machine_id + edge_generation + edge_worker_id
```

- Each successful enrollment creates a new immutable `edge_generation`.
- Restore retains generation; replacement/re-enrollment creates another.
- Claims and results are fenced by generation.
- Old workers remain routed through their immutable fleet reference even after
  a successor group is created elsewhere.
- Strict Edge preflight reuses `WorkspaceContext` path guards and returns repo
  identity, branch/HEAD, dirty summary, capacity, disk, and unintegrated-worker
  warnings.

## 9. Projection Protocol

- Edge sends periodic full worker snapshots plus optional deltas.
- Projection identity is `(machine_id, edge_generation, projection_revision)`.
- Revisions are monotonically increasing inside one generation.
- Duplicate or lower revisions are ignored.
- A revision gap requests a full snapshot; deltas are not applied across gaps.
- Full snapshots include tombstones or omission semantics for workers removed
  since the previous full snapshot.
- Hub freshness derives from receive time, not Edge wall-clock ordering.
- Projection polling does not mutate manager-facing status-delta caches.

## 10. Edge Journal, Outbox, And Action Reconciliation

Edge persists operation intent before domain execution and persists result in a
durable outbox before acknowledging completion to Hub. Hub acknowledges receipt
through the next poll/heartbeat exchange; Edge prunes only acknowledged results.

Generic receipts are not enough for mutations. Action-specific correlation is
required:

- worker start/message: operation and item IDs live in durable job metadata;
- inbox: operation ID lives in artifact metadata;
- Pro Requests: operation ID and expected revision live in request events;
- stop/cleanup: operation/disposition record persists before destructive work;
- integration: preview token and operation disposition persist around apply,
  with post-crash apply/reverse checks and file fingerprints.

## 11. Concurrency And Lock Order

Edge heartbeat, projection, command intake, execution, and result upload are
independent tasks. A long wait or worker turn cannot stop heartbeat/control
delivery.

Global lock order:

```text
operation target lock
-> worker/name lock when applicable
-> repository worktree-administration lock
-> repository checkout-mutation lock
-> worker runtime persistence transaction
```

- Same-worker message/stop/integrate/cleanup operations serialize.
- Same-name worker starts serialize.
- `git worktree add/remove/prune` uses the administration lock.
- Integration retains checkout mutation locking and adds crash reconciliation.
- Hub event waits hold no SQLite transaction or process lock.

## 12. Integration Preview Token

The Edge issues a signed opaque token bound to:

- principal and participant;
- fleet worker ref and worker revision;
- logical workspace projection;
- full patch hash;
- base HEAD and dirty-worktree fingerprint;
- accepted-dirty-base patterns;
- creation and expiry timestamps.

Integration requires the token. Edge recomputes all bindings under the relevant
locks. Stale or mismatched tokens return `blocked`. Reusing a token after a
successful apply returns the prior successful operation result; it never
applies twice.

## 13. Stop, Cleanup, Close, And Successors

- `cleanup_workspace=true` is not sufficient to discard unintegrated changes.
  The call must also carry explicit `discard_unintegrated_changes=true`.
- Group closure freezes title, goal, placement, outcome, summary, and closure
  disposition. It does not stop workers, clean workspaces, or dispose worktrees;
  attached operation/worker projections may continue to reconcile.
- Call `patchbay_worker_stop` explicitly before closing any worker that should
  stop, and complete any requested workspace disposal through that worker-level
  operation before close. Group close records the result; it does not perform
  those side effects.
- `leave_running` records an exceptional retained worker without stopping or
  cleaning it; it cannot support a `complete` outcome. The retained worker stays
  controllable by immutable fleet ref without reopening or mutating the closed
  group's frozen fields.
- Reassignment creates a successor group. It never changes the predecessor's
  pinned machine or worker routes.
- Close requires a disposition for every worker: `integrated`, `no_changes`,
  `reviewed_failure`, `stopped_preserved`, `discarded`, or `leave_running`.
  `discarded` requires that worker's explicit
  `discard_unintegrated_changes=true` consent.

## 14. Artifact And Pro Request Visibility

Artifacts and Pro Requests gain immutable principal, logical workspace,
machine-generation, group, lane, and operation associations. Private/shared
behavior follows the coordination model in section 1. Claims use revision CAS;
takeover does not silently transfer the durable principal.

## 15. SQLite And Migration

SQLite is multi-process safe for the Hub server and administrative CLI:

- WAL journal mode;
- foreign keys enabled;
- bounded busy timeout;
- `BEGIN IMMEDIATE` for conflicting mutations;
- one migration lock and schema version;
- no transaction held during network or model work;
- revision/CAS updates for claims, results, projections, and participant state.

Migration is offline and repeatable:

1. stop V1 Hub and Edge polling;
2. checksum and preserve the source JSON unchanged;
3. import enrollment codes, token hashes, machines, commands, groups, current
   pointers, and events into typed legacy records;
4. classify queued/running V1 commands as `legacy_recovery_required`, never
   replay them automatically;
5. preserve old in-place reassignment history without pretending worker routes
   are known;
6. validate counts, token authentication, and referential integrity;
7. start V2 only after compatible Edges are installed.

Rollback to V1 is supported only before the first V2 mutation unless a tested
reverse exporter exists. After V2 mutation, rollback means V2 database restore
or a separate single-machine endpoint.

## 16. Capability And Claim Fence

Every Edge advertises protocol version, edge generation, ordered tool manifest
hash, schema hash, and action capability versions. V2 Hub never offers a V2
operation to an incompatible Edge. Claim compares the required contract hash
and generation again, so an old Edge cannot claim work before preflight.

## 17. Hub-Specific Initialization Authority

For Hub mode, `patchbay_fleet_status` is the environment/capability entry point
and `patchbay_workspace_list` is the workspace entry point. They are the
approved Hub equivalents of single-machine `codex_self_test` and
`codex_open_workspace`. This preserves the exact 31-tool surface without skills,
tool-mode controls, or redundant self-test tools.

## 18. Public Cutover Rule

The public V2 catalog is atomic. It remains the V1 catalog until all 31 target
tools have implemented handlers, strict validated schemas, truthful
annotations, semantic results, and live evidence. Partial V2 labels are never
published as functioning tools.
