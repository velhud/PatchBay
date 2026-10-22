# S09 repeated-use disposition

S09 was researched at `60817e37`; implementation starts from published S08
`e1d23d9889edef5c35d9cfead0f501a89dc1b60d`. S07 report/receipt/CI and S08
loaded-release/restore controls remain defining owners. Research's 80 acceptance
specifications are not 80 executed tests. Local checks use disposable state,
actual Git/SQLite and recording adapters, with no model or live service.

| Finding | Current decision and boundary |
| --- | --- |
| S09-F01 keyed admission | Retain the complete canonical job-record scan and cross-process lock. A disposable small-record sweep of 100/1,000/10,000 jobs read 157,090/1,571,890/15,728,890 bytes per lookup; three lookup-only lock holds at 10,000 were 0.464–0.491 s. This excludes start preparation and contention. A secondary index would need atomic generation/rebuild/corruption semantics; no negative cache or split-admission redesign is justified by an actual workload here. History-heavy repeated starts remain unqualified. |
| S09-F02 review inventories | Retain immutable targets, private Git refs and old selectors. Synthetic 1/16/128 targets with 1,000 paths serialized to 108,358/1,712,854/13,693,186 bytes in 0.0013/0.0249/0.2568 s on this run. These are serialization observations, not fsync or client latency. The per-target/count limits do not constitute a small aggregate memory guarantee. Externalizing inventories requires restore/reference migration and is deferred; large retained-target histories remain outside the qualified envelope. |
| S09-F03 candidate capture | Bound Git stdout at 32 MiB for identity inventories and stderr at 64 KiB, with a finite producer deadline and complete exit validation. Binary patch retention is bounded by the existing 2,000,000-byte integration limit; oversized patches are explicitly incomplete and cannot be applied. Changed-file inventory skips patch rendering while preserving the same full candidate tree, conversion checks and real-index invariants. Member diffs use their configured display cap while checking producer completion. |
| S09-F04 queue fairness | Automatic dispatch persists a `(created,id)` keyset cursor in existing metadata and wraps after exhaustion. Each pass examines at most 100 rows. Targeted dispatch does not move that cursor. Stable queues are covered in at most `ceil(N/100)+1` passes; continuous insertion has no fixed latency guarantee. The cursor grants no authority: run-state CAS, current worker policy/control/source checks and execution fences remain. |
| S09-F05 receipt history | Add focused queue and profile/updated receipt indexes. Fresh state includes them; old schema-1 state uses explicit `optimize-admission` under operator/quiescent maintenance, never Store-open migration. EXPLAIN verifies indexed order without a temporary sort. Fresh bounded receipt verification is retained; no positive/negative receipt cache can conceal corrupt evidence or authorize a check result. Whole-store and repeated-history costs are still workload-dependent. |
| S09-F06 source/feedback pages | Retain strong complete content validation on each request and fresh task scope/policy checks. No path/stat cache or cached authorization is introduced. For large sources, prefer one verified context/artifact export and local reading; repeated tiny pages still rescan source/receipt bytes. No page latency or token-saving claim. |
| S09-F07 export pressure | Context export streams complete Git members into private temporary spools, hashes incrementally, then streams into a deterministic ZIP. Feedback streams the verified nested context descriptor with delivery hash/size checks. Source/member/inventory/archive limits and SQL/blob references remain. Export consumes staging disk and storage-census time; independent mode job limits do not reserve aggregate export or mixed-mode host capacity. |
| S09-F08 retention | GC skips receipt hashing for already absent run workspaces, but verifies referenced evidence before deleting an existing workspace. Doctor/backup retain integrity checks. Pro event tail reads at most 1 MiB and returns the last 100 complete parseable lines in that suffix. Keys, uncertain operations, reports, review targets and publication receipts have no new age expiry. No archive/automatic retirement policy is invented; `retention_days` is not a promise of deletion. |
| S09-F09 inbox | Finite defaults: archive 128 MiB, expanded 512 MiB, member 64 MiB, 10,000 archive entries including directories. Blank selects defaults; nonpositive caps refuse. Central-directory records are counted in a bounded preflight before ZipInfo allocation, including dishonest end-record counts. Actual expanded bytes are checked during streaming, ordinary files obey member limits, duplicate names refuse. Listing defaults to 100, max 200, with full current count and next offset. Offset pages are current-list observations, not a frozen snapshot. Materialization reuses only an exact content/mode inventory; changed destinations refuse without overwriting edits. Copies remain independent writable files. |
| S09-F10 operating claims | Versioned Linux/offline mechanics only. SQLite library, source ID and vendor package are captured in implementation evidence. Vendor backport inclusion is not established by its version string. No package upgrade or deployment was performed. Manager and Author remain independent admission domains; combined host capacity, native/client cohorts, billed usage and savings are NOT_RUN/UNKNOWN. |

