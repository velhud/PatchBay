# CodexPro Tool Card Donor Manifest

Track: `tool-card`

Copied date: 2026-06-27

Donor clone path: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

License: MIT. See root `NOTICE`.

## Copied Files

| Donor source | PatchBay destination | sha256 |
| --- | --- | --- |
| `README.md` | `src/patchbay/donors/codexpro/tool-card/upstream/README.md` | `fe9741656948e47ee2afcc0b3aa465320f6ae6488f145cb294969d982ab94802` |
| `docs/favicon.svg` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/favicon.svg` | `5ecb2b7d9b377248c09361a2593b08efcb91651451866f4fbeb93e463b52900a` |
| `docs/index.html` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/index.html` | `411b193c6aa9dffecf13cd72da61c8ae42cfe715d82ce983a238c36ca43e4794` |
| `docs/og.svg` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/og.svg` | `b95f0ad11901156070c36f79ea0ff688b9961d4ef33242ce26de3851eb241622` |
| `docs/script.js` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/script.js` | `d44936f4168658f02bd43260afb3705d572d70818701f5d3075686a2db370b4b` |
| `docs/star.svg` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/star.svg` | `98d26ba09881a205290a93b833deb7c8d5d9466bf5436dc76df520516f26af73` |
| `docs/styles.css` | `src/patchbay/donors/codexpro/tool-card/upstream/docs/styles.css` | `4db3329b67cbc66408b5708b3d2957de5942213edcf35aa36feb821a73110342` |
| `src/server.ts` | `src/patchbay/donors/codexpro/tool-card/upstream/src/server.ts` | `0678a3527ad2b9b7e7c2401775cc4a7c4f372de65c393f97ba7ea0fe456a4778` |
| `src/toolCardWidget.ts` | `src/patchbay/donors/codexpro/tool-card/upstream/src/toolCardWidget.ts` | `c4e25785faf4dbd2e326338005bf982b85f62b4079a97c0c0f32f9e3eb742e78` |

## Adaptation Entrypoints

- `src/patchbay/protocol/resources.py`
- `tests/test_tool_resources.py`
- `docs/history/codexpro-subsystem-inventory.md`
- `docs/architecture/runtime-decision.md`
- `NOTICE`

## Notes

- `upstream/` files are preserved donor copies and should not be edited after
  this hash record.
- PatchBay keeps its Python/FastAPI runtime. The copied TypeScript widget and
  server registration code are behavior/provenance authority for the richer
  PatchBay Apps resource.
- Tool catalog discipline/reduction is not part of this track.
