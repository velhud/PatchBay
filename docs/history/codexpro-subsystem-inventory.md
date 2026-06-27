# CodexPro Subsystem Inventory

## Provenance

CodexPro source evaluated:

- Git URL: `https://github.com/rebel0789/codexpro`
- Current donor-integration commit:
  `3062500409ba1b587d87935fb70f3a9b5f481025`
- Earlier migration-baseline commit:
  `03556103b3dc6de2e67e6e64835a72363c3a71a1`
- npm package: `codexpro@0.28.5`
- License: MIT

The final product remains `patchbay`. Current donor-integration work uses a
copy-first rule: copy each complete relevant CodexPro subsystem into
`src/patchbay/donors/codexpro/<track>/upstream/`, record provenance and hashes
in a track `MANIFEST.md`, then adapt or port it into PatchBay runtime code.
MIT attribution must be preserved.

## Classification Rules

- `adopt`: copy or port the subsystem as a core part of PatchBay.
- `port with changes`: use the design and selected code, but adapt naming, policy, schemas, or runtime.
- `defer`: valuable but not required for the first hybrid release.
- `do not port`: conflicts with the target product or should remain outside the default surface.

## Adopt

| CodexPro area | Source | Why it matters | PatchBay target |
| --- | --- | --- | --- |
| Path guard | `src/guard.ts` | Realpath checks, workspace roots, blocked paths, symlink escape control | New workspace/path policy service |
| Blocked globs | `src/config.ts`, `src/guard.ts` | Protects common secrets and irrelevant heavy folders | Configurable denylist with tests |
| Workspace orientation | `src/workspaceOps.ts` | Gives ChatGPT a useful first call before jobs | `codex_open_workspace` |
| Bounded tree/read | `src/fsOps.ts` | Safe repo inspection primitives | `codex_repo_tree`, `codex_read_file` |
| Search | `src/searchOps.ts` | ripgrep-first bounded search | `codex_search_repo` |
| Git summaries | `src/gitOps.ts` | Status/diff/log context for ChatGPT and Codex prompts | Context layer and result cards |
| AGENTS loading | `src/workspaceOps.ts` | Critical for following repo instructions | Context packs and job prompt assembly |
| `.ai-bridge` structure | `src/fsOps.ts`, `src/workspaceOps.ts` | Durable handoff and context artifacts | `.ai-bridge` handoff spec |
| Pro context export | `src/proContext.ts` | Solves manual copy/export pain | `codex_export_context` |
| Self-test concept | `src/server.ts`, smoke scripts | Lets ChatGPT verify connector health | Implemented as `codex_self_test` and `scripts/doctor.py` |

## Port With Changes

| CodexPro area | Source | Change required | PatchBay target |
| --- | --- | --- | --- |
| Setup/start/doctor CLI | `scripts/codexpro.mjs`, `src/profileStore.ts`, `src/http.ts`, `src/config.ts`, setup smokes and docs | Full donor setup subsystem copied in `src/patchbay/donors/codexpro/setup-ux/`; terminal setup guide and JSON `setup_guide` adapted into Python launcher while preserving no-auto-install tunnel behavior | `scripts/start.py`, `scripts/doctor.py`, connector profiles/status |
| Token auth | `src/http.ts`, `src/config.ts` | Integrated with FastAPI; bearer first and query token for copied ChatGPT URL flow | Auth layer |
| Tunnel handling | `scripts/codexpro.mjs` | Implemented after auth gates; no auto-public default | Optional launcher mode |
| Profile store | `src/profileStore.ts`, `src/http.ts` | Redact secrets; align with PatchBay config; avoid repo-committed profiles | User profile store |
| MCP annotations | `src/server.ts` | Convert to Python descriptors; enforce with tests | Tool registry |
| Compatibility alias schemas | `src/server.ts`, `src/fsOps.ts`, `src/bashOps.ts`, `src/gitOps.ts`, `src/workspaceOps.ts`, `src/capabilitiesOps.ts`, `src/proContext.ts`, smoke scripts | Full donor schema/handler source graph copied in `src/patchbay/donors/codexpro/alias-schemas/`; alias schemas and argument translation adapted into Python without reducing the tool catalog | `tools/list` descriptors, alias validation, canonical handler translation |
| Runtime-aware descriptor truthfulness | `src/server.ts`, `src/config.ts`, `config.example.env`, setup/settings/smoke scripts | Full donor mode/config registration subsystem copied in `src/patchbay/donors/codexpro/descriptor-truth/`; disabled runtime capabilities hide only the corresponding tools and aliases | `tool_descriptors_for_mode`, `tool_is_available` |
| Tool-card resource | `src/toolCardWidget.ts`, `src/server.ts`, CodexPro README/docs card sections | Full donor subsystem copied in `src/patchbay/donors/codexpro/tool-card/`; v2 widget adapted to PatchBay worker/job/artifact/diff/power-tool outputs | ChatGPT tool card |
| Skill inventory | `src/capabilitiesOps.ts` | Hide local paths by default; load by name with byte caps | `codex_list_skills`, `codex_load_skill` |
| Handoff/watch CLI | `scripts/codexpro.mjs` | Keep local terminal execution explicit; dry-run and confirmation defaults | `.ai-bridge` handoff commands |
| Redaction helpers | `src/redact.ts` | Merge with existing `security.py` patterns and tests | Shared redaction service |
| Safe bash policy | `src/bashOps.ts` | Expose only when the runtime bash mode enables command execution; hide descriptors when disabled | Optional power tool |

