# Author Mode operations

## Declare the exact profile

Before connecting an ordinary Chat client, record one immutable qualification
profile containing: server commit and build digest; OS/kernel and service
unit; Python version and dependency lock digest; configured state root and
filesystem; selected client surface and account/workspace; exact connector
source/client identity; ingress route and TLS/auth mode; role; accepted MCP
protocol version; catalog hash and instruction hash; operation deadline and
payload limits; Docker image digest and network policy when checks use Docker;
and the observed durability class. Never substitute a local API, Inspector,
Work, or browser test for the ordinary Chat route. A refresh after changing
catalog, instructions, auth, route, or server version creates a new profile;
do not silently carry the old qualification forward.

Use `python scripts/author_interface_eval.py --json` to record the repository
catalog/instruction baseline. It measures source-owned serialization; the
client's accepted catalog and effective instructions still need a live
readback. Save both hashes in the profile and retain the connector's refresh
time and observed readback. If the host caches metadata, disconnect/reconnect
or use its supported refresh flow, then call a harmless read-only capability
and confirm the expected version/hash. A successful server restart alone does
not prove the client refreshed its metadata.

## Authentication and ingress

Author HTTP admission defaults to eight active and sixteen pending requests.
Writes can occupy six active slots, ordinary reads and writes together seven,
and authorized urgent controls at most two, always within the total of eight.
Per-principal active limits are two for writes, three for reads and four for
urgent controls, counting all of that principal's active requests. Pending
limits are fourteen writes, fifteen ordinary requests or sixteen including
urgent controls; a principal may queue four ordinary or five urgent requests.
These are admission limits, not guaranteed simultaneous allocations.

Ordinary requests share a 60/minute, burst-20 token bucket per principal.
Urgent requests use a separate 30/minute, burst-five bucket and a global
60/minute, burst-ten bucket. Each principal ledger retains at most 4,096 opaque
keys. Admission attempts consume rate capacity even if queueing later fails.
The default gate reserves urgent capacity against ordinary reads as well as
writes; custom two-slot gates retain only their existing read reservation.

The actual authenticated MCP tool catalog, role, write qualification and
validated arguments determine admission class. Task recovery must pass scoped
task access. Cancellation must identify an existing check operation and pass
current lease ownership before receiving priority. HTTP recovery/cancel and
exact operator control routes follow the equivalent checks. Arbitrary caller
priority flags, inbox mutations and tool-name-looking strings grant no priority.
Final route checks run again; admission never grants mutation authority.

Classification has at most eight in-flight owners, a 64 KiB JSON prefix and a
0.1-second prefix-read budget. Malformed, oversized or incomplete classification
falls back to ordinary write admission and normal route validation. Buffered
ASGI messages are replayed without dropping bytes; the existing 2 MiB MCP/body
limit remains. Cancelling an observer does not free a classifier thread slot
until that classification finishes. Queue admission waits at most 0.25 seconds,
then returns `429 INGRESS_BUSY`, `Retry-After`, a bounded jitter hint and
`X-Request-ID`. Follow backoff rather than opening parallel retry storms.
Admission never retries a domain mutation or proves cancellation/cleanup
completion. Pure recovery/status reads do not start or reconcile work.

Author retains short pure reads rather than introducing a thread-occupying
long wait. Respect the returned state/cadence and recover existing operation
IDs after a lost response. Async Author waits and deployed route latency remain
separate qualification work; see the [S03 contract](../repairs/2026-10-22-s03-observation.md).

The request ID is correlation metadata, not the mutation's retry key. Accepted
operations retain it through materialization/recovery. Native downloads use a
45-second transfer budget and 10-second socket waits; this leaves local margin
but does not qualify DNS, proxies, serialization, or a deployed ingress deadline.

Draft apply and seal validation failures are durable terminal operation
results. The first call returns its specific validation error and records the
operation as failed. Replaying the same exact request with the same idempotency
key returns `OPERATION_FAILED` with the original error in `previous`; it does
not retry the mutation. After correcting the request, use a new idempotency
key. For a previously accepted but still queued draft operation from an older
runtime, replay the exact original request and key through the normal Author
service so current lease, task, and path policy checks can resolve it. Never
edit operation state directly or substitute a new request under the old key.

Keep the deployment on loopback unless a specific remote route is required.
For a remote route, terminate TLS at a named trusted proxy, disable request
body/access logging that could retain bearer tokens or private file names, and
require a role-specific credential. Store the token in the deployment secret
store, not in command history or the repository. Rotate by issuing the new
credential, updating the client, proving a read-only call, then revoking the
old credential. Do not broaden a read-only role to writer to troubleshoot a
read failure.

