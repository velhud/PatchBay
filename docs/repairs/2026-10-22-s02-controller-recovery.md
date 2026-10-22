# S02 controller loss, retries and ownership

The incoming S02 review was provisional: its source download failed, inventory
was empty and probes were blocked. It supplied hypotheses and unexecuted scripts,
not newly confirmed defects. Local verification started from
`362bd03923930b89c0acea18a5a25b81bcb43032`, retaining S01 and the Author Trial
follow-up. The scripts' old pin, hardcoded paths, nonexistent store method and
catalog-changing fault injection were adapted into disposable current-source
fixtures. No production endpoint, connector, browser, service or model worker
was operated for this repair.

## Change-request dispositions

| Request | Local disposition and defining owner |
| --- | --- |
| CR01 response faults | S01 invalid/oversize-result recovery retained. Author MCP now guards rendering/UTF-8 encoding and unexpected post-admission exceptions. Hub uses bounded transient admission IDs verified against its existing store; local keyed workers recover existing job IDs. Publication records unexpected lost replies as `publication_unknown`. No success is inferred from an error. |
| CR02 request framing | Shared pure framing validation precedes domain dispatch in Author, local Manager and Hub V1/V2. ID-less tool calls never admit work; malformed IDs fail; HTTP notification acceptance is 202. Stdio rejects batches before members. Real local HTTP and stdio entrypoints, including an owned stdio subprocess, were exercised. |
| CR03 local retry identity | Optional start/message keys live in existing atomic job snapshots under a cross-process admission lock. Exact retries return the retained admission; conflicting arguments fail. Local keyed integration now requires its signed preview token. Unkeyed compatibility remains explicit; no fuzzy prompt deduplication or follow-up queue was added. |
| CR04 ownership | Existing Author principal-scoped credentials/keys and lease epochs retained and tested. A delayed Hub resume now rechecks current active participant before its final group write, preserving a newer explicit takeover. Local owner namespaces remain distinct from Author authentication. |
| CR05 large recovery history | Full-set uncertainty classification retained. New opt-in Author snapshot traversal pins operation identities across activity; default strict traversal still rejects same-task changes. A fixture traverses more than 200 uncertain operations once each despite interleaved same/unrelated activity and backdated inserts. |
| CR06 uncertainty and progress | Existing exact-identity operator resolution remains Linux-only and local-only. Unit census fixtures verify rejection and idempotent release; a real owned process demonstrates admission-lock release after its death and legitimate successor progress. No force flag or uncertainty bypass. Native Docker/process qualification remains due. |
| CR07 retention/restore | Existing retention owners traced and contract clarified below. No second replay journal, automatic expiry or restore redesign. Cross-restore credential/epoch and discarded-evidence safety remain S08/S09 work. |

## Identity and authority map

| Surface | Identity and admission owner | Transaction / effect boundary |
| --- | --- | --- |
| Author request | Authenticated principal and task scope in `authority`; retry key/fingerprint in `persistence.Store.begin_operation` | Role/input checks precede dispatch. SQLite immediate transaction admits one principal-scoped request. A transport request ID is correlation only. |
| Author candidate | Task/revision/commit/tree plus lease token/epoch in `revisions.materialize` and `revisions.leases` | Immutable Git construction precedes final SQLite task-pointer CAS; lease and base are checked again there. A late old writer cannot advance the pointer. |
| Author checks | Existing operation/run, supervisor and container ownership in `execution.runner`, `receipts`, `recovery` | Admission, launch acknowledgement and terminal receipt are separate. Unknown execution stays fenced. Canonical receipt acceptance checks the current owned run/state; missing process evidence is not success. |
| Author publication | Actor-bound approval, current content/policy/receipt, task lease and existing publication operation | Remote Git is outside SQLite atomicity. Exact feature-ref readback and expected-head push lease precede completion; default branch and source checkout are untouched. |
| Local worker | Configured owner scope/hash, method, caller key and semantic argument fingerprint in `workers.requests`; existing `jobs.manager` snapshots | Process-released file lock serializes keyed lookup/create. Job identity is persisted before scheduling. Recovery returns that admission and never schedules another turn. |
| Local integration | Existing signed preview token, candidate/delivery identity and integration receipt in `runtime_integration` / `runtime_legacy` | Preview is separate; keyed application requires token and key. Existing crash-before/after receipt recovery is retained. |
| Hub group/operation | Authenticated principal, work-group participant, tool/target/key/fingerprint in `store_v2`, `runtime_v2`, `broker` | SQLite transactions and revision checks own admission, participant updates, attempts and results. Resume rechecks ownership after admission. |
| Edge attempt | Operation, attempt, generation, fence, payload and result hashes in `edge_journal` | Intent/result/outbox transactions preserve exact replay and uncertainty. Transport acknowledgement alone is not canonical Hub completion. |
| Pro Request | Existing request owner/revision and serialized store guard | `respond` stores content; explicit `dispatch` starts/messages a worker. Local legacy dispatch has no caller-key replay guarantee; inspect request and worker state after response loss. This change does not silently convert it into keyed dispatch. |

