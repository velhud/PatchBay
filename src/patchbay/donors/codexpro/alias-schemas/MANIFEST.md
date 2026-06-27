# CodexPro Alias Schema Donor Manifest

Track: `alias-schemas`

Copied date: 2026-06-27

Donor clone: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

## Purpose

This track preserves the CodexPro tool-registration and handler source graph
used to define precise compatibility tool schemas such as `read`, `write`,
`edit`, `bash`, `show_changes`, `open_workspace`, `codex_context`,
`export_pro_context`, `codex_sessions`, and `read_codex_session`.

PatchBay adaptations live in `src/patchbay/protocol/mcp.py` and tests. Files in
`upstream/` are copied donor artifacts and should remain unchanged after this
manifest is recorded.

## Copied Files

| Donor source | PatchBay copy | sha256 |
| --- | --- | --- |
| `scripts/http-smoke.mjs` | `src/patchbay/donors/codexpro/alias-schemas/upstream/scripts/http-smoke.mjs` | `78f83fd4e861031c62d67878105e9236d03b97afdad11777e7d96603b063d83c` |
| `scripts/smoke.mjs` | `src/patchbay/donors/codexpro/alias-schemas/upstream/scripts/smoke.mjs` | `c439b447853072346b6e6bcae85e8827afc37bdc1f0177087e6b0cb803e6f2c3` |
| `src/bashOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/bashOps.ts` | `3ca4ee02ed7d97256e31ed92bea722552bb33d3ff341d1d007eec1b3d1f05a3c` |
| `src/capabilitiesOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/capabilitiesOps.ts` | `4f57523647a43066c6c796aae915a74aac3ed3cbc7ec6711d47f6f0296293e36` |
| `src/codexSessions.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/codexSessions.ts` | `53e9fb438d5e3eb846a9fe8c4de4743caa265740029217373bb1cfbb0b895bf9` |
| `src/config.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/config.ts` | `b857901fe83d1dfd55f087e387eb0334a5acca47d8d1af6f2cb044e85d0c1678` |
| `src/fsOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/fsOps.ts` | `8fa8d524b3b7374b1f26366e8eb7f508c6a8d212cd246dfe06831ee9ab6ccb95` |
| `src/gitOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/gitOps.ts` | `91367642281433d7fb6d819afaae63c4aceb0f1a59d165cce959f317cb9ef564` |
| `src/guard.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/guard.ts` | `d518563080104b9049de3e8d5cca3db5d75d95bbbb82c18a1e6500280316fcb4` |
| `src/proContext.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/proContext.ts` | `593d9ee7ff8d77e132d719b3febbf50216558885aa50c22d63a3726a94abd06f` |
| `src/redact.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/redact.ts` | `f0a16d52271ea8c22009e67ac0f4748e06589dea80dbae50a04784dc2b428103` |
| `src/searchOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/searchOps.ts` | `4dd854e01430178d66e537f2238bbb9ec9dffaa989f9d4befbb2c8b76533b899` |
| `src/server.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/server.ts` | `0678a3527ad2b9b7e7c2401775cc4a7c4f372de65c393f97ba7ea0fe456a4778` |
| `src/toolCardWidget.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/toolCardWidget.ts` | `c4e25785faf4dbd2e326338005bf982b85f62b4079a97c0c0f32f9e3eb742e78` |
| `src/workspaceOps.ts` | `src/patchbay/donors/codexpro/alias-schemas/upstream/src/workspaceOps.ts` | `b98810f79d6c5f00e2d564605ed311c55a45c59aeaa8f4197070ffde215646f3` |

## Adaptation Entrypoints

- `src/patchbay/protocol/mcp.py`: compatibility alias descriptor schemas,
  argument translation, and validation.
- `tests/test_tool_surface.py`: descriptor and validation coverage.
- `scripts/live_mcp_eval.py`: runtime alias smoke coverage.

## Attribution

CodexPro is MIT licensed. Attribution is preserved in `NOTICE` and public
history docs for all copied donor source material.
