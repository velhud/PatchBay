# S03 observation, liveness and admission

This change follows S02 main `34fcdd92fe9481a0b6e9b68c580d555e829f93d5`.
The S03 research package reviewed older `362bd039`; its extracted-method
experiments identify mechanisms, not installed-service or ordinary-Pro passes.
The nine finding records include an unrun qualification gate. Existing S01/S02
authority, result recovery, retry and history contracts remain in force.

## Decisions and owners

| Findings / request | Disposition | Effective owner and behavior |
| --- | --- | --- |
| F001, F006 / CR01 | Implemented locally | `workers/runtime_legacy.py`: the existing projection token expires at the next discrete heartbeat/checkpoint silence threshold. Changed thresholds and clock rollback invalidate it. Existing content-hash suppression remains. Delta fields distinguish output activity from liveness, phase, cleanup and action-relevant status. |
| F002 / CR02 | Implemented locally | Author `interfaces/http.py` classifies bounded requests using the actual authenticated catalog/input/access owners. `interfaces/ingress.py` reserves bounded urgent concurrency and rate for scoped recovery, owned check cancellation and exact operator controls. Final authority is checked again. |
| F003 / CR03 | Implemented locally | `hub/app_v2.py` and `hub/runtime_v2.py` reuse per-group revision/snapshot work through `observation.ScopedObservationCache`. A generation recheck closes the snapshot construction race. Unrelated mutations no longer multiply full snapshots by observers. |
| F004 / CR03 | Measured; durable counter deferred | Existing indexes isolate unrelated history. Selected-group retained history still contributes linear revision-query work. Coalescing bounds its sampling frequency; a transaction-bound counter needs a demonstrated workload budget and migration/removal design. No schema or durable counter was added. |
| F005 / CR04 | Implemented locally; native envelope unrun | `tools/handler_legacy.py` lets cached coordination owners avoid blanket reconciliation. `jobs/executor.py` shares ordinary process-parent census work and offloads the routine supervision scan. Startup/cancel/cleanup retain forced fresh scans and exact ownership proof. |
| F007 / CR05 | Manager improvements implemented; Author wait and route gate deferred | Local `worker_wait` reads first, yields asynchronously and returns early on actionable/status changes. Default idle cap is 30 seconds. Hub future revisions trigger full resync. Author retains short pure reads: adding a long wait inside its synchronous MCP dispatcher would occupy a thread while idle. |
| F008 / CR06 | Optional no-change profile deferred | Full default output remains. Actual matched client comprehension and retrieval evidence is required before omitting narratives. Existing compact Manager output now retains pending/recovery cleanup flags. No token or quota savings are claimed. |
| F009 / CR07 | Local fixtures implemented; native/client qualification NOT_RUN | Actual disposable runtime, journal, SQLite and authenticated ASGI paths are tested. The native mixed workload and ordinary-Pro route require the separately authorized run below. |

`runtime.WorkerRuntime` remains the public local owner; adjacent `*_legacy.py`
files remain implementation layers. The Edge producer consumes its token and
snapshot through existing `edge_v2_legacy.py` / `edge_client_v2.py`; Hub reads
durable projections. There is one liveness derivation owner, not independently
tuned Hub thresholds. Observation does not launch a worker, renew a lease,
clear uncertainty, signal a process or accept a candidate.

## Observation contract

Native activity/checkpoint timestamps determine active/quiet/stale transitions.
Successful HTTP replies and observer heartbeats are separate facts. Threshold
deadlines change the token once when due, including a jump across multiple
thresholds; a rebuilt snapshot schedules the next deadline. A threshold crossed
during snapshot construction stays due for the next token read. A runtime restart
rebuilds its first snapshot. Elapsed age within the same classification bucket
does not defeat content suppression. Known process loss remains evidence-based.

Local deltas retain the old counters and `changed` field, adding
`activity_changed`, `status_changed` and `control_changed_fields`. Phase,
liveness, state, cleanup, messageability, review and integration changes may
change the next safe action without adding native output. No event is consumed
by a reader. Silence alone is not permission to stop or replace work.

