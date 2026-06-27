# AGENTS.md

## Project Purpose

This repository provides a hybrid ChatGPT-to-local-Codex bridge.

The product exposes a local Streamable HTTP MCP server so ChatGPT web/Pro or another MCP-compatible client can:

- inspect allowed local workspaces through bounded context tools;
- manage durable named Codex workers through natural-language briefs, reports, imported artifact context, isolated worktrees, and bounded peer-worker context;
- delegate larger work to local Codex CLI jobs;
- inspect async job status, structured results, session refs, worker reports, peer-worker context, imported artifact state, and worktree diffs;
- use `.ai-bridge` handoff artifacts;
- coordinate multiple ChatGPT/MCP sessions on one shared local server with session-local tool modes, session-relative ownership flags, explicit takeover for cross-owner mutation, and per-repository mutation locks;
- optionally use direct edit, bash, or transcript-read power tools when explicitly enabled.

The repo still supports local maintainer workflows, but do not describe the app as only a maintainer utility. The public identity is now the broader ChatGPT-to-local-Codex platform.

## Product Self-Knowledge

- Treat PatchBay as a ChatGPT-first local control plane: ChatGPT brings conversation, Projects, memory, generated artifacts, and coordination; local Codex brings the repository, git state, tools, credentials, and execution.
- Start docs/config/behavior changes by checking `README.md`, `QUICKSTART.md`, `docs/project/why-patchbay.md`, `docs/architecture/overview.md`, `docs/reference/public-tool-surface.md`, and `docs/user/chatgpt-instructions.md`.
- Keep the app self-describing enough that a future coding agent can update configuration, docs, tool metadata, and examples from repository context without needing private oral history.
- Do not replace concrete setup steps with vague philosophy. Add the rationale, then keep the exact command, ChatGPT connector step, expected tool result, and verification command.

## Private Campaign Routing

- When the user invokes a campaign workflow, read `.architect/README.md` and `.architect/indexes/active_campaigns.md` before acting.
- Treat exactly one active campaign as operational truth. Completed campaigns are reference evidence only unless the user explicitly reopens them.
- The onboarding/transport campaign is complete. Treat `.architect/campaigns/patchbay-onboarding-transport-2026-06-27/` and its repo-local skills as reference unless the user explicitly starts a new continuation campaign.

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
- Update `README.md`, `QUICKSTART.md`, `docs/user/chatgpt-instructions.md`, `docs/reference/public-tool-surface.md`, `docs/security/product-boundary.md`, and `TESTING.md` when changing connector behavior, auth/tunnel behavior, tool metadata, power modes, Codex CLI assumptions, or result parsing.
- Treat MCP `initialize.instructions`, public tool descriptions, tool annotations, and `--tool-mode worker` behavior as ChatGPT-facing prompt surface. Keep these instructions outcome-first, concise, stateful-worker-aware, and explicit about side effects, validation, and stop/blocked behavior.
- Keep shared-server coordination visible in ChatGPT-facing docs and descriptors: one Server URL shares worker/job/artifact/repo state across connected clients; reads may be shared; cross-owner mutation requires explicit `takeover: true`; base-checkout contention should return `repo_busy`.
- When documenting multi-repository runs, state that `--root` sets the default workspace and narrows `repositories.allowed` unless every extra repository is passed with `--allow-root` or configured explicitly.
- For first real ChatGPT validation, prefer `--tool-mode worker` so ChatGPT sees the natural-language worker surface plus required read-only context tools, not the full power-user catalog. Do not switch docs back to full mode as the default ChatGPT test path unless real ChatGPT tool-selection evidence supports it.
- Preserve CodexPro attribution in `NOTICE` and README whenever code, product patterns, docs, or tests derived from CodexPro remain in the repository.
- Do not claim public release readiness until real ChatGPT Developer Mode natural tool selection, ChatGPT-originated public-tunnel worker flow when advertised, apply-job, and resume scenarios have been verified on disposable repos.

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
11. shared-server instructions that omit session-local tool modes, explicit takeover, `repo_busy`, or multi-repository `--allow-root` setup;
12. stale documentation that describes the app as only a maintainer utility;
13. documentation that overstates safety, verified coverage, or production readiness;
14. missing CodexPro attribution.

## Required Checks

Run these before proposing a change:

```bash
python -m compileall src scripts tests
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
- `docs/README.md`: full public documentation index.
- `QUICKSTART.md`: disposable first-run flow.
- `docs/user/chatgpt-instructions.md`: MCP client workflow guidance.
- `docs/architecture/overview.md`: current hybrid architecture.
- `docs/reference/public-tool-surface.md`: canonical tools, aliases, metadata, mutability, and power modes.
- `docs/reference/context-and-handoff.md`: AGENTS, skills, context packs, and `.ai-bridge`.
- `SECURITY.md`: operator-facing security notes and reporting.
- `docs/security/product-boundary.md`: power-control model.
- `TESTING.md` and `docs/testing/evals.md`: verification commands and release evals.
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


## ChatGPT prompt surface

When changing anything ChatGPT sees through MCP, preserve these prompt-surface rules:

- `initialize.instructions` should tell ChatGPT to start with `codex_self_test` and `codex_open_workspace`.
- ChatGPT should manage workers by human name, not by backend job IDs, session IDs, branch names, or worktree paths.
- Worker mode should explain that default workers use isolated write worktrees, survive PatchBay restart when durable state exists, and continue through `codex_worker_message`.
- Integration must be described as preview-first, explicit, no-commit, and preserving the worker worktree.
- Tool descriptions should include when to use the tool, relevant side effects, validation expectations, and fallback behavior.
- Setup docs should recommend `--tool-mode worker` for first real ChatGPT validation.
- Shared-server docs should tell ChatGPT to start with `codex_self_test`, treat one copied Server URL as one shared local state surface, use `takeover: true` only after user confirmation, and report `repo_busy` instead of trying to bypass locks.