Local ownership is coordination within a configured authenticated server, not a
replacement authentication system. A new transport with the same configured
owner can recover its keyed admission. Changing token-derived owner scope may
require explicit takeover and is not the same as rotating an Author credential
for an unchanged principal. Neither public recovery nor a cursor discloses lease
tokens. Revoked credentials are denied before replay; a retained key grants no
authority of its own.

## Crash and response boundaries

Before admission, framing/input/role refusals imply no domain dispatch. After
admission, a missing/invalid response does not establish absence of an effect.
Post-return schema, budget and UTF-8 faults return bounded recovery metadata;
unexpected Author/Hub exceptions return `EFFECT_OUTCOME_UNAVAILABLE` without
their exception text. Read faults remain explicitly nonmutating. Trusted IDs
come from validated arguments and authorized durable readback, not corrupt
handler output. Hub admission capture is transient, bounded to two distinct
IDs to detect ambiguity, and re-reads the committed row; rollback creates no
trusted admission.

An Author candidate object created before a failed lease/base check is not the
current candidate. For publication, a push can succeed before its reply is
lost. Unexpected exceptions mark the existing operation `publication_unknown`
without regressing an already committed terminal outcome. Explicit same-key
replay reads the exact remote feature ref; if it already equals the approved
commit, no second push occurs. A real disposable bare-Git fixture verifies this;
its synthetic check receipt is unit input, not backend qualification.

Local keyed start/message recovery proves admission, not worker completion.
After a crash between persisted admission and scheduling, the existing restart
reconciliation may show a failed/uncertain job. Replay does not schedule it
again. Inspect and deliberately choose a successor request after resolving its
state. Active-turn messages are refused rather than queued. Exact same-key
retries with `auto_suffix=true` still replay the original admission; a new key
or unkeyed call may request legitimate independent work with identical prose.

The Hub broker retains attempt/fencing checks, equivalent-receipt replay,
cancellation and terminal-conflict rules. Its existing narrow exception permits
an exact fenced late receipt to repair an absence-based manual-recovery blocker;
this is not permission for arbitrary terminal regression.

## Recovery procedure

1. Read existing task/group/worker status and uncertainty before mutating.
   Follow error `recovery_actions`, the original operation ID and caller key.
2. Retrieve complete evidence. Author recovery's bounded recent/active views
   are indexes; follow all `author_recent_operations` pages. With
   `traversal="snapshot"`, retain the cursor and next offset. Initial identities
   are pinned; each page's states are fresh in its own transaction. This cursor
   neither freezes authority nor asserts all operations remained unchanged.
3. Recover current candidate/lease/participant again. Use the exact original
   key and semantic arguments only where replay is supported. A corrected
   terminally rejected request or intentional independent attempt needs new
   intent. Never use a new key or suffix as a blind retry.
4. For unknown execution, preserve immutable receipt and lifecycle evidence.
   The existing local operator assessment requires exact process/container and
   daemon identity, no active/uncertain competing runs, and repeated locked
   census. Drain healthy work under paused admission if necessary; do not relax
   quiescence checks or cancel unrelated work merely to force resolution.
5. An applied resolution preserves the historical unknown outcome and records
   a separate operator attestation. It releases admission without declaring a
   pass or retrying. No public read performs this reconciliation.

## Retention and restore limits

Author operation/idempotency rows have no age-pruning job. Referenced evidence
is retained by its existing graph. Worker identity snapshots are excluded from
ordinary age cleanup, so optional retry keys last with those records. Explicit
deletion/state retirement/old-backup restoration is outside that guarantee.
Hub operation records remain the replay owner. Edge pruning removes only
certain, acknowledged and Hub-confirmed outbox rows; intent/attempt identity,
fence and result-hash tombstones remain for duplicate detection. Uncertain
receipts are preserved. This is a traced existing implementation, not a newly
measured deployment retention promise.

A restore rewinds operations, credentials, revocations and writer epochs. Keep
restored state isolated and reconcile post-backup external effects and authority
before opening admission. Current code does not provide a cross-restore global
anti-replay generation or rejection after arbitrary replay-record deletion.
S08/S09 own that policy/design and its backup fault qualification. Snapshot
history cursors are not promised across restore or unsupported pruning; restart
discovery after maintenance. No fixed retry horizon, RPO, RTO or power-loss
guarantee is claimed by S02.

## Verification boundaries and deployment handoff

The new regressions are listed in [TESTING.md](../../TESTING.md). They use real
disposable SQLite/Git/files and owned processes with recording/no-inference
executors, or explicitly fake host/Docker census. They do not use paid workers.
The external implementation receipt records actual commands, totals and retained
failed attempts; historical source or package counts are not current passes.

Before deploying, pin the published commit and build, run the complete Linux/
WSL2 Author matrix including real offline Docker lifecycle/cancellation and
operator-resolution evidence, then perform the ordinary-client recipe in the
[MCP interface contract](../reference/mcp-interface-contract.md). Include a lost
reply/reconnect, credential rotation for one principal, delayed old writer after
takeover, and full history traversal. Refresh metadata and requalify the exact
runtime/catalog/role/route/output profile; S02 changes invalidate old runtime
bindings. Model execution and Mac worker cleanup require their own authorized
qualification. No deployed connection was refreshed by source publication.
