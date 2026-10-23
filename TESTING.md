# Testing the unified PatchBay candidate

Historical test counts from the source branches do not qualify the consolidated source. Run from the repository root on the exact candidate commit.

The [October audit source map](docs/testing/unified-audit-2026-10-20.md) records
the repaired paths and remaining qualification obligations. On macOS, Author
execution tests explicitly skip; their guard is also tested. Those skips do not
qualify Author execution. Include `tests/test_completion_regressions_20261020.py`
and `tests/test_final_audit_boundaries.py` in any focused audit recheck (the full
suite includes them automatically).

## 1. Pin and inspect the candidate

```bash
git fetch origin
git checkout main
git pull --ff-only

git status --short
git rev-parse HEAD
git merge-base --is-ancestor 58bb843baa3fd810c11071a1eed02f6b44cfd82f HEAD
git merge-base --is-ancestor ff6a8d3752775d91fa24f049355b3ab315f5af73 HEAD
```

Both ancestry checks must exit zero. The branch must not contain or activate `ReliableWorkerRuntime` as a replacement for `runtime.WorkerRuntime`.

## 2. Install in an isolated environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e '.[test]'
python --version
codex --version || true
```

Record the Python, operating-system, Git, and Codex versions with the results.

## 3. Compilation and complete tests

```bash
python -m compileall -q src scripts tests
python -m pytest tests -q
```

Do not replace the full suite with historical branch counts.

### macOS process-cleanup qualification

The macOS supervisor uses kqueue fork notifications and later process censuses;
it does not have lossless descendant tracking across a child fork/reap race.
The Linux rapid marker-stripping detached-descendant test skips on macOS and
does not qualify Darwin cleanup. A macOS full-suite pass alone is therefore not
evidence that ordinary Codex worker cleanup is conclusive. Before claiming that
support, run a successful ordinary Codex worker against a disposable repository
and verify that its durable terminal result has proven cleanup, no uncertainty
sentinel remains, and the repository lock is released. Any
`cleanup_unproven` result, including `fork_child_identity_unavailable`, fails
this cleanup gate and must remain fenced; never turn missing child identity
into an absence proof. Record the macOS and Codex CLI versions and the cleanup
proof outcome with the qualification evidence.

The stdout-completion/nonzero-wrapper regression establishes natural wrapper
exit before consuming the fixture's terminal events, and still asserts exit
code 7. This separates that path from deliberate post-terminal termination
(which can produce 143). Adjacent tests independently cover a still-live wrapper
and require the exact durable report before cleanup. No production grace or
cleanup deadline is relaxed by this fixture ordering.

For Linux qualification, enable the real 185-second check with
`PATCHBAY_AUTHOR_LONG_TESTS=1`. Supply `PATCHBAY_AUTHOR_DOCKER_IMAGE` only after
preparing and inspecting an immutable local image. The strict Docker test uses
`--pull=never`; an absent image remains an explicit exclusion. Host networking
does not need to be disabled to run the container isolation regression.

## 4. Critical repair regressions

```bash
python -m pytest tests/test_audit_regressions_20261019.py -q
python -m pytest \
  tests/test_worker_runtime.py::test_worker_file_view_pages_large_text_files \
  -q
python -m pytest \
  tests/test_codex_model_options.py \
  tests/test_unified_reconciliation_contracts.py \
  -q
```

The pagination regression must retain all truncation/cursor assertions and include the response note that the request was capped to 120 bytes.

## 5. Author Mode

Run on Linux or WSL2. The execution guard is part of the Author contract.

```bash
python -m pytest tests/authoring -q
python -m pytest tests/authoring/test_reconciliation_contracts.py -q
python -m pytest tests/authoring/test_drafts_maintenance.py -q
python -m pytest tests/authoring/test_trial_report_contracts.py -q
python -m pytest tests/authoring/test_docker_lifecycle.py \
  tests/authoring/test_operator_resolution.py -q
