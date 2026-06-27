# CodexPro Transport And Packaging Donor Manifest

Track: `transport-packaging`

Copied date: 2026-06-27

Donor clone path: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

License: MIT. See root `NOTICE`.

## Copied Files

| Donor source | PatchBay destination | sha256 |
| --- | --- | --- |
| `package.json` | `src/patchbay/donors/codexpro/transport-packaging/upstream/package.json` | `9e647c239e9fde1542d9b94a5f21b92020f24ca66fc1062a65bc67bd24306ba7` |
| `src/stdio.ts` | `src/patchbay/donors/codexpro/transport-packaging/upstream/src/stdio.ts` | `4c8f6054dd1742a0b1ff599576a23692b9f85e032644cfc012c5606e00fbe336` |
| `PUBLIC_LAUNCH_CHECKLIST.md` | `src/patchbay/donors/codexpro/transport-packaging/upstream/PUBLIC_LAUNCH_CHECKLIST.md` | `a1310dcb011c2692e53a7ab69f08ac8b5f2e5eca23938c699213a263d0d03d17` |
| `scripts/smoke.mjs` | `src/patchbay/donors/codexpro/transport-packaging/upstream/scripts/smoke.mjs` | `c439b447853072346b6e6bcae85e8827afc37bdc1f0177087e6b0cb803e6f2c3` |
| `README.md` | `src/patchbay/donors/codexpro/transport-packaging/upstream/README.md` | `fe9741656948e47ee2afcc0b3aa465320f6ae6488f145cb294969d982ab94802` |

## Adaptation Entrypoints

- `pyproject.toml`
- `src/patchbay/cli.py`
- `src/patchbay/stdio.py`
- `scripts/start.py`
- `scripts/doctor.py`
- `src/patchbay/connector/launcher.py`
- `src/patchbay/connector/tunnels.py`
- `tests/test_cli.py`
- `tests/test_stdio_transport.py`

## Notes

- `upstream/` files are preserved donor copies and should not be edited after
  this hash record.
- PatchBay remains the runtime authority. The copied CodexPro stdio entrypoint
  is behavior/provenance input for the Python stdio transport, not a permanent
  Node sidecar.
- CodexPro's `loop-handoff` and direct-edit-first product identity are not
  part of this track.
