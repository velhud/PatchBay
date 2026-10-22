# Tool surface and worker details

This document records operational details that apply to the current unified implementation. The authoritative schemas remain in source.

## Runtime composition

The effective single-machine worker runtime is `runtime.WorkerRuntime` composed with the critical integration and view fixes. The older audit branch's `ReliableWorkerRuntime`, `GitCandidateMixin`, and `ReadSafetyMixin` are not activated because they would override later complete candidate and pagination behavior.

Public wrapper modules may delegate to adjacent `*_legacy.py` implementation modules. Those files are compatibility layers, not dead code.

## Candidate and integration behavior

- Git candidates are constructed from complete machine-safe path data.
- Delivered snapshots establish the integration baseline.
- Acceptance becomes stale when the candidate changes.
- Integration preview is explicit and token-bound.
- Application preserves all non-delivered content and does not commit.
- Cleanup requires complete delivery or explicit discard proof.

S06 uses one candidate for preview and apply, typed raw entry pre/postimages, and one atomic delivered-baseline/applied-receipt update. Failed observations preserve uncertainty. `candidate_id` reads retained review source; `review_decision` records controller source acceptance. Legacy delivery disposition is not independent review evidence. Hub safe cleanup forwards discard false to the existing Edge proof. See the [complete candidate contract](../repairs/2026-10-22-s06-candidate-delivery.md).
- Read-only workers cannot mutate through Git options or command paths.

Optional local start/message retry keys are persisted in the existing atomic
job options, serialized through a process-released admission lock. Replaying
the exact owner/method/key/arguments reads the original admission, including
after restart; it does not schedule inference. No key, a new key or deliberate
`auto_suffix` remains a separate request. Unsupported active-turn follow-ups
are refused rather than enqueued. Keyed integration uses the existing signed
preview/receipt mechanism and requires both token and key.

## File and diff views

Worker file views are lossless over contiguous source lines. When a line or response reaches the byte budget, pagination stops rather than skipping ahead and inventing line numbers. Responses include an exact continuation cursor and report when a requested `max_bytes` value was capped by server policy.

Bounded output is a transport-stability contract. Managers may request the next page or a focused file/diff view when additional evidence is required.

Compact Manager text keeps independent outcome, candidate/source, partial-page,
cleanup and recovery fields when returned. It does not replace exact file/log
bodies. Oversized scalar identities/cursors are explicitly marked omitted and
remain intact in structured output. See the [MCP interface contract](mcp-interface-contract.md).

## Process and concurrency behavior

- Repository mutation uses cross-process serialization.
- Process ownership includes the complete tracked process tree.
- Signal delivery requires proven identity; uncertain or recycled identities fail closed.
- Terminal semantic evidence is persisted before bounded wrapper cleanup.
- Cancellation, completion, and unknown execution cannot silently upgrade one another after a terminal decision.
- Hub/Edge attempt, lease, receipt, projection, and idempotency state remains durable and monotonic.

A delayed Hub resume rechecks current controller ownership before its final
group write. Exact fenced late receipts may repair the existing narrow
absence-based manual-recovery blocker; ordinary stale results cannot overwrite
terminal decisions. See the [S02 identity, crash and recovery map](../repairs/2026-10-22-s02-controller-recovery.md).

## Manager polling and reports

Status/list/wait/compact-inspect are bounded coordination views. Poll at the returned cadence, normally 20–30 seconds. `poll_too_early`, cached status, or a wait timeout is not a worker failure.

Projection suppression now expires at the next discrete native-activity or
checkpoint silence threshold. Threshold changes and clock rollback invalidate
the observation token; elapsed age alone does not publish every second. Status
deltas separate `activity_changed` from `status_changed`, with bounded
`control_changed_fields` for phase, liveness, cleanup and other action-relevant
changes. Observer activity never resets native progress age.

Cached local coordination reads avoid redundant dispatcher reconciliation.
Ordinary native supervision shares a process-parent census and offloads that
scan from the event loop. Startup, cancellation and cleanup paths retain their
forced fresh census and exact process identity checks. Cached telemetry cannot
clear an uncertainty or cleanup fence.

Hub wait observation uses process-local per-group reuse (0.25-second sampling,
60-second idle expiry, at most 256 scopes), with generation recheck after
snapshot construction. Caller filters, output and cancellation remain separate.
Same-group retained history still affects revision-query cost; no durable
counter or schema migration is added. The [S03 measurements and remaining
qualification](../repairs/2026-10-22-s03-observation.md) give the limits.

Completed reports should include summary, detailed report, evidence, changed files, commands/tests, notes, risks, open questions, and next steps when Codex emitted them. Diagnostics and raw lifecycle details are explicit debugging views, not the default manager payload.

## Model and reasoning selection

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