```

Draft rejection coverage verifies that apply/seal validation errors become
terminal, same-key retries preserve and report the original error, and a
queued operation created by an older runtime is revalidated and terminalized
by an exact normal replay.

Docker lifecycle coverage includes delayed create acknowledgement, bounded
output, check deadlines, cancellation before start and during attachment, and
exact-ID cleanup. Operator-resolution coverage keeps historical unknown
receipts immutable, rejects incomplete/live/mismatched evidence, checks the
deployment identity guard, and verifies local-only idempotent resolution.
Run the actual offline Docker and ordinary-Chat cancellation trials as separate
deployment checks; simulated lifecycle regressions alone do not qualify them.

Exercise installed entrypoints and independent state:

```bash
patchbay --help
patchbay doctor
patchbay doctor --json
patchbay author --help
patchbay-author --help
python -m patchbay.authoring.interfaces.cli --help

AUTHOR_HOME="$PWD/.patchbay-author-verify"
patchbay author --home "$AUTHOR_HOME" init
patchbay-author --home "$AUTHOR_HOME" doctor
patchbay-author --home "$AUTHOR_HOME" token --role reader
```

Also verify that importing `patchbay.authoring.interfaces.cli` does not import `patchbay.workers.runtime` or initialize Codex workers.

A macOS run that stops at the explicit Linux/WSL2 guard is a platform qualification result, not proof of five independent defects. Do not remove the guard to make that run pass.

### Context and response efficiency

The S01 deterministic contract regressions exercise supported and malformed
versions through public protocol/HTTP entrypoints, source-selector alternatives,
safe bounded errors, role/annotation coverage, Unicode/long-line pages, paired
text outcomes and recovery after a disposable admitted effect returns malformed
output or loses its response:

```bash
python -m pytest tests/test_s01_mcp_contracts.py \
  tests/authoring/test_s01_interface_contracts.py -q
```

See the [MCP interface contract](docs/reference/mcp-interface-contract.md) for
the exact residual native-client/deployment qualification recipe. These fixtures
do not run paid workers, qualify Pro, or refresh deployed metadata.

```bash
python -m pytest tests/test_mcp_compact_text_serialization.py \
  tests/authoring/test_context_output_efficiency.py -q
python scripts/author_interface_eval.py --json
```

Check source/error/page metadata round trips, recovery with an unresolved
operation older than its recent-history window, full-history pagination,
strict limits, and absence of instruction reinsertion in routine results.
Measure UTF-8 bytes separately from tokens or model behavior. The Author
compatibility output remains the default. Before enabling
`structured-summary-v2`, observe an ordinary ChatGPT client reading complete
source, logs and errors from `structuredContent`, then record qualification
for that exact source, catalog, role, route and output profile. Changing Author
source or schema invalidates the old write qualification; refresh metadata
and qualify the updated route rather than copying an old receipt.

## 6. Deterministic local evaluators

### S03 observation and admission regressions

```bash
python -m pytest tests/test_s03_monitoring.py tests/test_s03_observation_cost.py \
  tests/test_s03_process_cost.py tests/authoring/test_s03_ingress.py -q
python -m pytest tests/test_job_executor_cancellation.py tests/test_process_supervisor.py \
  tests/test_worker_projection_v2.py tests/test_hub_edge_client_v2.py -q
```

These use actual local runtime, authenticated Author HTTP/MCP and SQLite owners,
with controlled clocks, recording transport, queued check fixtures and a fake
process census. They launch no inference and do not remove the Linux/WSL2 guard.
The [S03 contract](docs/repairs/2026-10-22-s03-observation.md) records real-store
query/snapshot measurements and the remaining 5/10-lane, 20-minute native/client
recipe. Local ASGI latency, virtual silence and process fixtures do not qualify
deployed urgent admission, native cleanup, Pro comprehension or quota savings.

### S02 controller recovery regressions

```bash
python -m pytest tests/test_s02_ingress_contracts.py \
  tests/test_s02_worker_retry.py tests/test_s02_hub_takeover.py \
  tests/test_s02_hub_response.py tests/authoring/test_s02_response_recovery.py \
  tests/authoring/test_s02_history_traversal.py \
  tests/authoring/test_s02_publication_replay.py -q
