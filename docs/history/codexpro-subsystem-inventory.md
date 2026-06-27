# CodexPro Subsystem Inventory

## Provenance

CodexPro source evaluated:

- Git URL: `https://github.com/rebel0789/codexpro`
- Commit: `03556103b3dc6de2e67e6e64835a72363c3a71a1`
- npm package: `codexpro@0.28.5`
- License: MIT

The final product remains `patchbay`. CodexPro code can be copied, ported, or rewritten inside PatchBay as long as MIT attribution is preserved.

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
| Setup/start/doctor CLI | `scripts/codexpro.mjs` | Ported to Python scripts; keep no-auth tunnel rejection | `scripts/start.py`, `scripts/doctor.py` |
| Token auth | `src/http.ts`, `src/config.ts` | Integrated with FastAPI; bearer first and query token for copied ChatGPT URL flow | Auth layer |
| Tunnel handling | `scripts/codexpro.mjs` | Implemented after auth gates; no auto-public default | Optional launcher mode |
| Profile store | `src/profileStore.ts`, `src/http.ts` | Redact secrets; align with PatchBay config; avoid repo-committed profiles | User profile store |
| MCP annotations | `src/server.ts` | Convert to Python descriptors; enforce with tests | Tool registry |
| Tool-card resource | `src/toolCardWidget.ts` | Passive Python-served card implemented; richer widget remains future work | ChatGPT tool card |
| Skill inventory | `src/capabilitiesOps.ts` | Hide local paths by default; load by name with byte caps | `codex_list_skills`, `codex_load_skill` |
| Handoff/watch CLI | `scripts/codexpro.mjs` | Keep local terminal execution explicit; dry-run and confirmation defaults | `.ai-bridge` handoff commands |
| Redaction helpers | `src/redact.ts` | Merge with existing `security.py` patterns and tests | Shared redaction service |
| Safe bash policy | `src/bashOps.ts` | Do not expose by default; make policy explicit and tested | Optional power tool |

## Defer

| CodexPro area | Source | Reason |
| --- | --- | --- |
| Codex session metadata | `src/codexSessions.ts` | Useful for continuity, but privacy-sensitive. Add after core connector and jobs are stable. |
| Codex session transcript reads | `src/codexSessions.ts` | Can expose private conversation history. Default must be off. |
| Full ChatGPT Apps widget | `src/toolCardWidget.ts` | Passive card is implemented. Interactive card remains high product value after real ChatGPT evals. |
| ngrok/named Cloudflare UX | `scripts/codexpro.mjs` | Useful after bearer/OAuth-style auth, token rotation, and docs exist. |
| Browser/control panel UI | `src/http.ts` | Nice setup feature, but not required for the first connector release. |

## Do Not Port As Default

| CodexPro area | Source | Reason |
| --- | --- | --- |
| Generic public `read`/`write`/`edit` names | `src/server.ts`, `src/fsOps.ts` | PatchBay should expose Codex-specific intent, not generic file-system verbs. |
| Direct source write as default | `src/fsOps.ts` | Keep default changes through Codex jobs or `.ai-bridge`. Direct write can be a disabled power mode. |
| Public arbitrary bash | `src/bashOps.ts` | Even "safe" bash can execute project scripts. Keep optional and clearly mutating/open-world. |
| Auto-downloaded tunnel binary as default | `scripts/codexpro.mjs` | Supply-chain and exposure risk. Prefer explicit install or disabled default. |
| Permanent TypeScript sidecar | all TS modules | Doubles runtime and policy surface. Use only if widget prototyping demands it. |

## License And Attribution

When copying or porting CodexPro code:

- preserve the MIT license notice;
- add CodexPro provenance to a root `NOTICE` or attribution section;
- keep copied code identifiable in commit history;
- document major copied modules in this inventory;
- do not imply CodexPro upstream owns or endorses PatchBay release.

## Tests Required Per Adopted Subsystem

- Path guard: root allowlist, parent traversal, symlink escape, blocked glob, case/normalization edge cases.
- File read/tree/search: max bytes, binary files, ignored folders, redaction, missing file, outside root.
- Git context: non-git repo, dirty repo, large diff, binary diff, detached HEAD.
- AGENTS/skills: nested instructions, missing files, bounded reads, no arbitrary local path reads.
- `.ai-bridge`: writes allowed only inside `.ai-bridge`, no source write escape, artifact size caps.
- Auth/tunnel: localhost no-auth allowed only by config, public/non-loopback requires token, missing token rejected.
- Tool descriptors: annotations, security schemes, output template metadata, mutating tools not marked read-only.
