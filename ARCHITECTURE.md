# Hybrid Architecture

## Product Identity

`codex-mcp-wrapper` is the release repository for a hybrid ChatGPT-to-local-Codex bridge. CodexPro is MIT-licensed source material and product inspiration, not an upstream target, fork base, or contribution destination.

The intended user experience is:

1. Start one local application.
2. Connect ChatGPT web/Pro through Developer Mode or an Apps-compatible MCP connector.
3. Open an allowed local workspace.
4. Load repository context, AGENTS instructions, selected files, skills, git status, and diffs.
5. Delegate larger work to local Codex jobs.
6. Inspect job status, results, changed files, and diffs from ChatGPT.
7. Resume prior Codex work when useful.
8. Optionally enable direct workspace tools such as edit, bash, or session transcript reads behind explicit power-mode controls.

The product is not trying to preserve either current architecture for its own sake. The wrapper repository remains the final application because that is the desired release target.

## Runtime Decision

The recommended architecture is Python/FastAPI first, with CodexPro subsystems ported into the wrapper rather than run as a permanent TypeScript sidecar.

Reasons:

- The wrapper already owns the most valuable execution boundary: async Codex jobs, isolated apply worktrees, result inspection, diff APIs, and environment restriction.
- CodexPro's most useful systems are mostly product and connector systems: setup flow, ChatGPT metadata, workspace context, path guarding, `.ai-bridge`, auth/tunnel handling, and optional UI resources.
- A permanent Node sidecar doubles process supervision, logging policy, package management, auth review, and failure modes.
- Porting CodexPro's concepts into the wrapper gives one public MCP server and one policy layer.

A temporary Node sidecar is acceptable only for a fast ChatGPT Apps widget prototype if Python MCP resource support becomes the blocker.

## Component Model

```text
ChatGPT Developer Mode / Apps-compatible MCP client
   |
   | Streamable HTTP MCP /mcp
   v
Connector layer
   | auth, session handling, tool descriptors, resources, tool cards
   v
Policy and tool registry
   | public allowlist, tool tiers, schema validation, mutability hints
   v
Workspace context layer
   | allowed roots, path guard, AGENTS, skills, tree, search, read, context packs
   v
Codex orchestration layer
   | async jobs, command builder, resume, interactive continuation
   v
Execution layer
   | Codex CLI subprocess, restricted env, durable job store, logs/artifacts
   v
Worktree and artifact layer
   | isolated apply worktrees, diffs, `.ai-bridge`, review artifacts
```

## Public MCP Boundary

The public boundary must remain explicit. Tools are registered from a typed registry, not discovered from handler functions.

Each public tool needs:

- stable name;
- "Use this when..." description optimized for ChatGPT;
- JSON input schema;
- output shape or documented structured content;
- `annotations.readOnlyHint`;
- `annotations.destructiveHint`;
- `annotations.openWorldHint`;
- `_meta.securitySchemes` mirrored for ChatGPT compatibility;
- short invocation labels;
- output template URI for the shared ChatGPT tool card resource.

Developer Mode treats tools without `readOnlyHint` as write actions, so missing annotations are product bugs.

## Tool Tiers

### Tier 1: Core Codex Jobs

Default tools for serious work:

- `codex_plan_job`
- `codex_apply_job`
- `codex_get_status`
- `codex_get_result`
- `codex_get_diff`
- `codex_review`
- `codex_list_sessions`
- `codex_resume`
- `codex_interactive`
- `codex_interactive_reply`
- `codex_get_config`

These are the current wrapper surface and should remain stable. Current Codex CLI `0.141.0` JSONL results are parsed from `item.completed` / `agent_message` events into structured job output.

`codex_resume`, `codex_interactive`, and `codex_interactive_reply` are async job starters classified as mutating/open-world in the public descriptors because they can continue sessions that write locally or call Codex externally. `codex_plan_job` remains locally read-only, but is not idempotent because it creates job state and can invoke Codex.

`codex_list_sessions` is metadata-only: it returns bounded known session ids from durable job records and explicitly does not read transcript bodies or return repo paths.

`codex_read_session` is the explicit transcript power mode. It is advertised as read-only but remains disabled by default; when enabled it reads only bounded Codex session JSONL messages, redacts likely secrets, and does not return local session source paths.

### Tier 2: Workspace Context

Read-only tools ported from CodexPro concepts:

- `codex_open_workspace`
- `codex_repo_tree`
- `codex_search_repo`
- `codex_read_file`
- `codex_load_context`
- `codex_export_context`
- `codex_list_skills`
- `codex_load_skill`

These make ChatGPT useful before it starts a Codex job. They are bounded, redacted, and rooted in the active workspace.