python -m pytest tests/test_worker_integration_tokens_v2.py \
  tests/authoring/test_principals.py tests/authoring/test_http_principal_contracts.py \
  tests/authoring/test_operator_resolution.py -q
```

The [S02 recovery contract](docs/repairs/2026-10-22-s02-controller-recovery.md)
maps defining owners, crash windows, authority and retention limits. These
fixtures use disposable state and recording/no-inference executors. The local
bare-Git push is real; its check receipt is synthetic unit input. Operator census
fixtures are not native Linux/Docker evidence. Run that native qualification and
the ordinary-client reconnect/write trials separately; local source publication
does not update a connector or qualify a deployed route.

These evaluators use disposable local state and do not require ChatGPT to select tools naturally:

```bash
python scripts/live_mcp_eval.py --json
python scripts/live_mcp_eval.py --json --exercise-terminal-reconciliation
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```

`tests/test_live_hub_v2_eval.py` also delays poison-phase responses across a
projection cadence. It verifies that Hub-visible completion is followed by
the exact Edge-local outbox acknowledgement state before fairness is asserted.
Timeouts still fail with diagnostics; a completed worker alone is not that
acknowledgement proof.

The MCP evaluator supports `--tool-mode worker|standard|full|minimal`, `--timeout`, `--keep-temp`, and `--verbose` for focused diagnosis.

## 7. Production restart and upgrade rehearsal

```bash
python scripts/production_entrypoint_restart_eval.py --json
python scripts/production_entrypoint_restart_eval.py \
  --json \
  --rehearse-old-schema
```

Use `--keep-temp` or an explicit `--fixture-root` when durable evidence is required. The evaluator exercises production Hub/Edge entrypoints, state continuity, bounded shutdown, backup, migration, restart, and restore.

## 8. Optional real-Codex worker trial

This check intentionally starts local Codex workers and depends on a configured account. It is not a deterministic unit-test substitute and was not run during source reconciliation.

```bash
python scripts/real_mcp_worker_trial.py \
  --json \
  --include-safety-cases \
  --multi-client
```

Evidence is written under `.local/validation/real_mcp_trial/` by default. Use `--worker-timeout`, `--request-timeout`, `--keep-temp`, or `--output-dir` as needed.

Before a live run, confirm the exact approved model appears in the deployed
CLI's account-specific catalog (`codex debug models` and
`codex_worker_options`). CLI 0.163.0 has been exercised with `gpt-6-luna`;
successful login and a general self-test do not prove model support.
Inspect process events and exit status as well as the final worker report.
An explicit failed turn must remain failed even if the session also records
generic completion. A successful final report that discusses an error, or an
unclassified wrapper exit after genuine completion, must not be relabeled as a
model failure. The corresponding process/session regressions are in
`tests/test_job_executor_cancellation.py` and `tests/test_session_terminal.py`.

## 9. Result record

Record separately:

- exact commit SHA;
- commands actually executed;
- pass/fail/skip totals;
- platform and dependency versions;
- any account-dependent or external checks not run;
- failures caused by an explicit platform guard;
- unresolved repository defects, with logs or evidence paths;
- whether the installed `patchbay`, `patchbay-stdio`, and `patchbay-author` entrypoints resolve to this checkout.

Record deployment qualification separately from source publication. No PR creation or UI changes are required by this matrix.

## Package completeness and client contracts

Run the portable package regressions and evaluators before deployment:

```bash
python scripts/author_requirements_eval.py --json
python scripts/author_interface_eval.py --json
python -m pip check
```

The [package coverage map](docs/authoring/coverage-map.md) keeps all original requirement identities. The [qualification guide](docs/authoring/qualification.md) defines evidence files and the disposable Linux fault lab. Unrun real-client cases stay unrun even when local contract tests pass.

## Bounded Linux fixtures and ordinary Chat preparation

The loopback HTTP/process regressions exercise endpoint restart, owned execution,
admission blockers, measured control latency, cancellation, and rejected stale
process identities without invoking a model or changing a shared service:

```bash
python -m pytest tests/authoring/test_linux_continuity.py \
  tests/authoring/test_cgroup_capacity.py -q
