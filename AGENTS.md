# AGENTS.md

## Project Purpose

This repository provides a hybrid ChatGPT-to-local-Codex bridge.

The product exposes a local Streamable HTTP MCP server so ChatGPT web/Pro or another MCP-compatible client can:

- inspect allowed local workspaces through bounded context tools;
- manage durable named Codex workers through natural-language briefs, reports, isolated worktrees, and bounded peer-worker context;
- delegate larger work to local Codex CLI jobs;
- inspect async job status, structured results, session refs, worker reports, peer-worker context, and worktree diffs;
- use `.ai-bridge` handoff artifacts;
- optionally use direct edit, bash, or transcript-read power tools when explicitly enabled.

The repo still supports local maintainer workflows, but do not describe the app as only a maintainer wrapper. The public identity is now the broader ChatGPT-to-local-Codex platform.

## Rules For Agents

- Do not add secrets, tokens, local paths, or private machine identifiers.
- Do not remove security checks without explaining why.
- Prefer small, reviewable changes.
- Keep read-only behavior as the default.
- Preserve local control and localhost-first defaults.
- Do not introduce network exposure without authentication.
- Do not enable dangerous bypass in public examples.
- Do not log prompts, secrets, auth files, or full Codex outputs by default.
- Mutating tools must be clearly marked as mutating.
- Add or update tests for path validation, job lifecycle behavior, worker coordination behavior, and unsafe input handling.
- Update README, examples, and tests when changing public tool names, CLI arguments, server behavior, or MCP schemas.
- Update `README.md`, `QUICKSTART.md`, `CHATGPT_INSTRUCTIONS.md`, `PUBLIC_TOOL_SURFACE.md`, `SECURITY_PRODUCT_BOUNDARY.md`, and `TESTING.md` when changing connector behavior, auth/tunnel behavior, tool metadata, power modes, Codex CLI assumptions, or result parsing.
- Treat MCP `initialize.instructions`, public tool descriptions, tool annotations, and `--tool-mode worker` behavior as ChatGPT-facing prompt surface. Keep these instructions outcome-first, concise, stateful-worker-aware, and explicit about side effects, validation, and stop/blocked behavior.
- For first real ChatGPT validation, prefer `--tool-mode worker` so ChatGPT sees the natural-language worker surface plus required read-only context tools, not the full power-user catalog. Do not switch docs back to full mode as the default ChatGPT test path unless real ChatGPT tool-selection evidence supports it.
- Preserve CodexPro attribution in `NOTICE` and README whenever code, product patterns, docs, or tests derived from CodexPro remain in the repository.
- Do not claim public release readiness until real ChatGPT Developer Mode, public tunnel auth, apply-job, and resume scenarios have been verified on disposable repos.

## Review Priorities

When reviewing changes, prioritize:

1. unsafe expansion of repository scope;
2. public network exposure or tunnel token leakage;
3. CORS or authentication weakening;
4. dangerous bypass support;
5. hidden tool exposure;
6. prompt, config, token, or environment leakage;
7. write tools incorrectly marked as read-only;
8. unvalidated paths or config overrides;
9. worktree cleanup and diff correctness;
10. ChatGPT-facing instructions that omit statefulness, preview-before-integrate, no-commit behavior, validation expectations, or worker-first tool-selection guidance;
11. stale documentation that describes the app as only a maintainer wrapper;
12. documentation that overstates safety, verified coverage, or production readiness;
13. missing CodexPro attribution.

## Required Checks

Run these before proposing a change:

```bash
python -m compileall .
python -m pytest tests -q
```

If tests are not yet available, add minimal tests for the changed behavior.

For connector or ChatGPT-facing changes, also run:

```bash
python scripts/live_mcp_eval.py --json
```

For Codex CLI execution changes, record the current `codex --version` in the verification notes.

## Documentation Map

- `README.md`: public entrypoint and current readiness.
- `QUICKSTART.md`: disposable first-run flow.
- `CHATGPT_INSTRUCTIONS.md`: MCP client workflow guidance.
- `ARCHITECTURE.md`: current hybrid architecture.
- `PUBLIC_TOOL_SURFACE.md`: canonical tools, aliases, metadata, mutability, and power modes.
- `CONTEXT_AND_HANDOFF_SPEC.md`: AGENTS, skills, context packs, and `.ai-bridge`.
- `SECURITY.md`: operator-facing security notes and reporting.
- `SECURITY_PRODUCT_BOUNDARY.md`: power-control model.
- `TESTING.md` and `TESTING_AND_EVALS.md`: verification commands and release evals.
- `NOTICE`: CodexPro and other attribution.

## Preferred Workflow

1. Open an issue describing the maintenance change.
2. Create a branch.
3. Add or update tests.
4. Open a PR.
5. Review the diff before merge.

## Literal Whole-Scope Requests
- When the user asks to do something across the whole project, across every file, across every page of a website, across every UI layer, across all cards/components/routes, or uses similar whole-scope wording, treat it as a literal requirement, not emphasis or rhetoric.
- Do not narrow the task to the most recent example, the most visible offender, the current file, or a representative subset unless the user explicitly narrows the scope.
- Before claiming completion, inventory the full requested surface: all relevant files, routes, pages, components, data sources, generated views, locales, and variants that the wording covers.
- Execute and verify against that full inventory. For websites, this means checking every affected public route/page and the shared components that can render the pattern. For code changes, this means searching all relevant files and call sites, not only the initial examples.
- If a literal whole-scope request is too large, risky, or ambiguous, stop and say exactly what scope is covered, what is excluded, and why. Do not silently reduce scope.
- Final reports for whole-scope requests must state the inventory checked and any remaining exclusions or unverified areas.


## Phase 4 worker integration

When changing worker integration behavior, preserve the natural-language management model: preview is read-only, applying a worker result is explicit, the wrapper must not commit automatically, and worker worktrees must remain available until explicit cleanup.

## ChatGPT prompt surface

When changing anything ChatGPT sees through MCP, preserve these prompt-surface rules:

- `initialize.instructions` should tell ChatGPT to start with `codex_self_test` and `codex_open_workspace`.
- ChatGPT should manage workers by human name, not by backend job IDs, session IDs, branch names, or worktree paths.
- Worker mode should explain that default workers use isolated write worktrees, survive wrapper restart when durable state exists, and continue through `codex_worker_message`.
- Integration must be described as preview-first, explicit, no-commit, and preserving the worker worktree.
- Tool descriptions should include when to use the tool, relevant side effects, validation expectations, and safe fallback behavior.
- Setup docs should recommend `--tool-mode worker` for first real ChatGPT validation.
