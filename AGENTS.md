# AGENTS.md

## Project Purpose

This repository provides a hybrid ChatGPT-to-local-Codex bridge.

The product exposes a local Streamable HTTP MCP server so ChatGPT web/Pro or another MCP-compatible client can:

- inspect allowed local workspaces through bounded context tools;
- delegate larger work to local Codex CLI jobs;
- inspect async job status, structured results, session refs, and worktree diffs;
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
- Add or update tests for path validation, job lifecycle behavior, and unsafe input handling.
- Update README, examples, and tests when changing public tool names, CLI arguments, server behavior, or MCP schemas.
- Update `README.md`, `QUICKSTART.md`, `CHATGPT_INSTRUCTIONS.md`, `PUBLIC_TOOL_SURFACE.md`, `SECURITY_PRODUCT_BOUNDARY.md`, and `TESTING.md` when changing connector behavior, auth/tunnel behavior, tool metadata, power modes, Codex CLI assumptions, or result parsing.
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
10. stale documentation that describes the app as only a maintainer wrapper;
11. documentation that overstates safety, verified coverage, or production readiness;
12. missing CodexPro attribution.

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