python scripts/author_storage_lab.py --raw-dir "$EVIDENCE_DIR/storage"
python scripts/prepare_author_chat_trial.py --output-dir "$EVIDENCE_DIR/chat-trial"
```

The opt-in [storage lab](docs/authoring/storage-lab.md) mounts only a private
64 MiB tmpfs with 2,048 inodes. Its default suite includes cheap backup failure
regressions; actual exhaustion requires the lab runner and sudo. It never falls
back to filling a host filesystem. The [Chat trial preparation](docs/authoring/ordinary-chat-trial.md)
creates a disposable task/context and blank observations; it does not execute
ordinary Chat or enable MCP writes. Use a new output directory for each exact
candidate. See the [Linux qualification record](docs/testing/linux-qualification-2026-10-20.md)
for evidence boundaries and the repaired failure classes.

### S04 native evidence and lifecycle regressions

```bash
python -m pytest tests/test_s04_native_evidence.py tests/test_s04_native_profile.py \
  tests/test_s04_process_identity.py tests/test_s04_startup.py \
  tests/test_session_terminal.py tests/test_job_executor_cancellation.py \
  tests/test_process_supervisor.py tests/test_worker_tool_surface.py \
  tests/test_hub_worker_adapter_v2.py -q
```

These use real application persistence/recovery, owned disposable Python
subprocesses and recording-only identity races. They invoke no native model.
The Linux pidfd branches are recorded by mocks on macOS; their pass is not a
Linux execution qualification. A Darwin zombie whose BSD info is unavailable
must be recognized as exited, while unknown live identities remain fenced.

The [S04 contract](docs/repairs/2026-10-22-s04-native-execution.md) specifies the
explicit offline static-manifest helper and its limits. Use a disposable complete
distribution manifest; missing required companions must fail even when the
launcher remains present. Static matching never implies native/tool/client
qualification. After explicit authorization, run the exact native/client recipe
in that contract and section 8, retaining the pinned distribution, source,
profile, typed events, report pages, exit and cleanup evidence separately.
All required existing checks above remain required; S04 does not waive them.

### S05 Author custody and transactional boundaries

```bash
python -m pytest tests/authoring/test_s05_sources.py \
  tests/authoring/test_s05_contexts.py tests/authoring/test_s05_artifacts.py \
  tests/authoring/test_s05_publication.py tests/authoring/test_s05_authority.py \
  tests/authoring/test_s05_dirty.py -q
