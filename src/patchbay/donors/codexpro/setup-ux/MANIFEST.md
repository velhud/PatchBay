# CodexPro Setup UX Donor Manifest

Track: `setup-ux`

Copied date: 2026-06-27

Donor clone path: `/tmp/codexpro-current-audit`

Donor commit: `3062500409ba1b587d87935fb70f3a9b5f481025`

Donor package: `codexpro@0.28.5`

License: MIT. See root `NOTICE`.

## Copied Files

| Donor source | PatchBay destination | sha256 |
| --- | --- | --- |
| `DOMAIN_SETUP.md` | `src/patchbay/donors/codexpro/setup-ux/upstream/DOMAIN_SETUP.md` | `a50b6b02da9496724b49ba72f8e67ac5f33d32c198bf37670111a539a707ddfb` |
| `FAQ.md` | `src/patchbay/donors/codexpro/setup-ux/upstream/FAQ.md` | `c7d5338effbc9b8a4e2b1bddcbc3dfb844d01000ed0c9e55a0927c6701d6f9e6` |
| `README.md` | `src/patchbay/donors/codexpro/setup-ux/upstream/README.md` | `fe9741656948e47ee2afcc0b3aa465320f6ae6488f145cb294969d982ab94802` |
| `config.example.env` | `src/patchbay/donors/codexpro/setup-ux/upstream/config.example.env` | `ed5af8ec3bae65d1ee7ba836f1ff01b4469ee4e20f3540f89faeb72467cf2dc5` |
| `scripts/codexpro.mjs` | `src/patchbay/donors/codexpro/setup-ux/upstream/scripts/codexpro.mjs` | `45ba0f1fac03183985084a032b046f6d3a6f4fc66193e5c1395b27139fae2f85` |
| `scripts/doctor-smoke.mjs` | `src/patchbay/donors/codexpro/setup-ux/upstream/scripts/doctor-smoke.mjs` | `3eb1ca39d8b105bcd4c130f9e7b07f10862dcb0e39fb41aa7270e257ab7fb7ab` |
| `scripts/settings-smoke.mjs` | `src/patchbay/donors/codexpro/setup-ux/upstream/scripts/settings-smoke.mjs` | `1879f10aa448bb59802984a8134558c66b14c876ee55e89641c5cd3ef3877091` |
| `src/config.ts` | `src/patchbay/donors/codexpro/setup-ux/upstream/src/config.ts` | `b857901fe83d1dfd55f087e387eb0334a5acca47d8d1af6f2cb044e85d0c1678` |
| `src/http.ts` | `src/patchbay/donors/codexpro/setup-ux/upstream/src/http.ts` | `69b5b759924dc454737df52bc533ff215a9be8fae3ac02dac178b33770d6975f` |
| `src/profileStore.ts` | `src/patchbay/donors/codexpro/setup-ux/upstream/src/profileStore.ts` | `e661b3df389c581b146fa36d71ee1b1baea11bf7618c9119fdd467aa01260452` |

## Adaptation Entrypoints

- `scripts/start.py`
- `scripts/doctor.py`
- `src/patchbay/connector/launcher.py`
- `src/patchbay/connector/profiles.py`
- `src/patchbay/connector/status.py`
- `src/patchbay/connector/tunnels.py`
- `tests/test_launcher.py`
- `tests/test_connector.py`
- `tests/test_profile_store.py`

## Notes

- `upstream/` files are preserved donor copies and should not be edited after
  this hash record.
- The donor browser admin page is copied as source material, but PatchBay's
  primary experience remains the ChatGPT-to-local-Codex MCP control plane.
- This phase may add guided terminal/status/control helpers. It must not make a
  browser dashboard the primary product surface and must not auto-install
  tunnel binaries by default.
