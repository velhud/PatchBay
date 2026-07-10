# Hub V2 Implementation WorkPackets

Status: `COMPLETE`

Normative inputs:

- `../resolved-contract-addendum.md`
- `../hub-manager-tool-contract.md`
- `../conflict-review/conflict-review-to-workpacket-handoff.md`

## WP-00: Contract Registry

Build the ordered internal 31-tool registry, exact schemas, truthful
annotations, action mappings, semantic envelope vocabulary, contract hashes,
and manifest regression tests. Do not expose V2 publicly yet.

Primary ownership: new Hub tool-surface/contract modules and contract tests.

Gate: exact 31 names, five V1-only names absent, strict argument validation,
output schema present, deterministic manifest/schema hashes.

## WP-01: Identity And Logical Workspaces

Add persisted operator principal, conversation/transport/work-run participants,
Edge generations, logical workspace identities, machine-local projections,
immutable fleet worker refs, and strict path/repository preflight contracts.

Gate: identity/workspace matrix including restart, token rotation, missing
conversation metadata, re-enrollment, alias precedence, and out-of-root paths.

## WP-02: Transactional State And Migration

Replace unlocked JSON updates with versioned multi-process SQLite/WAL state,
transactional CAS, events, migrations, source JSON backup/checksum, typed legacy
import, dry-run, corruption handling, and rollback rules.

Gate: import fixtures and concurrent Hub/CLI writers pass without lost updates.

## WP-03: Edge V2 Protocol And Scheduler

Add protocol/manifest negotiation, generation claim fences, durable Edge intent
journal/result outbox, Hub acknowledgements, projection revisions, and
independent heartbeat/poll/execution/upload tasks with target serialization.

Gate: long waits/workers do not stop heartbeat, message, stop, or result upload;
old/incompatible Edges cannot claim V2 work.

## WP-04: Operation Broker

Implement operation/attempt state machines, stable idempotency and semantic
hashing, leases/fencing, payload acknowledgement/TTL, event waits,
reconciliation, cancellation, semantic result normalization, and
`patchbay_operation_status`.

Gate: crash/retry/late-result/duplicate-result model tests pass.

## WP-05: Worker Projection

Add cache-independent full worker snapshots, deltas, revisions, tombstones,
immutable fleet refs, separated state axes, group/lane derivation, report and
integration references, and Hub-owned nonblocking waits.

Gate: group state follows actual workers across restarts, gaps, duplicates,
quiet periods, failures, stops, and lost Edges.

## WP-06: Read-Only Manager Surfaces

Implement fleet/workspace discovery, worker options/list/status/wait/inspect,
workspace open/tree/search/read/changes, pagination, visibility, and semantic
results through Hub.

Gate: full field parity and real routed inspection; no sleeping Edge wait.

## WP-07: Worker Mutations And Batch

Implement inbox, single start, batch start, and message with full mature fields,
group/lane routing, machine-qualified refs, action-specific durable correlation,
parent/child results, per-item idempotency, and active-turn blocked behavior.

Gate: partial batch and crash-mid-batch retries never duplicate workers.

## WP-08: Integration, Stop, And Cleanup

Implement signed preview tokens, exact binding/revalidation, integration crash
reconciliation, duplicate-apply protection, stop confirmation, explicit
unintegrated-discard consent, and target lock ordering.

Gate: every integration/cleanup crash point reconciles without duplicate apply
or silent work loss.

## WP-09: Authoritative Group Lifecycle

Implement create/list/status/resume over real projections, close dispositions,
frozen closure fields with attached reconciliation, successor-group reassign,
predecessor worker control, and collision warnings.

Gate: active/uncertain/unreviewed/unintegrated work cannot be called complete.

## WP-10: Pro Requests

Route list/read/claim/respond/dispatch/close with machine-qualified references,
principal/group associations, revision CAS, explicit dispatch boundary, and
action-specific operation correlation.

Gate: concurrent claims and dispatch retries are deterministic and visible.

## WP-11: Atomic Cutover And Acceptance

Expose all 31 tools together only after every handler is truthful. Run full
tests, migration rehearsal, real local MCP/EdgeRunner/WorkerRuntime/Codex
lifecycle, failure injection, two-Edge routing, restart recovery, and fresh
ChatGPT tool-selection acceptance. Then update docs and deployment procedures.

Gate: no private data, no partial V2 labels, tested rollback, independent final
review, and complete release evidence.

## Completion Record

WP-00 through WP-11 are implemented in `src/patchbay/hub/*_v2.py`,
`src/patchbay/hub/adapters/`, the worker runtime integration, and the complete
V2 test suite. The consequential acceptance evaluator is
`scripts/live_hub_v2_eval.py`; it exercises the production pull bridge through
the HTTP/MCP boundary with two Edge runners and real local `ToolHandler` /
`WorkerRuntime` instances.