## Author Mode details

Author Mode has its own state home, leases, tasks, revisions, operations, approvals, publications, tokens, and qualification records. It shares no worker initialization path.

Selected reconciliation safeguards:

- bounded CLI waits (1–86,400 seconds; flag-only default 3,600);
- symlink rejection for local lease caches;
- required approval actor and token role;
- HTTP access logging disabled by default;
- private instance ID removed from public qualification output;
- remote package-path validation, maintenance locking, lease checks, unknown-execution fencing, supervisor-launch error handling, JSON-RPC validation, task/publication operations, safe exports, and honest CLI failure codes retained.

The Linux/WSL2 guard remains part of the execution qualification contract.

## Compatibility and exclusions

Export-only branches and temporary delivery workflow material are not production behavior and are not imported merely to merge every branch name. Separate dependency maintenance and external Desktop bridge work remain separate until independently qualified.

## Native evidence, ownership and report sources

Typed top-level native failure receipts survive bounded display logs and restart.
Expected/observed session IDs stay separate, and identity conflicts refuse
continuation and acceptance. Transcript recovery persists exact-source cursors
with bounded reconstruction; home/workspace drift cannot silently select another
conversation. External concurrent use of a managed native session is unqualified.

Clipped reports explicitly return completeness, original sizes and ranges. Use
`view="report_source"` on the existing worker inspect tool, with the source hash
and returned byte offset, until `has_more=false`. Pages are UTF-8-safe and scoped
to retained artifacts of the resolved worker; hashes and filtering are explicit.

Supervisor startup has a finite readiness handshake before target-gate release.
Discovery retains start identity through signalling; available Linux pidfds bind
the final signal, while numeric fallback has a residual race. Unknown cleanup
stays fenced. See the [S04 contract](../repairs/2026-10-22-s04-native-execution.md)
for exact limits, owners and separately authorized native/client qualification.

## Manager inbox pages and repeated use

`codex_worker_inbox` action `list` accepts `offset` (default 0) and `limit`
(default 100, maximum 200). `count` is the full current total; `next_offset`
indicates another page. Concurrent imports/cleanup can shift offset pages.
Imported archives default to 128 MiB compressed, 512 MiB expanded, 64 MiB per
file and 10,000 entries, including directories. Blank settings select finite
defaults, and nonpositive settings refuse. Actual expanded bytes are checked
while unpacking. An unchanged context copy can be reused; changed copies are
preserved and refused rather than silently overwritten.

Candidate inventory omits patch rendering; integration still requires the
complete exact binary patch within the existing 2,000,000-byte envelope.
Oversized/failed producers cannot become an applicable empty patch. Retained
review/retry metadata is deliberately not expired by age. See
[S09 decisions and unqualified history scope](../repairs/2026-10-22-s09-repeated-use.md).

## Admitted brief identity after reload

Job admission always stores the exact prepared UTF-8 brief's SHA-256 and byte
length, independently of optional private body logging. Rehydrated `prompt`
stays empty. New review targets use this durable metadata and expose additive
`contract_identity` with `state=admitted_metadata`, `sha256`, and `utf8_bytes`.
The existing descriptor's `contract_sha256` participates in candidate identity;
provenance annotations do not rewrite that descriptor or old target IDs.

For an older record without metadata, a nonempty same-turn digest can be reused
only after checking its descriptor/candidate ID, source tree and named private
Git ref/commit. Such a new target can report `retained_target_digest`, with byte
length unknown. Otherwise a fresh target reports `legacy_unknown` and null
SHA/length. An ambiguous old empty hash does not prove an empty admitted brief;
a newly admitted genuinely empty brief has the known empty SHA and byte count
zero. Malformed/partial metadata or conflicting/corrupt legacy identity refuses
new target construction without rewriting old evidence. Existing retained
selectors stay readable and stored target records remain unchanged.

A known brief digest is provenance, not semantic acceptance or mutation
authority. Changed source and a new turn still change candidate identity.
See [S10](../repairs/2026-10-22-s10-final-composition.md).

## Import and wait observations after repeated use

The complete inbox import runs off the MCP control loop with bounded admission
and an overall deadline. Cancellation keeps its writer slot and private staging
until the writer exits after cleanup; a lost response can still correspond to a
complete published artifact. Inspect before another import. An old completed
worker no longer makes every unchanged mixed-team wait return immediately.
Observations are local to caller and exact selection; first completion, new
work/turns, meaningful transitions and urgent cleanup/liveness still return
early. Empty/all-terminal selections return immediately; observer cancellation
never stops workers. Local Pro Request dispatch is non-idempotent: never retry
automatically; inspect request/worker state before any deliberate repeat.
See [follow-up contracts](../repairs/2026-10-23-postimplementation.md).
