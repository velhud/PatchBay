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
git checkout chatgpt/patchbay-unified-reconciliation-20261020
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
```

Exercise installed entrypoints and independent state:

```bash
patchbay --help
patchbay doctor
patchbay doctor --json
patchbay author --help
patchbay-author --help
python -m patchbay.authoring.interfaces.cli --help

AUTHOR_HOME="$PWD/.patchbay-author-verify"
rm -rf "$AUTHOR_HOME"
patchbay author --home "$AUTHOR_HOME" init
patchbay-author --home "$AUTHOR_HOME" doctor
patchbay-author --home "$AUTHOR_HOME" token --role reader
```

Also verify that importing `patchbay.authoring.interfaces.cli` does not import `patchbay.workers.runtime` or initialize Codex workers.

A macOS run that stops at the explicit Linux/WSL2 guard is a platform qualification result, not proof of five independent defects. Do not remove the guard to make that run pass.

## 6. Deterministic local evaluators

These evaluators use disposable local state and do not require ChatGPT to select tools naturally:

```bash
python scripts/live_mcp_eval.py --json
python scripts/live_mcp_eval.py --json --exercise-terminal-reconciliation
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```

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

Do not merge the consolidation PR while required checks remain unexplained or while the branch contains uncommitted local corrections.
