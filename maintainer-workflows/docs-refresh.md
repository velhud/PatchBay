# Docs Refresh

## Goal

Use Codex to identify and stage documentation improvements.

## Preconditions

- Repository is owned or authorized.
- Server is local.
- Repository path is allowed.
- Secrets are not included in prompts.

## Steps

1. Run read-only analysis.
2. Inspect output.
3. Run apply job only if needed.
4. Inspect diff.
5. Run tests.
6. Merge manually.

## Human Gate

Codex output is advisory or staged. The maintainer decides.
