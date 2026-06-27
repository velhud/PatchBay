# Testing And Evals Plan

## Baseline Checks

Run before and after major changes:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase2_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --output-dir validation-reports/real_mcp_trial
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases --output-dir validation-reports/real_mcp_trial
```

## Worker Bridge Gates

Phase 4 implements durable named workers with default isolated writing worktrees, bounded peer-worker context for natural multi-worker coordination, and explicit accepted-result integration. The worker release gates are tracked in [docs/worker-bridge/08_TESTING_AND_RELEASE.md](docs/worker-bridge/08_TESTING_AND_RELEASE.md).

Worker bridge verification must distinguish targeted unit tests, live local MCP regression, real-Codex read-only continuity, real-Codex isolated writing continuity, integration preview/apply, real ChatGPT Developer Mode, and public tunnel coverage. Phase 4 adds real `codex_worker_*` descriptors, handlers, state behavior, external worker worktrees, peer-context relay, accepted-result integration, and eval coverage.

CodexPro source-material checks:

```bash
npm ci
npm run build
npm run smoke
npm audit --package-lock-only --json
```

These confirm the source material remains buildable if more behavior is ported.

## Current Verification

Verification performed for the current hybrid implementation:

- Wrapper `PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .`: passed.
- Wrapper `PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q`: passed, 196 tests at the time this section was updated.
- Wrapper `python scripts/live_mcp_eval.py --json`: passed against a disposable local repo with no ChatGPT and no public tunnel.
- Codex CLI `0.142.2`: verified locally.
- Real read-only worker continuity eval `scripts/worker_phase1_eval.py --timeout 600`: passed.
- Real isolated writing worker continuity eval `scripts/worker_phase2_eval.py --timeout 900`: passed.
- Real multi-worker peer-context eval `scripts/worker_phase3_eval.py --timeout 900`: passed.
- Real worker integration eval `scripts/worker_phase4_eval.py --timeout 900`: passed.
- Real MCP worker lifecycle trial `scripts/real_mcp_worker_trial.py --output-dir validation-reports/real_mcp_trial`: passed and wrote progressive `calls.jsonl`, `results.json`, and `summary.md`.
- Real MCP worker safety trial `scripts/real_mcp_worker_trial.py --include-safety-cases --output-dir validation-reports/real_mcp_trial`: passed for active/read-only/dirty-base/blocked-path/binary/conflict/cleanup negative cases, connector/OAuth stderr noise scan, and public artifact leak scan.
- Real worker validation configs run Codex worker subprocesses with `--ignore-user-config`, preserving `CODEX_HOME` auth while suppressing unrelated user-level MCP connector config in trial workers.
- Tokenized public-tunnel MCP probe through ngrok: passed for health, initialize, and worker-mode `tools/list` through a query-token URL. Real ChatGPT Developer Mode UI/tool-selection remains blocked in this session.
- Real `codex_plan_job` through the MCP server: passed against a disposable repo.
- Current Codex JSONL `item.completed` / `agent_message` result parsing: passed.
- CodexPro `npm_config_cache=/tmp/codexpro-npm-cache npm ci`: passed, 0 vulnerabilities.
- CodexPro `npm run build`: passed.
- CodexPro `npm run smoke`: passed all upstream smoke checks.
- CodexPro `npm audit --package-lock-only --json`: passed, 0 vulnerabilities.

These checks prove both source trees were analyzable before migration, that the current wrapper implementation has a live MCP regression path for the ChatGPT-facing surface, and that tokenized public-tunnel MCP reachability works at the health/initialize/tools-list level. They do not yet prove real ChatGPT Developer Mode, ChatGPT-originated public-tunnel worker flows, ChatGPT-originated apply-job, or ChatGPT-originated resume workflows.

## Unit Tests

Required test groups:

- tool descriptor snapshot;
- schema validation;
- public/internal argument normalization;
- path guard containment;
- blocked glob matching;
- symlink escape rejection;
- file read caps;
- search output caps and redaction;
- AGENTS chain loading;
- skill inventory and bounded load;
- context pack generation;
- command builder ordering;
- stdin prompt transport for Codex subprocesses;
- env allowlist;
- log redaction;
- artifact retention;
- diff membership validation.
- durable worker identity and name resolution;
- worker continuation through persisted Codex sessions;
- busy-worker rejection without queueing;
- worker output privacy;
- worker-mode descriptor filtering.
- isolated worker worktree creation/reuse/cleanup;
- worker changed-file and one-file diff views;
- worker resume command ordering with `--sandbox` and `--cd` before `resume`.
- peer-worker context relay using reports, changed files, or bounded diffs;
- team-report output from `codex_worker_list`.

## MCP Protocol Smoke Tests

Extend the MCP probe to capture:

- initialize result;
- server instructions;
- `tools/list` descriptors;
- annotations;
- `_meta.securitySchemes`;
- resource list and tool-card resource;
- unauthenticated request behavior;
- authenticated request behavior;
- missing/invalid token errors;
- representative tool calls and structured outputs.

Probe both:

- current wrapper;
- CodexPro source checkout;
- hybrid wrapper after each migration phase.

## ChatGPT Workflow Scenarios

Use disposable repositories only.

### Scenario 1: Workspace Orientation

1. Connect ChatGPT Developer Mode to local wrapper.
2. Call `codex_open_workspace`.
3. Confirm AGENTS summary, branch/status, tree, and tools are understandable.
4. Search and read selected files.
5. Export a context pack.

Acceptance:

- no blocked files returned;
- no local private paths in normal display;
- omitted content is explained.

Status: pending real ChatGPT Developer Mode verification.

### Scenario 2: Delegate To Codex

1. Load context.
2. Start `codex_plan_job`.
3. Poll status.
4. Fetch result.
5. Start `codex_apply_job` in a disposable worktree.
6. Inspect changed files and diffs.

Acceptance:

- plan job uses the configured sandbox and can be narrowed to read-only for restricted evals;
- apply job uses isolated worktree;
- diff API only returns changed-file diffs;
- output is summarized and redacted by default.

Status: `codex_plan_job` verified through local MCP with real Codex CLI; ChatGPT-originated `codex_plan_job` and real `codex_apply_job` remain pending.

### Scenario 3: Resume Prior Work

1. Start a Codex session/job.
2. Continue with `codex_resume` or interactive continuation.
3. Inspect result and status.

Acceptance:

- session metadata persists enough to continue;
- mutability semantics are clear;
- raw transcript exposure is not required.

Status: pending real resume/interactive continuation verification.

### Scenario 3A: Named Read-Only Worker

1. Start `codex_worker_start` with a human name and natural-language brief.
2. Inspect the report with `codex_worker_inspect`.
3. Restart/reconstruct wrapper runtime.
4. List workers and find the same human name.
5. Continue with `codex_worker_message`.

Acceptance:

- ChatGPT does not need low-level job/session ids;
- worker output omits private repo paths, job ids, session ids, raw transcripts, and raw logs;
- busy-worker follow-up returns `accepted: false` instead of silently queueing;
- same Codex session is used for continuation after restart.

Status: targeted unit tests and `scripts/worker_phase1_eval.py` coverage exist; real-Codex and real ChatGPT status must be recorded per integration run.

### Scenario 3B: Named Isolated Writing Worker

1. Start `codex_worker_start` with a human name and no `workspace_mode`.
2. Confirm the worker defaults to `isolated_write`.
3. Ask it to create or revise a small file.
4. Inspect changed files with `codex_worker_inspect(view="changes")`.
5. Inspect worker-side file content with `codex_worker_inspect(view="file", file_path="...")`.
6. Inspect one file's patch with `codex_worker_inspect(view="diff", file_path="...")`.
7. Restart/reconstruct wrapper runtime.
8. Continue the same worker by name and verify the same worktree/session are reused.

Acceptance:

- base checkout remains unchanged;
- worker worktree is external to the repo;
- same Codex session and worktree are reused after restart;
- public output omits private paths, backend job ids, branch names, and session ids;
- cleanup is explicit.

Status: targeted unit tests and `scripts/worker_phase2_eval.py` coverage exist; real ChatGPT status must be recorded per integration run.

### Scenario 4: Handoff

1. ChatGPT writes `.ai-bridge/current-plan.md`.
2. Local CLI dry-runs execution.
3. Local CLI executes against disposable repo.
4. ChatGPT reads handoff status and diff artifacts.

Acceptance:

- MCP write is scoped to `.ai-bridge`;
- local execution is explicit;
- artifacts are capped and redacted.

Status: local handoff commands and `.ai-bridge` tooling are implemented; full ChatGPT handoff eval remains pending.

### Scenario 5: Power Mode

Optional power tools exist and are disabled by default:

1. Enable direct edit or safe bash in disposable repo.
2. Confirm descriptors mark the tools as mutating/open-world where appropriate.
3. Attempt blocked paths and unsafe commands.

Acceptance:

- default config hides power tools;
- blocked paths fail;
- unsafe commands fail;
- returned diffs are clear.

Status: automated default-denial and enabled-mode tests exist; real ChatGPT power-mode eval remains pending.


### Scenario 8: Multi-Worker Coordination

1. Start a writing implementer.
2. Inspect its changed files or diff.
3. Start a read-only reviewer with `context_from_workers` pointing at the implementer and `context_detail="diff"`.
4. Send the reviewer report back to the implementer with `codex_worker_message`.
5. List workers and inspect `team_report`.

Acceptance:

- peer context is bounded and redacted;
- public outputs do not include job ids, session ids, branch names, or private paths;
- the implementer keeps the same session and worktree after receiving reviewer context;
- no message bus, mailbox, or role engine is introduced;
- base checkout remains unchanged.

Status: targeted unit tests and `scripts/worker_phase3_eval.py` coverage exist; real ChatGPT status must be recorded per integration run.

## Failure Mode Tests

Must be automated where possible:

- invalid repo root;
- path outside root;
- `../` traversal;
- symlink outside root;
- `.env` read;
- `.git` read;
- blocked secret glob;
- binary file read;
- too-large file read;
- bash disabled;
- unsafe bash command;
- token missing;
- invalid token;
- tunnel requested without token;
- malformed JSON-RPC;
- batch request behavior;
- long-running job timeout;
- cancelled job;
- malformed Codex JSONL output;
- Codex CLI missing;
- git unavailable;
- worktree cleanup failure.

## Security Regression Tests

Tests should fail if:

- any hidden/internal tool appears in `tools/list`;
- `codex_apply_job` is marked read-only;
- direct edit/bash tools appear when disabled;
- raw config paths or token-like values appear in `codex_get_config`;
- raw prompt text appears in audit logs by default;
- raw stdout/stderr is stored without opt-in;
- query-token connector URLs are logged.

## Release Evals

Before release, run scripted and manual disposable-repo evals:

1. initialize repo with AGENTS, source files, `.env`, symlink escape, and failing tests;
2. start wrapper in local authenticated mode;
3. probe MCP initialize and tools;
4. run context tools;
5. run plan job;
6. run apply job;
7. inspect diff;
8. attempt blocked reads/writes;
9. verify logs and artifacts;
10. connect real ChatGPT Developer Mode through the local/tunnel endpoint;
11. run an authenticated public tunnel eval;
12. run a real resume/interactive continuation eval;
13. stop server and confirm no child processes remain.

The eval should produce a short report suitable for release notes.

Current implementation provides this as `scripts/live_mcp_eval.py`. It starts the real launcher/server in a temporary workspace, probes MCP initialize/tools/resources/context/skills, verifies blocked `.env` and symlink reads, confirms direct writes deny by default, and emits a compact JSON report.

`scripts/live_mcp_eval.py` is necessary but not sufficient for public release because it intentionally does not attach to ChatGPT and does not open a real public tunnel.


## Phase 4 Worker Integration Eval

Run after Phase 4 changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
```

This proves that a real isolated writing worker result can be previewed, explicitly applied to the base checkout, and preserved in the worker worktree without exposing private paths.
