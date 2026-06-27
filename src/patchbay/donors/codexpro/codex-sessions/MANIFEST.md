# CodexPro Codex Sessions Donor Manifest

Track: `codex-sessions`

Copied date: 2026-06-27

Donor clone: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

## Purpose

This track preserves the CodexPro local Codex session browser subsystem used to
discover session JSONL files under Codex home roots, list bounded metadata, and
read bounded transcripts when explicitly enabled.

PatchBay adaptations live in `src/patchbay/jobs/sessions.py`,
`src/patchbay/tools/handler.py`, `src/patchbay/protocol/mcp.py`, and tests.
Files in `upstream/` are copied donor artifacts and should remain unchanged
after this manifest is recorded.

## Copied Files

| Donor source | PatchBay copy | sha256 |
| --- | --- | --- |
| `README.md` | `src/patchbay/donors/codexpro/codex-sessions/upstream/README.md` | `fe9741656948e47ee2afcc0b3aa465320f6ae6488f145cb294969d982ab94802` |
| `scripts/http-smoke.mjs` | `src/patchbay/donors/codexpro/codex-sessions/upstream/scripts/http-smoke.mjs` | `78f83fd4e861031c62d67878105e9236d03b97afdad11777e7d96603b063d83c` |
| `scripts/smoke.mjs` | `src/patchbay/donors/codexpro/codex-sessions/upstream/scripts/smoke.mjs` | `c439b447853072346b6e6bcae85e8827afc37bdc1f0177087e6b0cb803e6f2c3` |
| `src/codexSessions.ts` | `src/patchbay/donors/codexpro/codex-sessions/upstream/src/codexSessions.ts` | `53e9fb438d5e3eb846a9fe8c4de4743caa265740029217373bb1cfbb0b895bf9` |
| `src/config.ts` | `src/patchbay/donors/codexpro/codex-sessions/upstream/src/config.ts` | `b857901fe83d1dfd55f087e387eb0334a5acca47d8d1af6f2cb044e85d0c1678` |
| `src/redact.ts` | `src/patchbay/donors/codexpro/codex-sessions/upstream/src/redact.ts` | `f0a16d52271ea8c22009e67ac0f4748e06589dea80dbae50a04784dc2b428103` |
| `src/server.ts` | `src/patchbay/donors/codexpro/codex-sessions/upstream/src/server.ts` | `0678a3527ad2b9b7e7c2401775cc4a7c4f372de65c393f97ba7ea0fe456a4778` |

## Donor Behavior To Preserve

- Discover Codex JSONL sessions under `codex_home/sessions` and
  `codex_home/archived_sessions`.
- Bound scan depth and file counts.
- Parse metadata without reading full transcripts.
- Read transcripts only through an explicit transcript-read capability.
- Keep source-path handling contained to configured Codex session roots.

## Adaptation Entrypoints

- `src/patchbay/jobs/sessions.py`: Python session discovery and bounded
  transcript reader.
- `src/patchbay/tools/handler.py`: merge PatchBay-known job sessions with
  discovered Codex-home sessions.
- `src/patchbay/protocol/mcp.py`: `codex_list_sessions` query schema.
- `tests/test_codex_sessions.py`: temporary Codex home fixtures.

## Attribution

CodexPro is MIT licensed. Attribution is preserved in `NOTICE` and public
history docs for all copied donor source material.
