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

Author HTTP admission defaults to eight active and sixteen pending requests,
with two active and two pending slots reserved for HTTP reads/control. Each
principal has two ordinary active slots and four pending slots; reads/control
may use one additional principal slot. The token bucket allows 60 requests per
minute with a burst of 20 and retains at most 4,096 opaque principal entries.
Queue admission waits at most 0.25 seconds, then returns `429 INGRESS_BUSY`,
`Retry-After`, a bounded jitter hint, and `X-Request-ID`. MCP POST requests use
the ordinary admission lane; the authenticated HTTP recovery and cancellation
routes remain independent control paths. Follow backoff rather than opening
parallel retry storms. Admission never retries a domain mutation.

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
source checkout, pass each approved path explicitly. Policy changes require
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
condition. The local service must not invent a decision or invoke another
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
operation paging returns `STATE_UPGRADE_REQUIRED`; ordinary recovery remains
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
and structured output. Oversize responses fail explicitly with `RESOURCE_LIMIT`
and returned effect identities. `effect_returned=true` means a mutation already
returned locally; the wire-size error does not undo it or authorize a new key.
Recover the original operation and use smaller pages or an operator artifact
export. The compatibility output profile remains the default.
