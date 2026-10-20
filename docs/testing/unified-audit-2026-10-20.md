# Unified candidate audit and qualification boundaries

This document maps the supplied October 18 public audit and October 19 Author
implementation plan to the consolidated source. Test execution receipts belong
to a specific commit; this source map alone is not a release qualification.

## Current status

The source-to-plan map below records the initial consolidated audit and its
then-open acceptance boundaries. It is historical source traceability, not the
current execution report. Main now includes both implementation histories and
subsequent portable and Linux repairs. The published Linux source
`f611a02a89c8154d93d424a715dba9716aed23fd` passed 1,203 tests with four skips;
see [current Linux evidence and remaining boundaries](linux-qualification-2026-10-20.md).
Actual ordinary Chat, production ingress, host reboot/power loss and production
capacity remain separate qualification. PR creation and UI work are excluded.

## Modes and authorization

- Manager/worker mode explicitly uses `gpt-6-luna`, with `high` by default and
  `xhigh` or `max` permitted. Starts, continuations, direct jobs, reviews, Pro
  new-worker dispatch and nested handoffs share this policy. Availability/quota
  failures do not authorize another family or an older Luna.
- Author Mode imports its independent service/store and accepts actual change
  packages. It never delegates authoring or recovery to Codex. Source creation,
  context, packages, drafts, storage and read-only interfaces can be checked on
  macOS. Its Linux/WSL2 execution guard applies to both submit and dispatch,
  including automatically queued checks.
- The later October 20 instruction authorized Cloud Jack Linux verification and
  direct main publication. That work is complete for the recorded local matrix.
  Account-dependent Pro/ChatGPT writes and real model trials remain separate
  from deterministic tests; no VM reboot or power-loss qualification is claimed.

## Public audit disposition

The IDs below refer to the supplied audit, not to newly discovered findings.
Most exact-content repairs arrived in the critical-repair ancestor; subsequent
changes repair the consolidated runtime and remaining surface/recovery paths.