Diagnose ingress in this order: check service health locally; check MCP
initialize and negotiated protocol; check authentication role; check
`tools/list` hash; check a bounded read; then inspect service-side request and
operation IDs. Compare the caller-visible timeout to the configured ingress
deadline. A timeout is an unknown response, not proof that the operation was
rejected or accepted. Look up the durable operation by its returned ID or the
joined task/recent-operation recovery view before retrying. Retry only with
the same idempotency key and exact semantic payload when the contract allows
replay. Never repeat a non-idempotent side effect under a new key because the
response was lost.

### Existing local CLI controls

Use the same explicit `--home "$PATCHBAY_AUTHOR_HOME"` for every invocation
and point only at the intended Author state. Read-only discovery is available
without launching workers or queued checks:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" control-status
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" recovery TASK_ID
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" operations TASK_ID --limit 50
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" doctor
```

An inbox scan is explicit and bounded. It accepts only a chosen directory,
waits for a stable regular file, and defaults to 25 files; no daemon is started
by Author bootstrap. A watch is opt-in and `--cycles 0` runs until explicitly
stopped:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox-scan /approved/drop --stability-seconds 1 --max-files 25
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox-import /approved/drop/change.pbpkg --key REQUEST_KEY
```

Import records a package and, without a current explicit approval or stored
current-lease grant, leaves it `awaiting_approval`. `inbox-approve
OPERATION_ID --key REQUEST_KEY` requires the task's current writer lease and
routes through the ordinary materializer. `inbox-grant TASK_ID --key
REQUEST_KEY` stores a lease-bound capability; `--auto-validate` opts into
mechanical validation for later matching packages, while `inbox-revoke
TASK_ID --key REQUEST_KEY` removes that capability. Neither import nor grant
authorizes checks, publication, or external effects by itself.

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox-approve OPERATION_ID --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox-grant TASK_ID --key REQUEST_KEY --auto-validate
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" inbox-revoke TASK_ID --key REQUEST_KEY
```

The local operator workflow obtains a task lease explicitly before approval
or grant. The CLI writes its lease cache under the private state home and also
prints token/epoch data; protect terminal output as a secret:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" claim TASK_ID --label controlled-operator --key REQUEST_KEY
```

To capture an operator-selected dirty source subset without changing the
source checkout, pass each approved path explicitly. Capture verifies stable
per-file reads and unchanged HEAD, not a globally atomic filesystem snapshot.
Keep selected files, selected absences and parent paths quiescent for coherent
coupled edits. The result reports this consistency limit. Policy changes require
an expected-current digest, actor, and idempotency key. Pause/resume/revoke
controls require an actor and key; `restore-task` restores that task's grant but does not resume a paused service or execution:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" task REPOSITORY_ID --goal "bounded task" --base HEAD --dirty-path src/module.py --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" policy TASK_ID --policy /secure/policy.json --expected-digest CURRENT_POLICY_SHA256 --actor OPERATOR_ID --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" pause --actor OPERATOR_ID --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" resume --actor OPERATOR_ID --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" revoke-task TASK_ID --actor OPERATOR_ID --key REQUEST_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" restore-task TASK_ID --actor OPERATOR_ID --key REQUEST_KEY
```

`token --role ROLE` only reads one of the three static compatibility role
tokens created at `init`; it does not rotate them. `token-issue` creates a
principal-scoped credential; each `--task` restricts it to that task. Use a
fresh idempotency key for a deliberate replacement, deliver the new bearer
secret through the approved secret channel, prove a read-only call, then
revoke the old credential by its returned `credential_id`. Credentials do not
expire automatically; revocation is explicit. The bearer is returned by the
issue command and must not be copied into logs, tickets or command history.

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" token-issue reader-operator --role reader --task TASK_ID --key ISSUE_KEY
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" token-revoke CREDENTIAL_ID --key REVOKE_KEY
```