Shared Hub observation is process-local, read-only reuse: 0.25-second routine
sampling, 60-second idle expiry and an LRU limit of 256 scopes. It holds no
transaction or domain lock across an async wait and creates no background
subscriber. Builders recheck generation after reading; continued churn makes
the entry immediately due. Every caller still applies its own filters, cursor
and output construction. Cancellation of one observer cannot cancel another
observer or durable work. These caches are not authorization caches.

Hub `projection_observed_at` is the time a Hub snapshot was built. It is not a
claim that native execution progressed then. `observation_refresh_seconds`
describes internal sampling; clients follow the separately returned polling
cadence. A cursor ahead of current revision receives a full snapshot with
`resync_required=true`, including after generation rollback/restore. Establish
a new baseline from that snapshot; do not interpret it as an empty delta.

Local wait outcomes are `actionable`, `status_changed` and `timeout`. Existing
terminal/stale/lost work returns immediately; phase/liveness/cleanup/state
changes and new selected workers wake the wait. It uses monotonic time and
short async sleeps, then performs a fresh readback. The configurable
`workers.status_max_wait_seconds` defaults to 30 and is bounded to 1–120.
This caps the idle observation budget, not queueing, final reconciliation,
encoding or network delivery. An operator must reserve that additional margin
inside the actual route deadline. No universal client timeout is asserted.

Author status/recovery reads remain pure and short; they do not dispatch,
recover execution or cancel checks. A disconnected caller does not undo an
accepted effect. Recover durable IDs and keep exact retry keys under the S02
contract. Ending a chat does not schedule automatic model continuation.

## Admission and process fences