```

These regressions use real disposable Git/SQLite/MCP/HTTP/helper owners and
controlled read, copy, push-acknowledgement, policy and lease races. Publication
uses local bare remotes only; its synthetic receipt is persistence input, not
backend qualification. Admission-only tests disable dispatch and qualify no
Linux supervisor. The [S05 contract](docs/repairs/2026-10-22-s05-author-custody.md)
describes exact compatibility and trusted-local limits. Keep all full checks,
source/install identity checks and separate native/client gates above.

## S06 candidate and delivery regressions

Run `python -m pytest tests/test_s06_candidate_contracts.py tests/test_s06_delivery_commit.py tests/test_s06_review.py tests/test_s06_group_contract.py tests/authoring/test_s06_composition.py -q` in addition to the full required matrix. These actual Git/SQLite/application fixtures use no-model executors. They cover tracked ignored files, literal exclusions, sparse/gitlink/conversion refusal, pinned review and dependency starts, peer capture counts at 1/10/100 paths, exact postimages, atomic receipt recovery, baseline-anchor failure, safe cleanup and composition decisions. Hub protocol/adapter/group tests retain both permission and legacy retry boundaries.

Revalidate all S01–S05 stage commits, input packages and retained regressions against the final S06 candidate; do not substitute earlier totals. Keep the 72 S06 proposed acceptance oracles distinct from actual tests and the separate 52-case operational requirements register. Native Linux/Docker/client/deployment tests remain unrun unless separately authorized and actually exercised. See [S06 contract and operator handoff](docs/repairs/2026-10-22-s06-candidate-delivery.md).

## S07 evidence and authority regressions

```bash
python -m pytest tests/authoring/test_s07_*.py tests/test_s07_source_views.py -q
```

These parser, fake-CI, real-store, no-model source-view and isolated-command fixtures cover contradictory reports, encoded declarations, opt-in executed counts, incomplete scans, receipt consistency, approval revocation and literal feedback. Synthetic receipt fixtures are persistence inputs, not executed checks. Keep all required full checks and S01–S06 regressions. [S07 contracts and remaining gates](docs/repairs/2026-10-22-s07-evidence-authority.md) distinguish local checks from native/client/deployment qualification.


## S08 release, configuration and restore boundaries

```bash
python -m pytest tests/authoring/test_s08_backup_reference_integrity.py tests/authoring/test_s08_release_restore.py tests/test_s08_config_publication.py tests/test_s08_deployment.py -q
```

Run the full matrix above serially, the dedicated Author suite with the optional
long execution enabled, old-schema restart, and offline wheel/install parity.
The S08 subset covers loaded/disk drift, stale qualification and child-release
refusal; SQL/Git relation and private retention checks; restore file/directory
sync order and publication uncertainty; atomic private launch generations and
child config identity; explicit Origin before session/auth work; shared shutdown
budget and actual Hub WAL/FULL settings. Recording teardown tests do not run a
live tunnel or prove native fleet shutdown. Offline historical restore is not
automated authority activation. See [S08 limits](docs/repairs/2026-10-22-s08-deployment-durability.md)
and the [component custody template](docs/maintainer/deployment-custody.template.json).
Actual unit/cgroup, Docker, reboot/power-loss, native Mac/Luna and ordinary-Chat
qualification remain separate NOT_RUN gates.

## S09 repeated-use regression and measurement boundary

```bash
python -m pytest tests/test_s09_capacity.py tests/authoring/test_s09_repeated_use.py -q
```

Run the full matrix serially, dedicated Author long execution, old-schema
restart and offline wheel/install parity. These cases exercise >100 queued
blockers with equal timestamps and restart, explicit additive indexes, complete
streamed archives and failed producers, no-op GC versus corrupt retained
evidence, bounded binary patch/identity capture, actual expanded-byte limits,
entry/duplicate rejection, honest inbox pages, edit-preserving materialization
and bounded event tails. Recording launch refusal proves dispatch opportunity,
not a native worker launch. Keep historical research's 80 NOT_RUN specifications
separate from these tests. [S09](docs/repairs/2026-10-22-s09-repeated-use.md)
records the measured synthetic history/serialization scope and retained costs;
no workload, native, client or economic qualification follows from the counts.

Session lifecycle fixtures that exercise real owned child cleanup use external
safety guards derived from the existing supervisor/executor proof budgets.
Their synthetic children outlive those guards; completion, exact result, PID
absence and mutation-lock assertions remain mandatory. These tests do not
certify four-second native cleanup under arbitrary host load. Use a unique
owned `--basetemp` for long runs so other sessions' temporary-root rotation
cannot remove diagnostic fixtures; retain failed roots and logs separately.

## Final S10 composed verification

```bash
python -m pytest tests/test_s10_review_provenance.py \
  tests/authoring/test_s10_composition.py tests/test_s10_evidence_scope.py -q
