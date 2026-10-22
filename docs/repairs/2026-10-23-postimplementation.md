# Postimplementation reliability follow-up

This focused follow-up reconciles public main `39b0b626446a07dcdbbcafd7b8449ef70598d349`
after S01–S10. It changes four current defects without replaying historical
source packages. Author remains independent deterministic transport; Manager
retains explicit Luna-only execution and its existing authority fences.

## Current defects and repairs

| Item | Defining owner and resulting behavior | Local witness |
| --- | --- | --- |
| PF-01 | The effective async inbox handler awaits bounded execution of the complete artifact writer. A per-store executor admits two imports by default and refuses excess admission without queueing. | Actual `MCPProtocol` → effective `ToolHandler` with synchronized I/O, concurrent status, saturation, cancellation, slow-drip HTTP and publication failures. |
| PF-02 | Testcase informational descendants use the same structural-outcome/namespaced-element refusal as suite information. Escaped text and supported properties remain information. | Parser → canonical receipt → feedback → publication candidate gate, including nonzero exit and positive nested UTF-16. Process inputs in these joined tests are synthetic. |
| TEXT-01 | A completed worker is actionable on first observation; an unchanged mixed completed/running selection subsequently waits for a meaningful change or budget expiry. | Actual public defaults plus distinct callers/selections, reconnect, new turns, completion, persistent urgent/cleanup signals, timeout and observer cancellation. |
| TEXT-02 | Author qualifications coexist in the existing private SQLite metadata core by instance, full release/catalog/profile binding, role and principal. Replaying an old completed operation returns history without reactivating it. | Admissible global-operator recording for scoped principals, actual HTTP/MCP catalog and context mutation gates, concurrent writes/reads, migration, replacement, restart/replay, expiry/drift/revocation and backup restore. |

The four root reproductions initially failed: eighteen failures comprised one
blocked-control case, one repeated-wait case, fifteen hidden-outcome variants
and one displaced-principal case. These are controlled application-path
reproductions, not live Pro observations. Fixture corrections and intermediate
failures remain in the private implementation receipt; they are not suppressed
or counted as passing runs. See the machine-readable
[disposition](../testing/postimplementation-disposition.json) for all 26 supplied
specifications and additional witnesses. The supplied specifications retain
their original NOT_RUN research status; only named executed witnesses acquire
local results.

## Artifact writer lifetime and effect uncertainty

`artifacts.max_concurrent_imports` defaults to 2 and must be a positive integer.
`artifacts.import_timeout_seconds` defaults to 60 seconds, positive and finite,
covering admission through download, extraction/copy and publication with one
monotonic deadline. Socket waits are bounded by the remaining deadline; a
slow-progress response cannot keep extending the operation. The existing
finite compressed/expanded/per-file/entry/path/hash checks still apply.

The async boundary captures the immutable request coordination fields and
context before offload, without carrying the mutable transport session map.
The existing HTTP credential boundary still authenticates the request; owner
references are coordination metadata, not new authentication or grants.

Only the writer owns its private `.import-*` staging and reserved destination.
It publishes `artifact.json` last, after exact bytes and manifest are present;
list/inspect cannot accept a partially published artifact. Existing destinations
are never replaced. Cancellation or deadline expiry requests cooperative stop;
it does **not** kill a thread or prove that blocked I/O has returned. Capacity
remains held until that writer exits after its cleanup attempt. A cleanup
failure is an explicit error; no successful cleanup is inferred from an ignored
exception. Uninterruptible I/O can outlive the caller's deadline. A racing
complete publication can survive a cancelled/lost response. Inspect the inbox
and exact artifact hash before deciding whether another import is necessary;
there is no automatic retry or exactly-once import claim. An orphaned private
stage after process loss requires offline, ownership-verified maintenance, not
blind deletion while a writer might still run. Import abandonment never stops
an independent worker.

## Wait observation boundary

Wait observations use the existing bounded TTL/LRU monitoring storage. They are
separate from status/list snapshots and belong to the caller's coordination
identity and exact repository/scope/ownership/time filters. They are advisory
cadence state, not authority, durable acknowledgements or worker state. Another
caller, a reconnect with a new client identity, cache eviction/expiry or process
restart may receive a fresh first completion. They cannot consume another
manager's event. Unrelated cache invalidation does not erase wait observations.

Urgent lost/stale/failed states and unresolved terminal cleanup remain
immediately actionable. New work, a new turn of the same worker, meaningful
phase/event/checkpoint/cleanup transitions and completion wake an active wait.
All-terminal or empty selections return truthfully; there is no unconditional
sleep. Explicit/default budgets retain minimum cadence and the configured
server cap. Cancelling or timing out this observer does not cancel work.
Transport-specific delivery margins still require actual route qualification.

## Qualification storage and recovery