### Tier 3: Handoff Artifacts

Controlled write tools limited to `.ai-bridge`:

- `codex_write_handoff`
- `codex_get_handoff_status`
- `codex_get_handoff_diff`

These bridge ChatGPT planning to local terminal execution without giving ChatGPT arbitrary write access to source files.

### Tier 4: Power Tools

Disabled by default, but designed as first-class optional capabilities:

- direct file write/edit;
- safe bash;
- full bash;
- Codex session metadata;
- Codex session transcript reads;
- public tunnel mode, implemented as optional launcher-supervised child processes with token-gated HTTP.

Power tools are not "unsafe illusions"; they are product power. They must be controlled because broken control makes the tool less useful for real work.

## Codex Execution Boundary

The execution boundary should be rewritten around explicit services:

- `CommandBuilder`: builds `codex exec` commands with options before the final stdin prompt sentinel, keeping user prompts out of process argv.
- `JobStore`: durable job records instead of memory-only state.
- `ProcessManager`: process handles, cancellation, timeouts, and status transitions.
- `WorktreeService`: per-repo worktree roots, branch naming, cleanup, and artifact retention.
- `ArtifactStore`: capped and redacted stdout/stderr summaries, structured Codex JSONL events, result text, diffs, and review metadata.
- `PolicyEngine`: sandbox, network, approval, auth, allowed-root, and power-mode decisions.

CodexPro should not auto-register generic `read`, `write`, `edit`, or `bash` handlers into this boundary. Those capabilities can exist only through the tool tier policy.

## Workspace Context Layer

CodexPro's workspace layer is one of the highest-value imports. The wrapper should gain:

- active workspace selection from configured allowed roots;
- path guard with realpath checks and symlink escape rejection;
- blocked glob defaults for `.env`, private keys, `.git`, dependency/build output, cache folders, and configured secret paths;
- bounded tree listing;
- bounded file reads with binary and size detection;
- ripgrep-first search with safe fallback;
- git status, diff, and recent log summaries;
- AGENTS chain loading from repo root to target path;
- skill inventory and bounded `SKILL.md` loading by skill name;
- selected-file context bundle export.

The context layer should be read-only except for `.ai-bridge` artifact writes.

## State And Artifacts

State should be separated by purpose:

- config: committed defaults and local overrides;
- profiles: user/workspace startup preferences and connection mode;
- job store: durable job records and process metadata;
- artifacts: bounded job outputs, diffs, and summaries;
- `.ai-bridge`: handoff and context artifacts inside the active repo;
- audit log: metadata-only events with correlation IDs.

Raw prompts, secrets, auth files, full Codex outputs, and local session transcripts must not be logged by default.

## Current Verification

Verified:

- local Streamable HTTP MCP startup and probing against disposable repos;
- real Codex CLI `0.141.0` `codex_plan_job` through MCP;
- current Codex JSONL structured result parsing;
- token-gated auth and tunnel fail-closed behavior in automated tests;
- workspace path guards, blocked globs, symlink escape rejection, and default power-tool denial.

Not yet verified for release:

- real ChatGPT Developer Mode connection;
- real public tunnel connection with token auth;
- real ChatGPT-originated apply-job diff review;
- real ChatGPT-originated resume/interactive continuation.

## Remaining Architecture Work

The current hybrid implementation has the core ChatGPT-facing connector, workspace context, skill discovery/loading, `.ai-bridge` handoff, durable Codex jobs, resume/interactive job starters, public tool metadata, token-gated tunnel startup, and reusable live MCP evals in place.

Remaining work is additive:

- complete the real ChatGPT and tunnel release evals;
- richer auth modes beyond tokenized local/tunnel use if this becomes multi-user;
- deeper schema coverage for future tools as they are added;
- richer interactive ChatGPT card actions beyond the passive result card;
- broader Codex CLI compatibility probes across installed versions;
- CORS policy only if a trusted standalone local UI is added.

## Sources Checked

- CodexPro source at upstream commit `03556103b3dc6de2e67e6e64835a72363c3a71a1`.
- CodexPro npm version `0.28.5`.
- Wrapper source files: `server.py`, `mcp_protocol.py`, `tools.py`, `job_manager.py`, `job_executor.py`, `security.py`, `config.yaml`, and `tests/`.
- OpenAI Developer Mode docs: https://developers.openai.com/api/docs/guides/developer-mode
- OpenAI Apps SDK reference: https://developers.openai.com/apps-sdk/reference
- OpenAI Apps SDK auth docs: https://developers.openai.com/apps-sdk/build/auth
- OpenAI Apps SDK security/privacy docs: https://developers.openai.com/apps-sdk/guides/security-privacy
