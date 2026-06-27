# Runtime Decision

## Decision

Keep `patchbay` Python/FastAPI first.

Port CodexPro ideas and selected code into PatchBay's Python architecture instead of permanently embedding a TypeScript sidecar.

This is the current implementation direction, not only a future recommendation.

## Why Python Remains Primary

PatchBay already has:

- FastAPI Streamable HTTP MCP endpoint;
- explicit public tool allowlist;
- Codex job manager;
- Codex CLI subprocess executor;
- worktree apply model;
- tests for tool surface, path validation, redaction, security defaults, and protocol initialize;
- project rules favoring local control, localhost-first defaults, and explicit power boundaries.

The most valuable CodexPro features are not TypeScript-specific. They are product and architecture patterns:

- setup/start/doctor flow;
- ChatGPT Developer Mode connector output;
- token/tunnel policy;
- MCP annotations and `_meta`;
- workspace orientation;
- path guard;
- bounded read/search/tree;
- AGENTS and skill discovery;
- `.ai-bridge` handoff;
- context export;
- tool-card resource.

These can be ported to Python without carrying a second runtime.

## Why Not Rewrite To TypeScript

A TypeScript rewrite would provide faster access to CodexPro code but would create avoidable costs:

- reimplement PatchBay job engine, worktree lifecycle, and Codex CLI parsing;
- recreate existing Python tests;
- move project packaging and installation model;
- double-check every power-boundary invariant from scratch;
- increase risk of inheriting CodexPro's direct-local-editor assumptions as defaults.

The user goal is the strongest final app in PatchBay repo, not preservation of CodexPro's architecture.

## Sidecar Escape Hatch

A temporary Node sidecar is allowed only for:

- rapid ChatGPT Apps widget/tool-card prototyping;
- reusing CodexPro's existing HTML/resource flow before the Python server supports equivalent resource metadata;
- comparing behavior during migration.

Sidecar constraints:

- not required for core tool calls;
- not the permanent execution engine;
- no independent auth policy;
- no separate hidden file-system authority;
- logs go through PatchBay policy;
- removed or collapsed before stable release unless documented as intentional.

## Packaging Implications

Python-first packaging should provide:

- one install path;
- one CLI entrypoint;
- one config/profile system;
- one MCP endpoint;
- one policy layer.

If a frontend/widget build is added later, it should compile static assets that the Python server can serve as MCP resources.

## Dependency Policy

Add dependencies only when they remove meaningful risk or complexity.

Likely acceptable:

- JSON schema validation;
- TOML/YAML config validation;
- git helpers if current GitPython use is insufficient;
- small path/glob utility if Python standard library is too awkward.

Scrutinize:

- tunnel libraries;
- browser automation;
- shell execution helpers;
- long-lived process supervisors;
- UI bundlers.

## Porting Strategy

Preferred strategy:

1. Copy each approved CodexPro subsystem into
   `src/patchbay/donors/codexpro/<track>/upstream/` as full source material.
2. Record donor commit, source paths, destination paths, and sha256 hashes in a
   track `MANIFEST.md`.
3. Implement Python services, generated resources, adapters, or shims behind
   PatchBay interfaces using the copied donor subsystem as the behavior
   authority.
4. Keep public `codex_*` tool naming.
5. Add CodexPro-style UX only where it strengthens PatchBay's
   ChatGPT-to-local-Codex control plane.
6. Preserve MIT attribution where code is directly copied or closely ported.

The copy-first rule preserves donor fidelity; it does not change the runtime
decision. PatchBay remains Python/FastAPI first unless a later explicit
architecture decision introduces a temporary sidecar.

## Current Status

Completed in the Python runtime:

- ChatGPT-ready MCP descriptors and `_meta`;
- passive Apps-style tool-card resource;
- token auth and fail-closed tunnel policy;
- launcher profile/runtime config flow;
- workspace tree/read/search/git/context tools;
- AGENTS and skill discovery/loading;
- `.ai-bridge` handoff and Pro context scripts;
- natural-language worker facade;
- artifact inbox transfer for ChatGPT-generated files and zips;
- isolated worker worktrees, peer-worker context, and explicit worker integration;
- durable job state, cancellation, session metadata, and result parsing;
- direct tokenized public-tunnel MCP simulation through ngrok for the artifact inbox worker path;
- optional direct write/bash/session-read power tools.

Still open:

- real ChatGPT Developer Mode UI/tool-selection and ChatGPT-originated tunnel release evals;
- richer interactive tool-card behavior if needed;
- optional standalone local control surface if the product needs an app UI.