The legacy static-token inspection command also returns a bearer secret. Use
it only from a private terminal with output capture disabled:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" token --role reader
```

Qualification is separate: `qualify-client EVIDENCE.json --actor
OPERATOR_ID --key REQUEST_KEY` records observed evidence and binds it to the
current catalog/output profile; it does not mint or rotate credentials. Token
issue/revoke commands are trusted local-operator actions, not remote
reader/author capabilities. Do not improvise a database edit as token
rotation.

## Controller interruption and authority boundary

An accepted local operation may continue only through mechanics explicitly
authorized by the task policy. When a new source change, interpretation,
repair choice, approval, or acceptance judgment is required, stop in an
explicit `awaiting_controller`, `awaiting_input`, or `awaiting_approval`
condition (`awaiting_review` for passing nonpublishing candidates). The local service must not invent a decision or invoke another
model. A fresh Chat resumes by reading the joined recovery record, verifying
task/revision/lease ownership, then inspecting the relevant candidate,
operation, evidence and next required decision. An event notification or
process completion does not wake or complete a Chat conversation.

MCP `author_recover_task` returns 20 recent operations by default; set
`operation_limit` to an integer from 1 to 200 when another window is useful.
Active and uncertain effects remain separately visible, with counts and partial
flags. Use `author_recent_operations` versioned cursor pages to retrieve older
history. The local CLI/domain recovery default stays 50. Read-only recovery
does not reconcile, retry or release an execution fence.

MCP and HTTP operation history additionally accept `traversal="snapshot"`
(HTTP query `traversal=snapshot`). Carry its cursor and next offset through
every page. It pins the initial operation identity set and excludes later
inserts, even backdated ones; each page reads current operation states in its
own SQLite transaction. Default `strict` traversal still invalidates on
same-task activity, while unrelated-task activity does not invalidate it.
Snapshot traversal makes progress under activity but is not a retained
point-in-time authority snapshot. Recover current task and lease again before
acting. Restore or unsupported row deletion can invalidate traversal guarantees;
restart discovery after maintenance. The CLI's existing strict interface remains.

For current source, use `candidate.candidate_context_id` and check the revision
and `commit_oid` in `candidate.candidate_context`. Null means a writer must
export the current candidate first. `source_context_id` and `source_context`
describe authoring provenance; the deprecated `candidate.context_id` retains
that provenance meaning and can read an older revision. No read repoints or
creates a context. Passing nonpublishing tasks await review; record that review
externally without implying persisted acceptance or publication approval.

For an unknown execution, preserve the run directory and journal. Establish
supervisor/process identity from durable ownership records; never trust a PID
alone. If ownership or the effect cannot be proved, keep it fenced and request
explicit operator reconciliation. Do not blind-rerun, signal a recycled PID,
or clear an unresolved cleanup marker.

### Explicitly resolving a proven-quiescent unknown execution

The Linux-only local CLI includes an operator resolution command for the narrow
case where the original execution outcome must remain unknown but the
admission fence can be released after a complete quiescence assessment. It
does not start an executor, signal a process, remove a container, or call an
HTTP/MCP route. Its default invocation is assessment only; it may create lock
files but does not mutate the run, operation, or receipt:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" resolve-unknown-execution OPERATION_ID \
  --operator "Named operator" \
  --reason "Close admission after independent quiescence review"
```

Run with the same trusted Docker wrapper and `PATH` as the execution service;
a different endpoint must fail its recorded daemon identity check. A missing
immutable container ID remains fenced; a name-only absence query is insufficient.

Review the returned identity and Docker evidence. Apply is a separate explicit
choice and requires the exact statement printed by `--help` for the evidence
source. Record the bounded manual reason with `--reason`. The command rechecks the operation under maintenance, task, and run
locks. It requires a fresh successful Docker daemon identity and exact
container query, exact recorded process identities and groups to be absent or
zombie-only, and no other active or uncertain run. Native lifecycle evidence
must include acknowledged full container IDs and daemon identities. Older
runs without that journal require a separate, private
`legacy-docker-observation.json` bound to the immutable receipt and run
records, including explicit operator attestation of engine continuity and no
in-flight create request. Missing, inconsistent, or inconclusive evidence
leaves the run fenced.

An applied resolution writes a separate mode-0600 audit record and a completed
resolution operation. It preserves the original receipt bytes, classification,
result, and error; the historical outcome remains `EXECUTION_UNKNOWN`. The
original operation/run become `blocked` with phase `operator_resolved` and
cleanup disposition `operator_attested` only to release admission. This is
not a success, failure, cancellation, or claim that execution did not occur,
and it does not retry the task. The host census cannot prove that arbitrary
work escaped every recorded process group, so the named operator attestation
remains an explicit trust boundary.

## Backup, upgrade and rollback

