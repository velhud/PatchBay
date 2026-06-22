# Contributing

Small fixes, documentation improvements, compatibility notes, and test coverage are welcome.

This project is a ChatGPT-to-local-Codex bridge. Changes should preserve local control while keeping the product powerful enough to be useful from ChatGPT.

## Before Opening A Change

1. Keep localhost-first defaults.
2. Do not weaken auth for public, non-loopback, or tunnel use.
3. Do not commit credentials, tokenized URLs, logs, local config, Codex transcripts, or generated worktrees.
4. Keep mutating/open-world tools clearly marked in MCP descriptors.
5. Preserve the canonical `codex_*` public tool names unless the change is explicitly a compatibility migration.
6. Preserve CodexPro attribution in `NOTICE` and README for derived behavior.

## Required Verification

Run:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
```

For connector, tool-surface, auth, workspace-context, or ChatGPT-facing changes, also run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

For Codex execution changes, verify at least one disposable real Codex job through MCP and record the Codex CLI version.

## Documentation Requirements

Update docs in the same change when modifying:

- public tool names, schemas, aliases, or metadata;
- connector auth, tunnel, profile, or launch behavior;
- power-mode behavior;
- path guards, blocked globs, redaction, or logging;
- Codex CLI command generation or result parsing;
- release-readiness claims.

At minimum, check `README.md`, `QUICKSTART.md`, `CHATGPT_INSTRUCTIONS.md`, `PUBLIC_TOOL_SURFACE.md`, `SECURITY_PRODUCT_BOUNDARY.md`, and `TESTING.md`.

## Release Readiness Claims

Do not claim the project is release-ready unless disposable-repo evals cover:

- real ChatGPT Developer Mode connection;
- token-gated public tunnel connection;
- real plan and apply jobs;
- diff inspection;
- resume or interactive continuation;
- blocked path and disabled power-tool failures.