## Defer

| CodexPro area | Source | Reason |
| --- | --- | --- |
| Codex session metadata | `src/codexSessions.ts` | Implemented in CPI-005 by copying the donor session subsystem and merging configured Codex-home metadata with PatchBay-known job sessions. |
| Codex session transcript reads | `src/codexSessions.ts` | Can expose private conversation history. Must remain runtime-gated and descriptor-truthful. |
| Interactive ChatGPT Apps actions | `src/toolCardWidget.ts` | v2 passive rich card is implemented. Interactive actions remain deferred until real ChatGPT evals prove they improve reliability. |
| ngrok/named Cloudflare UX | `scripts/codexpro.mjs` | Useful after bearer/OAuth-style auth, token rotation, and docs exist. |
| Browser/control panel UI | `src/http.ts` | Nice setup feature, but not required for the first connector release. |

## Do Not Port As Default

| CodexPro area | Source | Reason |
| --- | --- | --- |
| Generic public `read`/`write`/`edit` names as the primary API | `src/server.ts`, `src/fsOps.ts` | PatchBay should expose Codex-specific intent as the canonical API. Compatibility aliases may still be advertised in full/minimal modes with precise schemas and canonical handler translation. |
| Direct source write as a hidden mismatch | `src/fsOps.ts` | PatchBay's checked-in profile is intentionally full-power. Narrower profiles must hide disabled direct-write descriptors instead of advertising tools that will reject every call. |
| Public arbitrary bash | `src/bashOps.ts` | Even "safe" bash can execute project scripts. Keep optional and clearly mutating/open-world. |
| Auto-downloaded tunnel binary as default | `scripts/codexpro.mjs` | Supply-chain and exposure risk. Prefer explicit install or disabled default. |
| Permanent TypeScript sidecar | all TS modules | Doubles runtime and policy surface. Use only if widget prototyping demands it. |

## License And Attribution

When copying or porting CodexPro code:

- preserve the MIT license notice;
- add CodexPro provenance to a root `NOTICE` or attribution section;
- keep copied code identifiable in commit history;
- document major copied modules in this inventory;
- keep full copied donor subsystem files under
  `src/patchbay/donors/codexpro/<track>/upstream/` before adapting them;
- record donor commit, copied paths, destination paths, and sha256 hashes in
  each track manifest;
- do not imply CodexPro upstream owns or endorses PatchBay release.

## Tests Required Per Adopted Subsystem

- Path guard: root allowlist, parent traversal, symlink escape, blocked glob, case/normalization edge cases.
- File read/tree/search: max bytes, binary files, ignored folders, redaction, missing file, outside root.
- Git context: non-git repo, dirty repo, large diff, binary diff, detached HEAD.
- AGENTS/skills: nested instructions, missing files, bounded reads, no arbitrary local path reads.
- `.ai-bridge`: writes allowed only inside `.ai-bridge`, no source write escape, artifact size caps.
- Auth/tunnel: localhost no-auth allowed only by config, public/non-loopback requires token, missing token rejected.
- Tool descriptors: annotations, security schemes, output template metadata, mutating tools not marked read-only.