## Operating and recovery envelope

This release qualifies the exercised local mechanics, not a native worker fleet
or an ordinary-Pro journey. Synthetic history sweeps do not certify a sustained
10,000-record deployment. Run one explicit export at a time in a bounded
operator workflow; monitor staging space and keep the existing admission
reserves. Five/ten native workers plus Author commands and long-history/control
latency require a separately authorized host trial. No cross-mode resource
broker is added.

On an existing quiescent Author home, an unscoped local operator may run:

```bash
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" optimize-admission
```

This installs only additive indexes and is idempotent. It does not dispatch,
change leases, clear unknown execution, or retire records. Existing schema-1
state still opens without those indexes; install before relying on the indexed
cost bound. Back up and drain using the existing S08 maintenance contract.

An oversized/incomplete candidate remains in its worktree; retrieve retained
source or revise the explicit delivery scope. Do not accept an empty patch hash
as delivery. A changed inbox copy remains intact; use a new context directory
or explicitly preserve/reconcile that copy. List callers must traverse
`next_offset`; concurrent imports/cleanup can shift pages, so repeat discovery
when a stable selection is needed.

Retain old keys and evidence. Deleting job records can turn an old request into
a new effect; age alone does not prove retirement. Restoring older state still
requires the manual offline authority/destination reconciliation from S08.
Native macOS, real Luna, Docker availability, ordinary-Pro rendering/selection,
service/cgroup restart, reboot/power loss, remote CI and economics remain
separate gates. Local body sizes are not billed tokens.

## Recorded SQLite and query scope

The linked test library reports SQLite 3.45.1, source ID
`2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`,
vendor package `libsqlite3-0 3.45.1-1ubuntu2.8`. Its local changelog did not
establish inclusion of the WAL-reset fix. [Upstream WAL documentation](https://sqlite.org/wal.html#walreset)
identifies fixed releases and backports; a vendor version string is insufficient
proof of that patch. Concurrent production WAL durability remains unqualified
until the deployment owner establishes vendor fix provenance. No host upgrade
was performed by this integration.

For the exact profile-history query over 100,000 synthetic rows, the additive
index preserved the selected 20 receipts and reduced measured SQLite VM steps
from 302,414 to 137. This is a query-work observation on a disposable projection,
not an end-to-end latency, native throughput, or provider-cost claim. Fresh
receipt bytes and current host capacity are still checked at every admission.

## Verification-harness correction

The first completed S09 full run recorded 1,591 passes, seven skips and three
session/cleanup fixture failures. Their outer 4–6-second guards could cancel
paths whose existing supervisor proof budget is six seconds on Linux and whose
executor cleanup-call budget can be 21 seconds. A retained diagnostic run of
the unchanged three cases passed; that alone was not treated as remediation.
The fixtures now derive their external guards from those existing owner
budgets. Synthetic child lifetimes exceed the guards, so natural child expiry
cannot manufacture cleanup proof. Completion identity, exact result, PID death,
held mutation barrier and later authorized lock reacquisition assertions remain.
Production timing, signalling, cleanup and unknown-state fences are unchanged.

Subsequent suite results, including the full corrected run, are recorded in the
external implementation receipt. Verification uses per-run owned fixture roots
to retain diagnostic state independently of the shared pytest temporary-root
rotation. This is test custody, not an assertion that the prior failures were
caused by that rotation. No four-second native cleanup or host-load service
level is certified.