```

Run the required full matrix serially, the dedicated Author suite with its long
local command enabled, old-schema restart and offline wheel/install parity.
S10 adds logging-off UTF-8 brief identity, reload/legacy/empty/invalid metadata,
retained descriptor/ref corruption, immutable old selectors and source/turn
invalidation; accepted revision/auto-check reply loss plus lease takeover and
legitimate progress; cancellation after receipt-blob preparation with/without
policy change; stale-policy evidence; joined JUnit/source-observation/receipt/
feedback/Publisher candidate gates; fresh capacity/receipt/corruption inverses;
and the hash-correct irrelevant-artifact governance negative. Synthetic process
receipts test persistence/gate mechanics and are never native execution proof.

Keep baseline/restored-bug failures separate from passing corrected assertions.
Do not treat structural validator PASS as semantic, installed/loaded or actual
client acceptance. Preserve the original T01–T52 bytes/statuses and keep all
42 research recipes and 28 composed schedules scoped to their actual witnesses.
[Final capability gates](docs/repairs/2026-10-22-s10-final-composition.md) explicitly
leave ordinary-Pro/native/Mac/Docker/deployment/power-loss/soak/economics outside
this local qualification. The original programme stops at S10.

S10 retained an initial disk-fixture full run with five timeouts, rather than
calling a focused retry a full pass. The startup regression now uses the existing
owner-derived proof guard; its inert children outlive that guard. The restart
evaluator gives the follow-up the same bounded 65-second lifecycle allowance as
the initial turn. Production deadlines and all terminal/cleanup/identity
assertions remain unchanged. Duplicate-claim coverage waits for three actual
polls while the first effect is blocked, checks both 20 ms and 80 ms polling,
and always shuts down its owned runner. The observed original failure was a
five-second first-handler timeout; the separate delayed-poll negative control
demonstrates why the old 60 ms sampling step was also inadequate.

Each receipt records its fixture filesystem. A full run with an owned tmpfs
`--basetemp` reduces disk-stall interference for those fixtures; it does not
certify disk latency, physical durability or power-loss behavior. Retain disk
attempts and affected disk/integration results separately. Do not combine
different runs into an invented all-green disk qualification.

## Postimplementation joined regressions

```bash
python -m pytest tests/test_postimplementation_control.py tests/authoring/test_postimplementation.py -q
```

Run the complete matrix above serially, dedicated Author with the long local
check enabled, old-schema restart and offline wheel/install parity. The new
fixtures enter actual Manager MCP/handler/runtime paths with inert execution;
synchronized imports prove control overlap, saturation, slow-drip deadline,
writer-owned cancellation/cleanup and publication/no-clobber boundaries. Wait
fixtures cover actual defaults, per-caller selection and fresh reconnect, new
turns/completion, persistent liveness/cleanup urgency and observer cancellation.
Author parser-to-canonical-receipt/feedback/publication fixtures label synthetic
process evidence; HTTP/MCP qualification fixtures preserve global/scoped
authority, full bindings, migration/replay, corruption, revocation and backup.
The original 26 suggested specifications remain research NOT_RUN records; see
[individual dispositions](docs/testing/postimplementation-disposition.json).

Retain initial failures and corrected fixture assertions separately. No
ordinary-Pro, native macOS, live Luna, Docker, service-unit, deployment, physical
durability, fleet/soak or economic claim follows. The original S01–S10 programme
is complete; this is a focused follow-up, not a new numbered stage.

The recorded follow-up candidate passed 1685 full-suite tests (7 skipped) and
402 dedicated Author tests (4 skipped), including the real 185-second
local check. All required local gates/build/install passed; requirements
evaluation remained INCOMPLETE with all 52 NOT_RUN. Initial failures and
fixture filesystems are retained in the [follow-up record](docs/repairs/2026-10-23-postimplementation.md).

## Live native-client peer-context regression

```bash
python -m pytest tests/test_worker_coordination.py tests/test_s06_review.py tests/test_s10_review_provenance.py -q
```

Shared-write peer context is advisory. Test start and follow-up with `changes`,
`diff`, and `review`, preserving unrelated checkout dirt and read-only recipient
permissions without creating an isolated candidate identity. Keep existing
isolated-source retention and acceptance regressions. Executable verification
needs an explicitly writable disposable lane; a read-only review lane cannot
qualify checks requiring temporary databases or compiler caches. See the
[live native-client record](docs/repairs/2026-10-23-live-native-client.md) for
actual delivery evidence and unresolved browser/model boundaries.
