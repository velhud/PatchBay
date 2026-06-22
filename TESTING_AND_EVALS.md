# Testing And Evals Plan

## Baseline Checks

Run before and after major changes:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

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
- Wrapper `PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q`: passed, 148 tests at the time this section was updated.
- Wrapper `python scripts/live_mcp_eval.py --json`: passed against a disposable local repo with no ChatGPT and no public tunnel.
- Codex CLI `0.141.0`: verified locally.
- Real `codex_plan_job` through the MCP server: passed against a disposable repo.
- Current Codex JSONL `item.completed` / `agent_message` result parsing: passed.
- CodexPro `npm_config_cache=/tmp/codexpro-npm-cache npm ci`: passed, 0 vulnerabilities.
- CodexPro `npm run build`: passed.
- CodexPro `npm run smoke`: passed all upstream smoke checks.
- CodexPro `npm audit --package-lock-only --json`: passed, 0 vulnerabilities.

These checks prove both source trees were analyzable before migration and that the current wrapper implementation has a live MCP regression path for the ChatGPT-facing surface. They do not yet prove real ChatGPT Developer Mode, public tunnel, apply-job, or resume workflows.

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

- plan job is read-only;
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