See [Author operations](../authoring/operations.md#authentication-and-ingress)
for exact default active, queue, principal, classification and rate limits.
Urgent admission is bounded opportunity under ordinary saturation, not a
guarantee that all urgent callers succeed or that cleanup has completed.
Malformed/large bodies fall back to ordinary limits and normal validation;
bytes are replayed intact. Classification grants no effects. Task scope,
role, qualification, operation kind and current lease are checked before
priority, then checked again by normal dispatch. No arbitrary priority flag
or broad path prefix grants privilege. A classifier cancelled by its observer
retains its bounded owner slot until its thread finishes.

Ordinary process-parent sampling is shared and offloaded from the async
supervision loop. A partial census stays unknown. Forced cleanup observations
always bypass an ordinary sample, even when a caller supplies one. Per-job
descendant attribution and start identity remain required. Cached reads cannot
authorize continuation/integration through a cleanup fence. This change does
not claim every OS operation has become asynchronous or qualify native cleanup
on an untested host.

## Local evidence and cost limits

New regression files are `test_s03_monitoring.py`,
`authoring/test_s03_ingress.py`, `test_s03_observation_cost.py` and
`test_s03_process_cost.py`. They use actual application owners with disposable
state, controlled clocks, recording transport and synthetic queued check/process
inputs. No model inference is launched and the Linux/WSL2 guard is retained.
Tests cover real runtime-to-Edge-journal silence transitions, checkpoint expiry,
policy/clock/restart changes, status-only deltas, cache hits, early waits,
observer cancellation, cleanup fields, scoped reuse, future-cursor resync,
generation races, LRU/expiry, partial census and forced fresh census.
Authenticated HTTP/MCP tests hold legitimate exports/reads and admit owned
queued-check cancellation; the predeclared two-second fixture target is a local
test bound, not a deployed latency promise. Rejections include invalid and
oversized bodies, wrong role/scope, misleading flags and bounded queue/rate use.

A separate actual SQLite/Hub application measurement used five synthetic quiet
worker projections, ten selected-group operations and 100,000 unrelated retained
terminal operations. With unrelated write churn, 1, 5 and 10 concurrent observers
each needed **one snapshot and six group-revision queries** over a roughly
0.8-second wait; elapsed time was 0.801–0.803 seconds. CPU was 0.0046–0.0068
seconds. Each full selected result was 9,814 structured JSON bytes. These are
local fixture measurements, not billed tokens or ordinary-Pro context use.

Ten raw revision queries cost approximately 450 VM steps/query before unrelated
history and 460 afterward (sampling resolution 100 instructions). With 50,000
operations **inside** the selected group, each query cost about 1,500,160 VM
steps; ten took 0.175 seconds. Ten observers of that large group still shared
one snapshot and six revision queries, taking 0.849 seconds elapsed and 0.092
CPU seconds. This confirms both the existing unrelated-history index and the
remaining same-group cost. It supports coalescing first, without pretending
that a durable monotonic counter has been implemented or every group size
meets a production budget.

The optional compact/no-change profile remains deferred: the full default still
repeats summaries. A matched A/B trial must check exact scope/revision,
freshness, full-set critical counts, uncertainty/cleanup, partial pages, source
identities and complete retrieval before reducing output. Reject a profile
that hides a decision or merely adds retrieval calls. Bytes are not tokens.

## Operator handoff: remaining qualification

No running connector, deployment, browser profile or production route is
updated by this source change. Build/install the exact reviewed artifact in
the separately authorized disposable deployment, refresh its authenticated
catalog and repeat write qualification rather than copying an old pass.
Capture source/tree/wheel, Python/OS/kernel, CLI/resources, explicit approved
Luna model/effort, runtime limits, host capacity, role, connection, catalog,
instructions, negotiated MCP version and output profile. Keep credentials and
private access identities out of public evidence. Separate Manager and Author
results; Author must never initialize model workers.

Before measuring, set the route deadline and explicit margins for admission,
decode, idle wait, final refresh, encoding and delivery. Set host CPU/memory,
event-loop-delay and urgent-admission targets. The local two-second admission
assertion and 30-second idle default are not substitutes for those budgets.

Run five supported lanes, then ten, with 1, 2 and several independent observers:

| Lane | Required observation |
| --- | --- |
| Quick success | Exact report/check receipt becomes reviewable once while other lanes continue. |
| Twenty minutes of intentional quiet | Owned native/check workload stays present; silence crosses both thresholds without fabricated progress, cancellation or replacement. Emit a genuine event afterward and check classification recovery. |
| Genuine input/approval blocker | Expose the supported decision and allowed action; if unsupported by this mode, fail explicitly rather than simulating input support. |
| Deterministic failure | Preserve exact failure log/report; another lane's success cannot replace it. |
| Report ready, cleanup delayed | Report remains readable while continuation/integration stays fenced until exact fresh cleanup proof. |

First run each fault separately, then compose overload, a lost response and
simultaneous terminal/cleanup changes. Drop a computed wait response and recover
with/without its cursor; cancel one observer while execution continues under
the same ID; exhaust ordinary slots and rate from one and several principals
before authorized MCP recovery/cancel; flood output beside the quiet lane;
churn unrelated groups; complete several lanes in one interval; retain failed,
uncertain and cleanup-pending workers off-page or outside `active_only` views.
Fail observation while execution stays alive and retain uncertainty/freshness.
Use isolated clock fixtures for time shifts. Revoke observer permission during
a wait and verify final authority without revoking execution implicitly.
Rehearse isolated observer/service restarts under the durability contract.
End a chat without a final handoff, then recover the same task/group and all
remaining obligations from a fresh authorized conversation.

Record per-layer request IDs, durable identities, generations, calibrated UTC
and monotonic durations, admission/wait outcomes, response bytes, actual
snapshot/revision counts and SQL VM work, censuses/stat reads, writes/fsyncs,
thread occupancy, CPU/RSS, event-loop delay, change-detection latency and
cancellation/cleanup evidence. Keep failed attempts and full logs once, by
identity. Compare group waits with a bounded short-poll baseline and shared
observation on the same workload. Only evaluate an optional compact profile
after correctness passes, with matched ordinary-Pro prompts and comprehension.
Do not conflate browser rendering failure, no further host calls, ingress
delay, stale projection, native silence and execution failure.

Native Linux/WSL2 execution, deployed admission and end-to-end deadlines,
ordinary-Pro tool visibility/comprehension, five/ten concurrent native lanes,
twenty-minute execution, quota savings, reboot/power-loss and multi-hour model
continuation remain **NOT_RUN by S03 local qualification**. Capability-specific
gates limit claims about those capabilities; they do not erase useful local
source/application evidence. See [TESTING.md](../../TESTING.md) and the
[MCP route qualification contract](../reference/mcp-interface-contract.md#qualification-still-required).

Protocol references: [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[MCP cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
and [Python 3.12 shield semantics](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.shield).
The HTTP route's real deadline must be measured/configured independently.
