# Testing And Evals Plan

## Baseline Checks

Install runtime and test dependencies before running these checks:

```bash
pip install -r requirements.txt -e ".[test]"
```

Run before and after major changes:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase2_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json
PYTHONDONTWRITEBYTECODE=1 python scripts/external_chatgpt_style_validation.py --json
```

## Worker Bridge Gates

PatchBay includes durable named workers with default isolated writing worktrees, bounded peer-worker context for natural multi-worker coordination, local artifact inbox transfer for ChatGPT-generated files/zips, and explicit accepted-result integration. The worker release gates are tracked in [../worker-bridge/08_TESTING_AND_RELEASE.md](../worker-bridge/08_TESTING_AND_RELEASE.md).

Worker bridge verification must distinguish targeted unit tests, live local MCP regression, real-Codex read-only continuity, real-Codex isolated writing continuity, integration preview/apply, real ChatGPT Developer Mode, and public tunnel coverage. The current worker surface includes real `codex_worker_*` descriptors, handlers, state behavior, external worker worktrees, peer-context relay, accepted-result integration, and eval coverage.

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

- PatchBay `PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests`: passed.
- PatchBay `PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q`: passed, 281 tests at the time this section was updated.
- PatchBay `python scripts/live_mcp_eval.py --json`: passed against a disposable local repo with no ChatGPT and no public tunnel.
- Codex CLI: current local validation recorded `0.142.2`.
- Real read-only worker continuity eval `scripts/worker_phase1_eval.py --timeout 600`: passed.
- Real isolated writing worker continuity eval `scripts/worker_phase2_eval.py --timeout 900`: passed.
- Real multi-worker peer-context eval `scripts/worker_phase3_eval.py --timeout 900`: passed.
- Real worker integration eval `scripts/worker_phase4_eval.py --timeout 900`: passed.
- Real MCP worker lifecycle trial `scripts/real_mcp_worker_trial.py`: passed and wrote progressive `calls.jsonl`, `results.json`, and `summary.md`.
- Real MCP worker negative-case trial `scripts/real_mcp_worker_trial.py --include-safety-cases`: passed for active/read-only/dirty-base/blocked-path/binary/conflict/cleanup negative cases, connector/OAuth stderr noise scan, and public artifact leak scan.
- Direct multi-client MCP trial `scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json`: passed for two logical MCP sessions, session-local tool modes, shared worker inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, preview-before-integrate, no automatic commit, and sanitized private evidence.
- Real worker validation configs run Codex worker subprocesses with `--ignore-user-config`, preserving `CODEX_HOME` auth while suppressing unrelated user-level MCP connector config in trial workers.
- External ChatGPT-style direct MCP validation `scripts/external_chatgpt_style_validation.py --skip-baseline --skip-public-tunnel --json`: passed for setup/token behavior, worker-mode and full-power tool surfaces, artifact import/zip/rejection, artifact-to-worker use, handoff, session discovery, repo mutation locking, single-worker integrate, restart continuation with explicit takeover, multi-worker collaboration, low-level plan/apply job, and resume/interactive continuation.
- External validation baseline `scripts/external_chatgpt_style_validation.py --skip-heavy-codex --skip-public-tunnel --json`: passed for Codex CLI `0.142.2`, compileall, full pytest, live MCP eval, connector setup, descriptors, artifacts, handoff, session discovery, and repo locks.
- External validation public-tunnel gate `scripts/external_chatgpt_style_validation.py --skip-baseline --skip-heavy-codex --json`: blocked because `ngrok config check` passed but no `PATCHBAY_VALIDATION_NGROK_HOSTNAME` or `--ngrok-hostname` was provided. This is an external tunnel setup blocker, not a PatchBay MCP failure.
- Current onboarding/transport closeout run on 2026-06-27: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.12 scripts/external_chatgpt_style_validation.py --skip-baseline --skip-heavy-codex --json` returned `passed_with_blockers` under `.local/validation/external_chatgpt_style/20260627T191503Z`; light direct-MCP setup/tool/artifact/session/handoff/lock/runtime descriptor scenarios passed, heavy Codex scenarios were intentionally skipped, public ngrok simulation was blocked only by missing validation hostname, and real ChatGPT Developer Mode remained a manual UI gate.
- Earlier tokenized public-tunnel MCP simulator coverage through ngrok covered health, initialize, worker-mode `tools/list`, Apps-style file-parameter metadata, artifact inbox import/list/inspect, repeated import, `file://` rejection, isolated worker artifact attachment/read, artifact-context integration exclusion, clean base preservation, and cleanup. Re-run with a configured hostname before using that as current release evidence. Real ChatGPT Developer Mode UI/tool-selection remains blocked in this session.
- Guided setup output: `patchbay start --print-only` now prints a ChatGPT setup guide and `--json` includes `setup_guide`; focused launcher tests verify redacted tokens and Developer Mode/profile guidance.
- Onboarding/transport coverage: tests verify installable CLI dispatch, noninteractive `patchbay setup` failure behavior, settings profile set/list/show/delete, stdio initialize/tools/list/tools/call/resources/list, explicit tunnel binary version checks, and Cloudflare release asset mapping.
- Compatibility alias schemas: `tools/list` now exposes precise CodexPro-derived alias input schemas, validates aliases before translation, and the live MCP eval exercises alias `read` plus path-scoped `show_changes`.
- Runtime descriptor truthfulness: disabled direct-write, bash, and transcript-read profiles hide the corresponding canonical tools and compatibility aliases, while the checked-in full-power profile still exposes 66 tools in live MCP eval.
- Codex session discovery: `codex_list_sessions` now merges PatchBay-known job sessions with configured Codex-home session metadata, dedupes by session id, supports bounded metadata query, and does not return transcripts, repo paths, or source paths.
- Real `codex_plan_job` through the MCP server: passed against a disposable repo.
- Current Codex JSONL `item.completed` / `agent_message` result parsing: passed.
- CodexPro `npm_config_cache=/tmp/codexpro-npm-cache npm ci`: passed, 0 vulnerabilities.
- CodexPro `npm run build`: passed.
- CodexPro `npm run smoke`: passed all upstream smoke checks.
- CodexPro `npm audit --package-lock-only --json`: passed, 0 vulnerabilities.

