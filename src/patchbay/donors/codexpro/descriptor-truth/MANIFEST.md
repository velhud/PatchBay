# CodexPro Descriptor Truth Donor Manifest

Track: `descriptor-truth`

Copied date: 2026-06-27

Donor clone: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

## Purpose

This track preserves the CodexPro mode/config registration subsystem that makes
tool descriptors reflect runtime capability settings such as write mode, bash
mode, tool mode, and Codex session history mode.

PatchBay adaptations live in `src/patchbay/protocol/mcp.py` and tests. Files in
`upstream/` are copied donor artifacts and should remain unchanged after this
manifest is recorded.

## Copied Files

| Donor source | PatchBay copy | sha256 |
| --- | --- | --- |
| `config.example.env` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/config.example.env` | `ed5af8ec3bae65d1ee7ba836f1ff01b4469ee4e20f3540f89faeb72467cf2dc5` |
| `scripts/codexpro.mjs` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/scripts/codexpro.mjs` | `45ba0f1fac03183985084a032b046f6d3a6f4fc66193e5c1395b27139fae2f85` |
| `scripts/doctor-smoke.mjs` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/scripts/doctor-smoke.mjs` | `3eb1ca39d8b105bcd4c130f9e7b07f10862dcb0e39fb41aa7270e257ab7fb7ab` |
| `scripts/http-smoke.mjs` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/scripts/http-smoke.mjs` | `78f83fd4e861031c62d67878105e9236d03b97afdad11777e7d96603b063d83c` |
| `scripts/settings-smoke.mjs` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/scripts/settings-smoke.mjs` | `1879f10aa448bb59802984a8134558c66b14c876ee55e89641c5cd3ef3877091` |
| `scripts/smoke.mjs` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/scripts/smoke.mjs` | `c439b447853072346b6e6bcae85e8827afc37bdc1f0177087e6b0cb803e6f2c3` |
| `src/config.ts` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/src/config.ts` | `b857901fe83d1dfd55f087e387eb0334a5acca47d8d1af6f2cb044e85d0c1678` |
| `src/server.ts` | `src/patchbay/donors/codexpro/descriptor-truth/upstream/src/server.ts` | `0678a3527ad2b9b7e7c2401775cc4a7c4f372de65c393f97ba7ea0fe456a4778` |

## Donor Behavior To Preserve

- Disabled write mode removes `write` and `edit` from advertised tools.
- Disabled bash mode removes `bash` from advertised tools.
- Codex session tools are advertised only when configured session mode supports
  them.
- Tool-mode filtering is capability-aware, not an aesthetic catalog reduction.

## Adaptation Entrypoints

- `src/patchbay/protocol/mcp.py`: runtime capability filtering for descriptors
  and `tools/call` availability.
- `tests/test_tool_surface.py`: enabled/disabled profile coverage.
- `scripts/live_mcp_eval.py`: full-power descriptor count regression.

## Attribution

CodexPro is MIT licensed. Attribution is preserved in `NOTICE` and public
history docs for all copied donor source material.