| Finding | Source and regression coverage |
| --- | --- |
| W1, W2, W4, W5: exact integration, current acceptance and cleanup | `workers/integration_state.py`, `runtime_integration.py`; Git tree/blob/mode snapshots, content-bound tokens and cleanup gates. `test_audit_regressions_20261019.py`, `test_worker_integration.py`, `test_worker_projection_v2.py`. Binary files, quoted/Unicode names, executable and committed changes are real Git fixtures. |
| W3: read-only authority | `repo_locks.py`, worker runtime; explicit read-only options cannot inherit dangerous write authority. Audit regressions check sandbox and locking. |
| W6: blocked TERM/INT | Standalone `jobs/process_supervisor.py` restores the executable supervisor contract and unblocks child signals. Process-supervisor and audit suites exercise real local processes. Darwin tracking stays separate from Linux subreaper behavior. |
| W7: per-event cost | Descendant discovery uses its existing cache; ordinary telemetry writes coalesce, while checkpoints/session/terminal updates remain durable. The completion regression streams 1,000 events and checks durable terminal data. This is not a fleet throughput benchmark. |
| W8: unbounded session line | `jobs/session_terminal.py` caps each read and buffered partial record, drains oversized records, then resynchronizes on newline. Completion regression uses a 12 MiB partial record followed by a real terminal event. |
| W9, surface S4/S5: source pages and read cost | Lossless cursors refuse unresumable partial lines; bounded physical-line draining prevents giant-line allocation; bounded samples replace whole-file sample reads; workspace digest/count results cache by filesystem identity and modification metadata. Tests include cap notices, source coordinates and measured allocation for a 12 MB line. Deep line pagination still requires streaming to the requested line; no constant-time index is claimed. |
| W10: artifact label | Handler translation preserves the canonical inbox label. Worker inbox/runtime regressions cover artifact delivery. |
| W11: timestamps | Public job schemas accept nullable lifecycle timestamps; protocol and output-schema suites cover ordinary job status. |
| Surface S1: blocked stdio scheduling | `stdio.py` moves blocking iterator advancement off the event loop. A regression proves scheduled work runs while input is blocked. |
| Surface S2/S3: ownership | Low-level cancellation respects coordination ownership; the owner namespace persists privately across restarts and fails closed on corrupt identity. Existing ownership suites plus cross-process namespace regression apply. This remains coordination protection, not a new authentication scheme. |
| Surface S6: complete Pro dispatch | Private canonical response is delivered completely; an alternate worker message is validated before storage and is no longer silently clipped. Audit and final-boundary tests check complete content. |
| Surface S7: repository mirror | Mirror writes share the repository mutation lock; receipts distinguish disabled/written/deferred effects. Mirror text is sanitized while canonical private content stays exact. Mirror leaf symlinks are refused. Busy optional mirroring preserves private evidence and reports deferral; it does not promise automatic replay. |
| Surface S8/S9/S10: command/search boundary | Safe shell checks the original multiline text; ripgrep separates options from its literal query; Python search checks resolved containment, including file symlinks. Completion regressions exercise each path. |
| Surface S11: one-shot CLI dispatch | CLI awaits its owned accepted executor tasks before its event loop exits. It does not claim detached background service ownership. |
| Surface S12/S13/S15: launch configuration | Omitted flags preserve configured roots/settings, tunnel executable resolution uses the resolved fallback path, and revealed local/custom connection output applies to real starts. Launcher/tunnel/CLI tests cover these paths without opening a public tunnel. |
| Surface S14: fleet shutdown | Owned cancellations start concurrently, with process cleanup/locks retained. Launcher allows the server's cleanup budget. Process cancellation tests cover ownership and retained barriers. |
| Surface S16: tool card | Stop confirmation/refusal is not rendered as successful stop. Resource tests cover packaged cards. |
| Surface S17, R1/R2: Pro concurrency/projection | Cross-process mutation exclusion, durable shared revision, complete queue projection and truthful tombstones. Audit, Pro store, Edge and adapter suites cover these mechanisms. |
| H1/H2/H3: enrollment/checkout/generation | Enrollment batch commits code consumption and credentials atomically after validation; independent physical checkouts retain distinct projections; routing rejects old generations. Injected transaction failure and replacement-generation tests apply. Lost successful enrollment acknowledgement requires fresh enrollment, never recovery of a plaintext token from a public identifier. |
| H4: false integrated closure | Ordinary integrated disposition requires authoritative delivery evidence. Explicit preservation/discard outcomes retain their own semantics. Completion regression checks rejection without delivery. |
| H5: unrelated history scan | Hub schema 4 adds exact-contract indexes for operation targets and worker group ownership. Migration retains backup-marker refusal. Query-plan, backup, doctor and production old-schema restart tests apply. |
| Hub S1: historical Pro read rollback | `adapters/pro_requests.py` keeps canonical revision refresh monotonic; old operation results cannot overwrite a newer projection. Existing adapter/audit tests cover historical readback. |
| Hub S2: accepted message replay | Immutable idempotency lookup precedes admission of a new active turn, while broker semantic-payload validation remains authoritative. Worker adapter suite covers replay. |
| Hub S3: legacy group close | A completed start command does not prove a worker stopped. Active/uncertain lanes block normal V1 close; explicit forced close preserves active-lane facts. |
| C1: stop gate | Stop bypasses start-style preflight readiness while retaining route/owner/generation checks. |
| C2/C3: polling and filtered completion | Group-filtered indexed projections, active operation hydration, unchanged-store snapshot reuse, unfiltered completion facts and additive revision tokens. Final-boundary tests cover idle polling, retained history, filtered guidance and changes across Edge revisions. SQL compact status aggregates still depend on the selected group's own history, not unrelated fleet history. |
| C4: secondary projection interruption | Primary projection journals the accepted bounded Pro/base metadata in an outbox; secondary work drains before a newer revision is admitted. Restart fault injection checks exact metadata replay without private report bodies. |
| C5: reassignment context | Successor contains an actual bounded handoff of up to 50 worker report metadata/summary entries with predecessor identities. Full reports, artifacts and diffs require explicit retrieval/review. `reports_and_changes` does not transfer files; both result and tool description say so. Automatic cross-machine artifact migration is not implemented or claimed. |
| C6/C7/C8/C9: contracts and accounting | Doctor uses the runtime's exact schema contract; Pro mutation descriptors require the runtime revision precondition; group-targeted Pro mutations persist group association; batch lanes validate against declared group lanes. Hub contract, store and adapter suites apply. |

