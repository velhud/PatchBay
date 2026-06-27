# CodexPro Donor Subsystems

This directory stores copied CodexPro source material used by PatchBay's
donor-integration campaign.

PatchBay remains the product runtime. Copied CodexPro files are preserved here
as provenance and behavior authority before PatchBay-specific adapters, Python
ports, generated assets, or glue are added elsewhere in `src/patchbay`.

## Source

- Upstream repository: `https://github.com/rebel0789/codexpro`
- Donor package: `codexpro`
- Audited campaign commit: `3062500409ba1b587d87935fb70f3a9b5f481025`
- Audited package version: `0.28.5`
- License: MIT

## Layout

Each copied subsystem uses this structure:

```text
src/patchbay/donors/codexpro/<track>/
  MANIFEST.md
  upstream/
    ...full copied donor files...
```

`upstream/` files are original donor copies. Do not edit them after hashing.
PatchBay adaptations should live outside `upstream/` or in ordinary PatchBay
runtime modules.

## Manifest Convention

Each track `MANIFEST.md` must record:

- donor clone path used for the copy;
- donor commit;
- copied date;
- donor source path for every copied file;
- PatchBay destination path for every copied file;
- sha256 hash for every copied file;
- adaptation entrypoints;
- attribution notes.

## Campaign Tracks

The active copy-first tracks are:

1. `tool-card`: rich ChatGPT card/widget renderer.
2. `setup-ux`: guided setup, connection, status, profile, and local control UX.
3. `alias-schemas`: precise compatibility alias schemas and argument mapping.
4. `descriptor-truth`: runtime-aware descriptor truthfulness.
5. `codex-sessions`: broader Codex session discovery/browser behavior.
6. `transport-packaging`: public package metadata, stdio transport entrypoint,
   and launch checklist behavior.

Tool catalog reduction is explicitly out of scope for this campaign.