The private metadata registry has a version marker and opaque full-binding
keys. Reading catalogs or status never migrates state. Until that marker exists,
a valid legacy `client-qualification.json` can satisfy only its exact identity
and binding. The first authorized qualification write migrates that observation
and records the new one in the same SQLite transaction as operation completion.
The legacy file stays untouched for provenance; after migration it is ignored
for authority, including on later replay. Invalid matched records/marker fail
closed. Other principals' observations are not publicly enumerated.

The current global-operator restriction on qualification remains. Scoped
operators/authors cannot perform this global action. The trusted local operator
may record an actually observed named-principal trial; a named unscoped operator
is still restricted to its own explicit principal. Qualification creates no
token, lease, task grant or bypass. Expiry, current source/loaded runtime,
catalog, profile, role, principal and credential revocation still apply. HTTP
capability reporting now uses the actual role and selected output profile,
matching the MCP catalog and mutation gates. Same-binding replacement changes
only that binding; old completed retry keys return their saved historical
result without replacing newer or unrelated observations.

No file/schema overlay is needed. CLI `qualify-client` and HTTP/MCP eligibility
use the same core. SQLite backups already include all metadata, and legacy
file retention remains supported. Restoring a historical database restores its
historical observations and authority state; it does not activate a service,
refresh a client or authorize reuse. Retain the existing offline restore,
credential/lease reconciliation and deliberate activation procedure. A source
update requires new real client qualification; fixture observations are not
valid deployment evidence.

## Optional PF-03 and external PF-04

Local Pro Request dispatch remains explicitly **non-idempotent**, with no retry
key. The tool description now says never automatically retry after a lost
response. The actual route invokes its effect once; controlled lost-reply
coverage checks that it does not loop/retry. Read the Pro Request and origin
worker before a new deliberate dispatch decision. Busy/name-conflict refusals
do not make it replayable. Hub replay is a separate contract. The optional
keyed-dispatch design and its changed-key/replay specifications are not selected;
no second journal, hidden automatic retry or new dispatch authority is added.

For an existing Author home, drain admission, resolve/fence unknown execution,
prove owned cleanup and create/validate an offline backup before maintenance.
Opening old schema-1 state does not install admission indexes. Run the existing
local-operator `optimize-admission` while quiescent and independently inspect its
index result and retained logical state. Record immutable installed wheel/build,
interpreter/dependencies, loaded startup identity and current disk identity;
Git main alone does not identify a running process. Disposable old-home index
and offline installed-wheel checks are distinct from an actual deployment.

Record the deployed SQLite library/source ID/vendor build and establish vendor
WAL-fix provenance; its version string or `synchronous=FULL` alone is not proof.
Combined host capacity, service-unit drain/activation, physical durability and
old historical receipt/backup reconciliation remain deployment-owner duties.
No live home, account, service or provider was changed by this follow-up.

Actual ordinary-Pro qualification requires an authorized exact account/client,
model/effort, ingress/protocol, refreshed catalog/instructions, legitimate scoped
authority and a permitted observed write canary. There is no unqualified-write
bootstrap bypass. If that lawful route is unavailable, keep writes blocked and
record BLOCKED_ENVIRONMENT/NOT_RUN, using the independent authorized artifact
workflow. Fresh-chat recovery must be observed in that exact client. In-process
MCP fixtures, local native commands, and Linux package checks cannot establish
ordinary-Pro, native macOS, Docker, service restart, reboot/power-loss, live Luna,
long soak, fleet capacity, billing or economic qualifications. These remain
explicit external gates, not unfinished independent code repairs.

## Recorded local verification

The final complete suite passed 1685 tests with 7 skips. The
dedicated Author suite passed 402 with 4 skips, including the real
185-second local command (185.54 seconds observed). Compilation, audit
regressions, ordinary/terminal MCP evaluators, Hub/Edge and Hub V2 evaluators,
old-schema entrypoint restart, Author interface evaluation, dependency check,
wheel build, offline install and 167 Python-source-file parity all passed.
The requirements evaluator intentionally returned 1 with all 52 operational
cases NOT_RUN. Existing-home indexes were exercised only on disposable state.

The initial full attempt retained 1 failure, 1681 passes and 7 skips. It found
a reader capability compatibility regression: the reader must retain its own
sanitized qualification inspection while write eligibility remains role/profile
restricted. That behavior was restored without weakening the existing privacy
assertions. Final compile/full/audit reruns cover this correction and complete
terminal-runtime cleanup urgency. The dedicated Author subtree/shared helpers
were unchanged throughout its run; Manager-only corrections preceded the
remaining evaluators and build. An earlier interrupted run and intermediate
fixture failures remain separate evidence.

Final full/Author fixtures used an owned tmpfs root; affected disk-fixture
results are retained independently. No all-green physical disk or client
qualification is inferred. The machine-readable disposition and external
implementation receipt record commands, logs, counts, skips, source lineage,
installed identities and verified remote publication.
