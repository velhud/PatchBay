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
