# S06 candidate, review and delivery contracts

S06 follows S05 main `c3b2ef8bde321711151b7ccfd96f029b3e8ff2b8`.
The research supplied 12 findings, 12 proposed changes and 72 unrun acceptance
oracles. They are engineering inputs, not 72 observed failures or passes. The
implementation extends the existing worker Git, job, Hub group and independent
Author owners. It adds no model, review service, fleet scheduler or graph engine.

| Request | Implemented behavior |
| --- | --- |
| CR01 | Worktree admission passes the resolved commit explicitly to Git and verifies actual HEAD/object format before job admission. A short worker-allocation reservation covers creation and owned rollback without blocking independent isolated starts behind a busy destination checkout. Existing paths/branches are preserved. |
| CR02 | The temporary index captures baseline, HEAD and real-index tracked paths with working-tree precedence, including newly tracked ignored files. Ignored untracked scratch remains excluded. The real index stays unchanged. Unmerged and sparse/skip-worktree states are refused. |
| CR03 | Copied context retains versioned kind, executable mode and raw Git blob identity. Changed type, mode or content prevents exclusion and delivery. Legacy byte-only context requires current known regular-source revalidation; uncertainty preserves the workspace. Exclusions are literal paths. |
| CR04 | Machine patches disable external diff and textconv. Supported changes are ordinary/executable files and symlinks; changed/dirty gitlinks are refused. Raw filesystem kind/mode/blob identities must match the captured tree. Attribute or line-ending conversions that prevent this equality are refused. |
| CR05 | A retained review target binds worker, turn, baseline, tree, inventory, brief digest and report-source reference. Private Git refs retain source across edits/restarts. Existing inspect reads accept `candidate_id`. Explicit `review_decision` records controller acceptance separately from mechanical delivery and advisory prose. |
| CR06 | Apply uses the same captured candidate used for validation. Reservation order is worker source, destination, then short job update. The destination remains reserved through exact affected-path readback and receipt persistence. Follow-up/cleanup/preview coordinate on the worker source reservation. |
| CR07 | Applied-token result and delivered commit/tree/ref are persisted in one job-options mutation. An owned pre-record ref is adopted only for its exact intended tree/parent. Anchoring failure retains delivery and sets `baseline_repair_required`; cleanup/follow-up cannot treat it as a healthy baseline. Exact-key replay can repair bookkeeping without another apply. |
| CR08 | Failed Git observations cannot certify clean state. Affected pre/postimages include absence, kind, executable mode and raw blob identity. Recovered exact postimages prove content outcome, without claiming witnessed causal execution. Replayed receipts are historical; `current_target_matches` is separate. |
| CR09 | Peer context captures one candidate per source worker and derives all member diffs from that tree. Errors, per-member caps, protected content and aggregate omission make coverage partial. Retained target selectors support later pinned reads. No cache or token/billing saving is claimed. |
| CR10 | Hub forwards `cleanup_workspace=true` with discard false/default to Edge. Complete delivery and process-cleanup proof remain authoritative. True remains a separate explicit decision to discard undelivered work. |
| CR11 | Deterministic Author composition rejection ends the existing parent as blocked/needs-decision with exact dependencies, conflict and child-task identity. Same-key replay retains that decision. An accepted package child remains recoverable and is never deleted or resubmitted to reconstruct the parent reply. |
| CR12 | A dependent isolated worker may use an explicit same-repository `source_candidate`. Hub groups optionally retain a lightweight `source_contract`; complete closure requires matching-generation, matching-tree evidence for its declared combined checks. This is explicit controller attestation, not a canonical execution receipt. |

## Review and dependency use

Request `view=integration_preview` for the implementation worker. Retain
`review_target.candidate_id`, `snapshot_commit`, `baseline_revision`, `tree_sha`
and `contract_sha256`. Read exact retained source with the existing inspect
selector, for example:

```json
{"worker":"Implementer","view":"file","candidate_id":"RETURNED_ID","file_path":"src/main.py","start_line":1,"max_bytes":8192}
```

Use `view=changes` for its inventory and `view=diff` for a member patch. Reads
remain scoped to the resolved worker and configured repository; protected paths
stay withheld. Line reads are a sanitized UTF-8 representation, not raw-byte
export. Binary/non-UTF-8 files and files exceeding read policy return explicit
incomplete results. Retrieve such content through the retained Git commit in an
authorized local source environment; no read-policy bypass is implied. Retain
line continuation and completeness fields. A clipped excerpt cannot establish
complete review. Retained review metadata is bounded to 128 targets per turn
and 2 MiB per target; exhaustion refuses new retention rather than deleting old
review evidence.

`context_detail=review` passes this same immutable target to a reviewer. Its
ambient CWD may contain old base source. Inspect the named target, not that CWD
or a subsequently edited worktree. A read-only reviewer must report unsupported
executable verification or request a separate authorized `isolated_write` check
lane with `source_candidate={"worker":"Implementer","candidate_id":"RETURNED_ID"}`.
That lane has its own writable worktree and least-privilege check brief. This
release does not run or allocate it automatically.

