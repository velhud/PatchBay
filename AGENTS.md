# AGENTS.md

## Project Purpose

This repository provides a local-first Streamable HTTP MCP wrapper for Codex CLI maintainer workflows.

## Rules For Agents

- Do not add secrets, tokens, local paths, or private machine identifiers.
- Do not remove security checks without explaining why.
- Prefer small, reviewable changes.
- Keep read-only behavior as the default.
- Preserve local-first behavior.
- Do not introduce network exposure without authentication.
- Do not enable dangerous bypass in public examples.
- Do not log prompts, secrets, auth files, or full Codex outputs by default.
- Mutating tools must be clearly marked as mutating.
- Add or update tests for path validation, job lifecycle behavior, and unsafe input handling.
- Update README, examples, and tests when changing public tool names, CLI arguments, server behavior, or MCP schemas.

## Review Priorities

When reviewing changes, prioritize:

1. unsafe expansion of repository scope;
2. public network exposure;
3. CORS or authentication weakening;
4. dangerous bypass support;
5. hidden tool exposure;
6. prompt, config, token, or environment leakage;
7. write tools incorrectly marked as read-only;
8. unvalidated paths or config overrides;
9. worktree cleanup and diff correctness;
10. documentation that overstates safety or production readiness.

## Required Checks

Run these before proposing a change:

```bash
python -m compileall .
python -m pytest tests -q
```

If tests are not yet available, add minimal tests for the changed behavior.

## Preferred Workflow

1. Open an issue describing the maintenance change.
2. Create a branch.
3. Add or update tests.
4. Open a PR.
5. Review the diff before merge.