These checks prove both source trees were analyzable before migration, that the current PatchBay implementation has a live MCP regression path for the ChatGPT-facing surface, and that tokenized public-tunnel MCP reachability works for the artifact inbox worker path. They do not yet prove real ChatGPT Developer Mode UI setup, natural tool selection, ChatGPT-hosted file-parameter import from the actual UI, ChatGPT-originated apply-job, or ChatGPT-originated resume workflows.

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
- worker-mode descriptor filtering;
- artifact inbox descriptor metadata, import/list/inspect behavior, repeated imports, archive containment, worker materialization, and integration exclusion.
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
- resource list and rich v2 tool-card resource;
- unauthenticated request behavior;
- authenticated request behavior;
- missing/invalid token errors;
- representative tool calls and structured outputs.

Probe both:

- current PatchBay;
- CodexPro source checkout;
- hybrid PatchBay after each migration phase.

## ChatGPT Workflow Scenarios

Use disposable repositories only.

### Scenario 1: Workspace Orientation

1. Connect ChatGPT Developer Mode to local PatchBay.
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
3. Restart/reconstruct PatchBay runtime.
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
7. Restart/reconstruct PatchBay runtime.
8. Continue the same worker by name and verify the same worktree/session are reused.

Acceptance:

- base checkout remains unchanged;
- worker worktree is external to the repo;
- same Codex session and worktree are reused after restart;
- public output omits private paths, backend job ids, branch names, and session ids;
- cleanup is explicit.