Fresh Author homes include transactional operation clocks for bounded,
snapshot-consistent paging. A schema-1 home created before this addition
requires an explicit additive upgrade while no run/cleanup is active or
uncertain:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" upgrade-state
```

Opening state and recovery reads never install triggers. Before the upgrade,
strict operation paging returns `STATE_UPGRADE_REQUIRED`; ordinary recovery remains
readable. The upgrade is idempotent, preserves operation history, and keeps
schema version 1 compatible with rollback. Requalify the client/runtime binding
after upgrading service code.

1. Announce a maintenance window and stop admission of new mutating work.
2. Wait for active operations to become terminal. Resolve or fence unknown
   execution and finish owned process cleanup. The Author backup command fails
   closed while runs or cleanup are active/unresolved.
3. Create an access-controlled backup outside the runtime state directory
   using `patchbay-author --home "$PATCHBAY_AUTHOR_HOME" backup PATH` (or
   `Maintenance.backup` from a controlled maintenance tool). Preserve the
   exact archive and output receipt. Validate by restoring it into a new empty
   state directory; restore checks archive inventory, file hashes, schema and
   repository Git integrity before making that state available.
4. Record the current schema version, service package/build digest, state
   directory identity and backup hash. Test restore into a new empty directory
   before upgrading; verify database integrity and repository Git object
   integrity.
5. Upgrade the service using the deployment's pinned artifact. Run database
   migration only through the supported startup/maintenance path. Keep the
   pre-upgrade backup immutable and do not point two service versions at one
   live state directory.
6. Verify health, read-only capability, catalog/instruction hashes, a test
   task, state retrieval and repository refs. Then reopen admission.
7. On failure, stop admission and service, preserve the failed state for
   diagnosis, restore the validated backup to a new directory, and switch the
   service configuration atomically. Verify the restored identity/schema and
   operation history before reopening. Do not overlay a backup onto a live
   state directory or delete the failed state until its evidence is retained.

Restoring a backup rewinds state to the backup point. Any post-backup effects
must be independently reconciled before service resumes. A backup demonstrates
the tested recovery point, not protection against loss of both the state and
backup media.

Restored credentials and writer epochs also rewind. Keep the restored service
isolated, reconcile post-backup revocations/claims and external effects, and
establish current credentials/leases before reopening admission. Current state
does not supply a cross-restore global anti-replay epoch. Automated restore activation remains unsupported under the S08 manual cutover
contract. Deduplication-horizon policy remains separate S09 work; S02
does not claim replay safety after discarded evidence or an old backup.

## Retention and garbage-collection recovery

The current local defaults and their actual meaning are:

| Data | Current behavior / default | Operator decision |
|---|---|---|
| Operation rows, request idempotency records and canonical receipt blobs | No expiry or pruning job. Rows remain in SQLite until state is retired/restored; referenced blobs are protected by the reference graph. | Declare a state-retirement and backup archive policy. There is no supported age-based operation-row deletion. |
| Lease | Default 86,400 seconds; configured bounds are 60 seconds to 604,800 seconds. | Set per deployment and account for controller think time; reclaim only by explicit fenced claim/takeover. |
| Client qualification | 30-day expiry; exact instance/catalog/output-profile binding also invalidates it earlier. | Requalify after expiry or any bound metadata change; refresh client metadata and retain observed evidence. |
| Event journal | Default maximum 10,000 rows globally; oldest rows are pruned as new rows are added. | Treat event watermarks as bounded change cursors, never as permanent audit history; export required audit evidence separately. |
| Unreferenced blobs, log content and proven execution workspaces | GC default age guard 3,600 seconds; minimum accepted guard 60 seconds. Run workspaces and spool files are eligible only with proven cleanup and a retained, verified receipt. Bounded stdout/stderr bytes referenced by receipts remain content-addressed and referenced; there is no age-based receipt/log-blob expiry. | Set the invocation age guard to exceed the longest ingest/retry/inspection window. Never GC if reference enumeration is incomplete. |
| Staging and uploads | GC removes entries older than the same invocation age guard. | Preserve any entry involved in an unresolved operation until reconciliation. |
| Backups | Written outside Author state; the product has no backup-expiry job. | Publish and enforce a separate count/age/location policy, test restore, and retain at least one validated pre-upgrade copy. |

SQLite connections set `synchronous=FULL`; blob writes flush and fsync the
temporary file, atomically place content, and fsync the containing directory
before references are committed. This is the implemented local ordering, not a
measured power-loss guarantee: filesystem, mount, controller cache, and host
power-loss behavior remain unqualified. No RPO/RTO is claimed. Declare a
deployment durability class from its actual storage and backup arrangement.

Keep the database reference graph and backup policy authoritative. If GC
fails, stop repeated attempts, retain the exact error and partial counts,
inspect `doctor` and operation/recovery state, resolve or fence every active
effect, reconcile remaining references and temporary files, create and verify
a fresh backup, then retry with the age guard after confirming no active
references were missed. Never manually delete an unresolved run directory or a
referenced blob to free space.

Current implementation qualification does not yet prove every deployment's
retention period, power-loss durability or full Linux storage-recovery
behavior. Use [qualification](qualification.md) and retain the output of the
requirements evaluator with each release candidate.

## Mutation and retry inventory

The journal is authoritative for task creation, repository registration and
refresh, selected dirty capture, lease claims, context/feedback exports, package
submission, draft apply/seal, check submission/cancellation, policy revisions,
inbox grant/revoke/import/approval, publication approval/apply, integration,
credential issue/revoke, qualification, backup and GC. Supply an explicit
`--key` or interface key for caller retries. CLI compatibility paths that omit
a key generate and return a discoverable local operation; the same omitted
key is not an assertion that a later command has the same intent.

Pause/resume and task grant controls use an atomic metadata receipt journal,
scoped to the principal. Initialization and restore require a new destination;
repeating against an existing destination fails instead of overwriting it.
The additive state upgrade uses its installed marker as its replay receipt.
Scheduler dispatch, execution recovery and package recovery target existing
operation IDs with ownership/CAS gates. They do not create replacement jobs
for uncertain execution. Explicit folder scans/watch cycles use package
semantic keys and advance bounded batches before wrapping; unchanged earlier
files cannot permanently starve later files. Export/build helpers and native
temporary transfer do not promote candidates or assert execution success.

An authorized operator taking over a package operation resumes its original
journal and immutable candidate intent, rather than minting a new principal's
submission. A stored auto-validation grant retains only the granting
principal's exact task capability; scanning does not widen it or renew its
lease. Credential revocation, task grant revocation, and cancelling an accepted
run are distinct actions.

## Model-visible response budgets

Task lists expose at most 2,048 goal characters, with `goal_truncated` and
`goal_bytes`; `author_get_task` retrieves the complete exact goal. Recovery and
recent-operation pages keep result/error details above 8 KiB inside SQLite,
return identity summaries with explicit partial/byte fields, and provide an
exact `author_operation_status` action for the complete record. Paging does
not load every large historical result into Python. These are coverage
indicators, never permission to treat a summary as complete source/evidence.

The complete MCP result envelope is limited to 8 MiB, including the text copy
and structured output. Oversize responses fail with `RESOURCE_LIMIT` and the
boundary `effect_result_too_large` for writes or `read_result_too_large` for
reads. An invalid output has `INVALID_OUTPUT` and the corresponding
`effect_may_exist_result_invalid` or `read_result_invalid` boundary. No success
is asserted. Safe identities come from validated requests and authorized durable
readback, never arbitrary fields of the bad result. Follow `recovery_actions`
and the `original_key` where returned; a wire fault neither undoes an effect nor
authorizes a new key. Request smaller pages or use an existing operator artifact.
Compatibility fields `effect_returned` and `result_identity` remain, with the
identity now restricted to trusted readback. The former marks a returned
mutating handler, not proof that an effect succeeded.
Unexpected domain exceptions instead return `EFFECT_OUTCOME_UNAVAILABLE`
(`READ_RESULT_UNAVAILABLE` for reads), with `effect_returned=false` and no raw
exception text. They still require durable readback. A publication interrupted
after a possible push (including typed timeout/I/O) remains `publication_unknown`;
explicit same-key replay reads the retained exact remote/ref and never repeats a
possible push. A confirmed past effect remains recoverable after policy changes,
with historical labeling and no new CI. Proven pre-effect refusal is blocked;
a mismatched or unconfirmed destination remains fenced.
This does not approve publication or authorize any new destination.
New exports remain writes. The compatibility output profile stays the default.
See the [MCP interface contract](../reference/mcp-interface-contract.md) for
selectors, bounded input diagnostics, roles and supported protocol versions.

## S05 source, packaging and transfer contract

Selected version-2 contexts retain complete presence inventory without loading
omitted payloads. Use explicit `identity_only` for no payloads. Exact draft byte
paging, completed preparation markers, legacy bundle compatibility, atomic
no-clobber delivery and policy/cancellation transaction boundaries are documented
in the [S05 contract](../repairs/2026-10-22-s05-author-custody.md). Its deployment
handoff requires a separate service-owner update and renewed client qualification.

## S07 evidence policy and migration

JUnit checks may set `min_executed` (default 0) in addition to `min_tests`; this changes the admitted profile digest. Skips still require `allow_skipped=true`. Reported executed count means collected minus skipped. Exit-only checks prove process success and have no test counts. Version-2 receipts distinguish complete boundary observation from unknown source equality. Historical receipts remain historical.

Enabled publication now requires `allow_external_ci=true` for the default `remote_effects=downstream_possible`, whether or not `github_repository` is configured. An operator may explicitly select `remote_effects=controlled_no_automation` only after establishing that destination contract. No destination is inferred safe by its URL. This does not suppress server hooks. Existing uncertain pushes retain original-address readback without fresh effects. See the [S07 contract](../repairs/2026-10-22-s07-evidence-authority.md) for dialect, provenance, receipt compatibility and client requalification.

S08 source relation validation now joins SQL commits/trees to the correct mirror and retained private-ref ancestry at backup and restore. Restore syncs finalized files and directories before publication and preserves a published destination on `RESTORE_PUBLICATION_UNCERTAIN`. Keep it offline for inspection. Follow the [S08 manual cutover](../maintainer/workflows/deployment-release-cutover.md); restored historical credentials/epochs grant no automatic activation approval.

## Repeated-use and maintenance limits

Automatic check dispatch rotates stable `(created,id)` windows of 100 across
calls and process reconstruction. Targeted submissions do not reset that
cursor. Blocked/unknown tasks keep their existing fences. A scheduler pass is
an explicit action; pure recovery does not launch or reconcile checks.

Fresh state includes queue and receipt-history indexes. Existing quiescent
schema-1 state supports local operator `optimize-admission`; opening state
never installs them. Context exports spool and hash complete Git source before
publishing ZIP/blob/SQL references; feedback streams its verified nested
context. Prefer a single context export to many tiny pages for large source.
Exports still require staging disk and a complete storage census. Per-mode
job counts are not a combined host resource reservation.

GC skips absent workspace receipts because it is a deletion operation; use
`doctor`/backup for integrity validation. It still validates evidence before
removing an existing workspace. No automatic age retirement of retry keys,
unknown operations or retained evidence is supported. See the complete
[S09 disposition](../repairs/2026-10-22-s09-repeated-use.md).

## Final qualification custody

The final S10 disposition separates server-enforced identity/source/policy and
loaded-release checks from declared client/model/route labels and observed
canaries. Read-only bootstrap does not qualify writes; there is no unqualified
MCP-write bypass. Keep the mutation capability blocked/unrun when permitted
bootstrap is unavailable, and use the separate authorized artifact path.

`author_requirements_eval.py` now exposes structural evidence scope explicitly.
Review claim relevance independently before accepting a capability; a matching
HEAD and hash cannot certify loaded/install identity or actual external work.
See [evidence adjudication](../testing/evidence-adjudication.md) and the
[final release matrix](../repairs/2026-10-22-s10-final-composition.md).

## Coexisting client observations after the reliability follow-up

Qualification is stored transactionally in private SQLite metadata, keyed by
instance, full source/runtime/catalog/output-profile binding, role and principal.
An authorized new observation replaces only the same binding. Replaying a
completed old qualification returns history without reactivating it. A valid
legacy `client-qualification.json` is read without migration until the first
authorized qualification write; that transaction preserves it in the registry.
The old file is retained but ignored for authority thereafter. Status/catalog
reads never migrate. Corrupt matched records or registry markers fail closed.

CLI `qualify-client` retains global-operator authorization. Scoped operators and
authors cannot qualify globally; no token, scope or lease is widened. HTTP
capabilities, MCP catalogs and mutations use the actual role and selected
profile. Backups include the registry through the existing SQLite snapshot;
historical restore does not grant activation or refresh any client. Observe an
actual permitted client write before recording support.

For an old quiescent schema-1 home, follow the maintenance/backup procedure above
and run `patchbay-author --home "$PATCHBAY_AUTHOR_HOME" optimize-admission`.
Opening the home is intentionally read-only with respect to index installation.
Record the installed wheel and loaded/disk identity independently, then
requalify the exact client after refresh. Retain current deployment SQLite
source/vendor WAL-fix provenance, capacity and offline restore obligations.
[Full migration, replay and external gates](../repairs/2026-10-23-postimplementation.md).