Supply `review_decision={"candidate_id":"RETURNED_ID","disposition":"accepted",
"evidence_refs":["retained-review-reference"]}` with integration to record the
controller decision. `not_required` explicitly states that no separate review is
required. Omitting the field preserves direct-controller tasks and records only
delivery acceptance. A source/turn change rejects an old decision. Compatibility
`review_disposition=accepted` continues to describe delivery disposition; the
separate `source_review`/`controller_review` fields describe source-review scope.
No report keyword, model prose, test filename or exit code supplies semantic
acceptance automatically.

Default isolated starts use committed source. They do not inherit dirty source
left by no-commit integration. Use an explicit retained `source_candidate` for
a dependent lane or deliberately prepare a committed baseline under separate
authority. No source selector commits or modifies the operator checkout.

## Application and recovery limits

Worktree-only application preserves both real indexes. Unrelated explicitly
accepted dirt can coexist. Touched destination paths must equal the exact
candidate baseline; even a patch-applicable out-of-hunk edit is refused before
effect. This deliberately supported profile avoids misreporting a mixed file as
the candidate's complete postimage. There is no implicit `--index`, three-way
merge, submodule implementation or arbitrary filter-conversion guarantee.

Cooperating requests share file-backed reservations. Noncooperating filesystem
writers remain outside atomicity; failed post-readback leaves the applying
intent uncertain and preserves both workspaces. A post-apply observation error
is not a no-effect claim. Only keyed/token-bound calls have the retained
exact-replay contract; legacy unkeyed application remains explicitly weaker.

If a receipt says applied with `baseline_repair_required=true`, preserve it and
replay the exact original token/key. The replay first validates retained source
and current affected-path evidence, then repairs the commit anchor/options.
It never reapplies simply to repair bookkeeping. Legacy raw-tree baselines require exact-evidence commit anchoring before another turn or clean-state claim. Known unkeyed anchoring failures retain `_worker_delivery_pending_anchor` identities for operator reconciliation; an absent original key is not permission to reapply. Replaying an older receipt cannot rewind a different current delivery baseline, and a tree recurring in a later turn does not rewrite earlier operation receipts. Incompatible old split records
remain fenced. A later target edit does not erase historical application;
`receipt_scope=historical_delivery` and `current_target_matches` distinguish it.
Retained review refs remain as evidence after worktree cleanup; they are not
pruned silently by this release.

Author conflicts/no-op/policy refusals are deterministic decisions. Unexpected
persistence interruption remains recoverable; external or execution uncertainty
is not terminalized by the new conflict handler. Accepted-child replay continues
to require fresh combined checks before any release action.

## Combined verification and semantic authority

The optional group `source_contract` declares `input_generation`, natural-language
`lane_relationships`, `interface_owner`, `join_condition` and exact
`required_combined_checks`. Independent/research lanes can declare no combined
checks; existing groups retain compatibility. At complete close,
`combined_verification` names that input generation, the actual tested
`combined_tree`, current `delivery_generation` from group status, and check name/tree/outcome/evidence references. Durable worker content or integration changes invalidate that generation. This fence covers recorded source facts; a noncooperating checkout edit requires fresh controller readback/checks and cannot be inferred remotely. Mismatched,
failed or missing declared checks block complete closure; partial closure can
retain honest unfinished work. Existing stop, owner, uncertainty and worker
closure gates remain.

The exact shell validates identities, bounded schemas, authority and declared
state obligations. Meaning and sufficiency of the task, review, join condition
and check evidence remain human/controller decisions. The attestation is labelled
`canonical_execution_receipt=false`; it does not manufacture a runner service or
infer a pass by parsing prose. New explicit fields participate in keyed request
identity; omitted fields preserve legacy retry fingerprints.

## Qualification and deployment handoff

Run the entire matrix in `TESTING.md` on the exact final source, including all
S01–S05 regressions and S06 controlled faults. Retain full-suite and Author JUnit,
MCP/Hub/Edge/restart/interface results, wheel/source parity and dependency checks.
The external receipt maps each original oracle to actual local evidence or an
explicit remaining boundary. Controlled executors perform no inference; synthetic
Author eligibility/receipts do not qualify a native backend.

macOS preserves the Linux/WSL2 Author guard. Ordinary ChatGPT visibility/rendering,
real Luna process cleanup, strict Docker, production multi-project capacity,
reboot/power-loss and deployment remain separately authorized qualification.
The 52-case operational requirements evaluator remains `INCOMPLETE` without
observed per-case evidence. Local source publication does not update a service,
connector, browser, account or trial. The service owner should install the
published wheel/source through its existing procedure, preserve state, refresh
metadata and qualify the changed exact runtime/catalog/role before live writes.
Keep the selected read-only trial read-only until that separate qualification.
