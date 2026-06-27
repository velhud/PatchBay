# Hybrid Migration Plan

## Goal

Transform `patchbay` into the stronger combined application:

- CodexPro-quality ChatGPT connector and workspace context.
- PatchBay-quality Codex job orchestration, worktree isolation, resume, diff inspection, and auditability.
- Optional power tools without making them the default product identity.

This is not a CodexPro fork. All implementation lands in PatchBay repository.

## Phase 0: Baseline And Documentation

Status: mostly complete; current documentation platform update is the final pre-commit doc pass.

Deliverables:

- root investigation docs;
- current test baseline;
- CodexPro build/smoke/audit baseline;
- project `AGENTS.md` updated when the user explicitly requested documentation guidance changes.

Done criteria:

- `python -m compileall .` passes;
- `python -m pytest tests -q` passes;
- CodexPro `npm ci`, `npm run build`, `npm run smoke`, and `npm audit --package-lock-only --json` pass;
- docs avoid local private paths and secrets.

## Phase 1: Stabilize Existing PatchBay Core

Status: complete for the current pre-release branch.

Fix release blockers before importing large connector features:

- add schema validation for all public tool calls;
- test public schema to internal argument translation;
- preserve tested `codex exec` command builder ordering;
- keep user prompts on stdin instead of argv where the Codex CLI supports `-`;
- enforce read-only plan semantics;
- make `codex_get_diff` apply-job-only and changed-file-only;
- stop raw stdout/stderr logging by default;
- fix `codex_get_config` to return only safe capability/config summaries;
- split public registry from hidden experimental handlers;
- delete legacy cloud/apply-diff/string/sandbox handler bodies in favor of public power tools and isolated jobs.

First PRs:

1. `tool-registry-and-schema-validation`
2. `codex-command-builder-tests-and-stdin-prompts`
3. `safe-config-and-log-redaction`
4. `apply-diff-contract`
5. `remove-hidden-experimental-handlers`

## Phase 2: Connector Auth And ChatGPT Metadata

Status: implemented locally. Direct tokenized public-tunnel MCP simulation has since passed; real ChatGPT Developer Mode UI/tool-selection remains a release gate.

Add the ChatGPT-facing connector foundation:

- bearer-token auth middleware;
- fail-closed policy for non-loopback or tunnel modes;
- request size limits;
- session handling cleanup;
- tool descriptors with annotations and `_meta.securitySchemes`;
- invocation labels;
- optional structured content shape for job/status/diff results.

First PRs:

1. `http-auth-and-session-policy`
2. `chatgpt-tool-descriptor-metadata`
3. `mcp-probe-snapshot-tests`

Public tunnel process supervision exists, but public release still requires a real token-gated tunnel eval.

## Phase 3: Workspace Context Layer

Status: implemented and covered by unit/live local MCP evals.

Port CodexPro context systems into Python:

- active workspace manager;
- path guard with realpath/symlink/blocked glob checks;
- bounded repo tree;
- bounded file reads;
- search;
- git status/diff/log summaries;
- AGENTS chain loading;
- skill inventory and optional skill loading;
- selected-file context packs.

New tools:

- `codex_open_workspace`
- `codex_repo_tree`
- `codex_search_repo`
- `codex_read_file`
- `codex_load_context`
- `codex_export_context`
- `codex_list_skills`
- `codex_load_skill`

First PRs:

1. `workspace-manager-and-path-guard`
2. `repo-tree-read-search`
3. `agents-skills-context-pack`

## Phase 4: Durable Codex Job Engine

Status: implemented for plan/apply/resume/interactive job records, cancellation, redacted artifacts, and current Codex JSONL parsing.

Rebuild the execution core around explicit services:

- durable job store;
- process manager;
- cancellation;
- artifact retention;
- worktree service;
- Codex JSONL event parser;
- async resume and interactive continuation;
- result summaries separate from raw logs.

First PRs:

1. `durable-job-store`
2. `process-manager-and-cancel`
3. `worktree-service-and-artifacts`
4. `resume-interactive-as-jobs`

## Phase 5: Handoff And `.ai-bridge`

Status: implemented locally; pending real ChatGPT handoff eval.

Port CodexPro's handoff model as a controlled local workflow:

- `.ai-bridge` initializer;
- handoff plan write tool;
- local dry-run/execute/watch CLI;
- status and diff readers;
- execution logs with caps/redaction.

New tools:

- `codex_write_handoff`
- `codex_get_handoff_status`
- `codex_get_handoff_diff`

Execution remains a local CLI action unless a future explicit job tool is added.

## Phase 6: Product UX

Status: implemented for CLI doctor/start/profile flow and passive MCP Apps card. Richer standalone UI remains optional future work.

Add user-facing convenience after the core is solid:

- setup wizard or standalone control surface;
- doctor command, implemented as `scripts/doctor.py`;
- ChatGPT Developer Mode connection instructions;
- profile/config store, implemented as the private launcher profile store used by `scripts/start.py`;
- self-test tool, implemented as `codex_self_test`;
- richer tool-card resource, with the first passive card implemented through MCP `resources/list` and `resources/read`;
- optional local control panel.

The tool-card now starts as a simple Python-served `text/html;profile=mcp-app` resource linked from every public tool descriptor. A temporary Node/Vite sidecar is allowed only if future interactive card work requires a frontend build that Python resource serving blocks.

The launcher now starts from a base `config.yaml`, applies a saved per-workspace profile unless disabled, writes a private runtime config under `PATCHBAY_HOME` or the user's home directory, prints connector metadata, and starts `server.py` with `PATCHBAY_CONFIG`. Public URL mode sets tunnel mode and therefore fails closed without `PATCHBAY_HTTP_TOKEN`.

Optional public tunnel process supervision is now implemented for Cloudflare quick tunnels, Cloudflare named tunnels, and ngrok stable hostnames. Tests use a fake tunnel executable; no real public tunnel is opened during automated verification. PatchBay does not auto-install provider binaries.

## Phase 7: Power Modes

Status: implemented as optional server-side power modes, disabled by default, with tests.

Add optional high-power features:

- direct source edit/write;
- safe bash;
- full bash;
- Codex session metadata;
- bounded Codex session transcript reads;
- public tunnel modes, implemented as token-gated launcher-supervised child processes.

Each power feature needs:

- default off;
- explicit config/profile setting;
- descriptor annotations;
- docs warning;
- regression tests;
- redaction and output caps;
- clear tool names.

## Compatibility Strategy

- Keep current `codex_*` tool names stable.
- Keep neutral/compatibility aliases controlled by `app.tool_mode`.
- Advertise aliases only in the tool modes that intentionally expose them, and route them through canonical handlers.
- Version the public tool surface in docs and tests.
- Add migration notes when schemas change.

## Release Criteria

The first hybrid release is ready when:

- ChatGPT can connect through authenticated Streamable HTTP MCP. Pending real ChatGPT eval.
- ChatGPT can open a workspace and load bounded context. Verified locally through MCP probe; pending ChatGPT eval.
- ChatGPT can start plan/apply jobs and inspect status/result/diff. Plan verified locally with real Codex; apply pending real eval.
- Apply jobs use isolated worktrees.
- Raw prompts, tokens, and full outputs are not logged by default.
- Public descriptors pass mutability and schema tests.
- Tunnels fail closed without auth.
- Direct edit/bash/session transcript tools are disabled by default.
- README and examples describe actual behavior without overstating safety.

## Do Not Do

- Do not turn PatchBay into a CodexPro fork.
- Do not expose generic `read/write/edit/bash` as default public tools.
- Do not ship a public tunnel without token/auth enforcement.
- Do not rely on ChatGPT confirmation prompts as the only safety layer.
- Do not log raw prompt bodies or full Codex outputs by default.
- Do not copy CodexPro files without preserving MIT attribution.