GitPython is pinned consistently to 3.1.62 in both manifests, matching the
maintainer release and the existing dependency PR's version. This is a direct
manifest edit; no dependency branch is merged. The installed environment and
full regression run must use that exact pin. The updated dependency is now on
main. Current GitHub advisory status is a separate provider observation and is
not inferred from this source map. This suite is not a blanket advisory clearance
for every dependency or optional component.

## Author plan A00–A15 source map

Proposed file names in the backlog were ownership suggestions; the implemented
modules combine some of those responsibilities. No empty compatibility modules
are required just to match the proposed layout.

| Backlog | Implementation and evidence family | Qualification remaining |
| --- | --- | --- |
| A00 architecture/baseline | `docs/architecture`, independent `authoring/AGENTS.md`, workflow docs, import-boundary tests | Exact commit/environment receipt for each qualification run |
| A01 contracts/policy | `models.py`, package schema/validation, strict JSON, receipts | Execution-owned receipt scenarios on Linux |
| A02 bootstrap/store | `service.py`, persistence modules, lazy CLI, HTTP factory | Linux execution restart; real upgrade/rollback operational rehearsal |
| A03 contexts/builder | repositories, snapshots, package builder/diff | Large real-project context selection remains an operator judgment |
| A04 ingestion | archive limits, traversal/collision/link rejection, hash/operation validation, native downloader | Actual client file route is optional and unqualified |
| A05 revisions | leases, materializer, Git objects, CAS and semantic idempotency | Broader crash/host resource matrix on Linux |
| A06 checks | admission, process identity, supervisor, log spooling, Docker/trusted-local backends | Full Linux process tree, offline Docker and >3-minute run |
| A07 feedback | execution receipts/reports, feedback/diff/continuation export | Real failing/corrected canonical execution loop on Linux |
| A08 CLI loop | same service and artifact core; fixture correction tests | Linux canonical failure→correction→pass loop |
| A09 recovery | operation journal, writer epochs, candidate pin recovery, fail-closed unknown ownership | Execution interruption/cleanup recovery on Linux |
| A10 publication | content-bound approval, exact feature commit, lost push/PR response reconciliation, exact-SHA CI | Linux check-dependent local bare-remote fixtures; external GitHub workflow remains separately authorized |
| A11 UI/read-only bridge | packaged task UI, HTTP auth/import, read-only MCP purity | Real keyboard/mobile/rendered UI acceptance and live account transport are not proved by HTTP tests |
| A12 concurrency/integration | isolated candidates, bounded admission/storage/logs, combined candidate requiring fresh checks | Linux execution concurrency/soak and measured fleet resource qualification |
| A13 optional writes/files | native file schema, explicit capability record, leased versioned drafts | Ordinary Pro account/model/client route must be observed explicitly; no synthetic qualification |
| A14 coexistence | independent import graph/state and full legacy regression suite | Final Linux coexistence and release fixture |
| A15 install/rollout | wheel resources/entrypoints, independent init, operator docs and TESTING matrix | Linux acceptance before release/main merge; no deployment performed |

The 54 C/S/E/P/X scenarios in the supplied plan are acceptance obligations,
not 54 independent success claims. Portable source/package/contract cases run on
macOS; Linux-dependent tests are marked explicitly. Optional long/Docker tests
must be enabled in the Linux phase. Uncovered operational acceptance is listed
above rather than inferred from the presence of implementation or test files.

## Reproducing Linux checks

The initial handoff has been executed for the recorded Linux matrix. Use a
Linux checkout of the published main candidate and follow
`TESTING.md`. Verify both required ancestors and a clean checkout. Run the full
suite, compile pass, deterministic MCP/Hub evaluators, production restart and
old-schema rehearsal, installed entrypoints and independent Author init.

Then explicitly enable the long check and the strict Docker fixture:

```bash
PATCHBAY_AUTHOR_LONG_TESTS=1 python -m pytest tests/authoring/test_execution.py -q
# Use an explicitly prepared local Linux image with python3 available.
PATCHBAY_AUTHOR_DOCKER_IMAGE=YOUR_PREPARED_IMAGE python -m pytest \
  tests/authoring/test_security_contracts.py -q
```

The image must already exist; the service does not pull it implicitly. Preserve
separate setup/network permissions and exact image identity. Keep trusted-local
qualification separate from hostile-source isolation. Review every remaining
skip and the execution receipts, not just the pytest exit code. No production
repository, default branch, model account, tunnel or deployment is part of these
disposable local fixtures.