Status: targeted unit tests and `scripts/worker_phase2_eval.py` coverage exist; real ChatGPT status must be recorded per integration run.

### Scenario 3C: ChatGPT Artifact Inbox To Worker

1. ChatGPT creates a small text file or zip package.
2. Call `codex_worker_inbox(action="import_file")`.
3. Repeat import with a second artifact.
4. Start or continue an isolated worker with `context_from_artifacts`.
5. Verify the worker can read `.ai-bridge/imported-artifacts/ARTIFACTS.md`.
6. Inspect worker changes and integration preview.

Acceptance:

- imports do not edit the base checkout;
- multiple artifact ids can exist for the same workspace;
- sensitive-looking filenames are allowed as artifact contents;
- archive traversal/link escape attempts are rejected;
- `.ai-bridge/imported-artifacts/**` does not appear in worker changed files, diffs, or applied patches;
- public outputs omit raw download URLs and local artifact storage paths.

Status: targeted unit tests and direct tokenized public-tunnel MCP simulation exist; real ChatGPT file-parameter import from the actual UI must still be recorded during Developer Mode validation.

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

The checked-in profile is intentionally full-power, while narrower profiles can
disable direct write, bash, and transcript reads:

1. Run the full-power profile in a disposable repo and confirm descriptors mark the tools as mutating/open-world where appropriate.
2. Run a disabled profile and confirm direct write, edit, bash, session-read tools, and their aliases are absent from `tools/list`.
3. Attempt blocked paths and unsafe commands.

Acceptance:

- full-power config exposes power tools;
- disabled profile hides power tools and aliases;
- blocked paths fail;
- unsafe commands fail;
- returned diffs are clear.

Status: automated disabled-profile descriptor tests and enabled full-power tests exist; real ChatGPT power-mode eval remains pending.


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

### Scenario 9: Shared Server Multi-Client Coordination

1. Start one PatchBay server in `worker` tool mode against a disposable repo.
2. Connect two logical MCP clients with separate MCP sessions.
3. Verify one session can switch tool mode without changing the other session's catalog.
4. Start a worker from client A and inspect it from client B.
5. Verify client B mutation is refused without `takeover: true`.
6. Retry with explicit takeover, wait for the turn, and verify ownership flags flip.
7. Preview and integrate only from the current owner.

Acceptance:

- raw MCP session ids are not returned;
- ownership flags are session-relative;
- cross-owner mutation requires explicit takeover;
- base checkout remains clean until integration;
- integration does not commit;
- private evidence is sanitized.

Status: direct MCP coverage exists through `scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json`; real ChatGPT multi-chat UI behavior remains a manual release eval.

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
2. start PatchBay in local authenticated mode;
3. probe MCP initialize and tools;
4. run context tools;
5. run plan job;
6. run apply job;
7. inspect diff;
8. attempt blocked reads/writes;
9. verify logs and artifacts;
10. connect real ChatGPT Developer Mode through the local/tunnel endpoint;
11. run the direct authenticated public tunnel MCP simulator, and separately record a real ChatGPT-originated tunnel eval when the UI is available;
12. run the direct multi-client MCP trial against a disposable repo;
13. run a real resume/interactive continuation eval;
14. stop server and confirm no child processes remain.

The eval should produce a short report suitable for release notes.

Current implementation provides this as `scripts/live_mcp_eval.py`. It starts the real launcher/server in a temporary workspace, probes MCP initialize/tools/resources/context/skills, verifies blocked `.env` and symlink reads, confirms full-power direct write and command execution, and emits a compact JSON report.

`scripts/live_mcp_eval.py` is necessary but not sufficient for public release because it intentionally does not attach to ChatGPT and does not open a real public tunnel.


## Worker Integration Eval

Run after worker integration changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
```

This proves that a real isolated writing worker result can be previewed, explicitly applied to the base checkout, and preserved in the worker worktree without exposing private paths.
