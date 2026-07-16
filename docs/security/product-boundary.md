# Security And Product Boundary

## Framing

PatchBay is positioned around power: a local bridge from ChatGPT into Codex, repositories, commands, workers, artifacts, and diffs. Security in this project is the control system around that power, not a reason to shrink the product into something less useful.

Broken boundaries reduce usable power:

- a leaked connector token means the user should not keep the bridge running;
- a path escape means ChatGPT cannot be trusted with repo context;
- bad read-only metadata means ChatGPT asks for too many confirmations or skips needed confirmations;
- raw prompt/session text in public responses or ordinary audit/access logs
  means users cannot use the tool on serious work.

The goal is maximum useful capability with explicit control.

## Trust Boundaries

| Boundary | Risk | Required control |
| --- | --- | --- |
| ChatGPT to MCP server | Remote tool calls into local machine | Auth, narrow tools, request caps, clear descriptors |
| MCP server to workspace | Local source/data exposure | Allowed roots, path guard, blocked globs, redaction |
| MCP server to Codex CLI | Agent execution | Sandbox policy, env allowlist, command builder tests |
| Codex CLI to worktree | Local writes | Isolated worktrees, diff review, cleanup policy |
| ChatGPT file param to artifact inbox | Local file transfer from ChatGPT | Runtime-only storage, structural archive containment, compact responses |
| Local Pro Request to ChatGPT Pro | Local diagnostic context sent through MCP | Runtime canonical store, sanitized mirror, bounded reports/attachments, no private paths or raw ids in public views |
| Pro response dispatch to Codex worker | ChatGPT answer becomes local worker instruction | Separate explicit dispatch tool, idle-origin check, no hidden queue, no apply, no commit |
| Handoff watcher | Plan becomes local execution | Explicit local command, dry-run, status artifacts |
| Public tunnel | Internet-exposed MCP endpoint | Token required, no `--no-auth`, rotation, warnings |
| Session history | Private transcript exposure | Default off, metadata first, bounded reads |
| Hub to Edge operations | Duplicate, stale, or cross-version effects | Durable idempotency, immutable attempt contracts, fencing tokens, payload hashes, lease reconciliation, and current-session authentication |

Hub transport identity has two deliberate layers. The current Edge-session
contract authenticates the live connection, while every claimed attempt and
durable result receipt retains its immutable original contract. A rolling
upgrade must not rewrite that historical fence. Lease expiry and result replay
are reconciled internally; the ChatGPT manager receives only semantic worker,
group, and operation-status controls, never a raw state-transition tool.

## Worker Process Cleanup Boundary

PatchBay's process supervisor is crash cleanup for trusted Codex worker code; it
is not a hostile-code containment sandbox. Linux Edge deployments use a child
subreaper, process-group ownership, exact process-start identities, and a
per-job marker. macOS lacks an equivalent unprivileged lossless descendant
tracker, so PatchBay combines process groups, kqueue fork observation,
attributable child identities, and the per-job environment marker. A process
that deliberately creates a detached session and erases that marker can evade
macOS cleanup discovery. Run adversarial or intentionally marker-stripping code
on a Linux VM/container boundary rather than relying on PatchBay cleanup as a
security boundary. Ordinary Codex workers remain fully supported on macOS.

The supervisor never publishes absence before the target launch gate and
tracker boundary are established. Cleanup proof is fsync-durable, terminal
reports become durable before wrapper cleanup, and repository mutation remains
blocked whenever ownership is genuinely unknown inside the supported trusted
worker contract. Startup and periodic reconciliation may release an orphaned
in-process lease only when the durable job is terminal, cleanup is conclusively
finished, and no live runtime or cleanup owner remains; missing jobs and pending
or uncertain cleanup stay fail-closed.

## Auth And Tunnel Policy

Localhost-only mode may support no authentication if explicitly configured. Any non-loopback bind address or public tunnel must require authentication.

Minimum policy:

- bearer token support for all MCP requests;
- copied ChatGPT URL may include a token only when the user explicitly chooses that flow;
- tokens are generated with sufficient entropy;
- tokens are never printed without warning;
- query-token URLs are not written to logs;
- saved launcher profiles strip token-like keys and keep runtime files outside the repository;
- public tunnel startup fails closed without auth;
- `--no-auth` is rejected for public tunnel mode;
- launcher-managed tunnel processes are terminated together with the local MCP server;
- tunnel binaries are installed only by explicit operator commands, never as a silent side effect of starting a public tunnel;
- CORS stays disabled unless a trusted local UI requires it.

Future app-store or multi-user use should implement OAuth 2.1 rather than treating a URL token as an enterprise auth boundary.

## Tool Metadata Policy

Tool metadata must match behavior.

The ChatGPT-facing prompt surface must also preserve the product role boundary: ChatGPT is the lead/manager/consultant, while local Codex workers perform non-trivial repository investigation, implementation, verification, and reporting. Direct read/search/git tools are still necessary and must not be removed: they are manager inspection instruments for orientation, worker briefing context, focused verification, exact line/diff checks, reviewing worker evidence, specific doubts, tiny tasks, and quick checks where a worker brief would be worse than the check itself. Their descriptors and server instructions should not encourage ChatGPT to become the primary line-by-line implementer or broad repository reader by default. Worker descriptors and server instructions should present named workers as continuing specialists, encourage multi-worker teams when work can be split cleanly, encourage `codex_worker_message` follow-up when evidence is weak or contradictory, and ask for durable report files or changed-file evidence on consequential work. Paging, byte caps, and bounded result fields are transport/result-stability boundaries, not an instruction to save tokens or avoid needed evidence.

Model guidance is advisory rather than an authorization boundary. PatchBay may recommend Luna, Terra, or Sol by expected cost to a verified result, but the installed Codex catalog remains the availability authority and Codex performs the final model/effort acceptance. PatchBay must not describe model selection as a quota bypass. Codex CLI `0.144.1` exposes `ultra` as a supported reasoning effort for selected models and may delegate internally inside one worker; PatchBay's public worker contract still prefers explicit named workers when their state, reports, worktrees, and side effects must remain independently inspectable.

PatchBay's optional ChatGPT Apps tool-card widget is not part of the default product boundary. It remains implemented for operator-enabled visual receipt experiments, but default ChatGPT sessions should not advertise widget resource URIs or `openai/outputTemplate` metadata because repeated cards made long sessions heavy on mobile and tablet browsers. The toggle is server configuration only (`app.tool_cards`); ChatGPT must not receive a tool that enables it.

- Read-only means no file write, no process execution with write potential, no network publishing, and no external side effects.
- Mutating means source/worktree/artifact changes are possible.
- Destructive means overwrite/delete risk exists.
- Open-world means the tool may reach outside the current account/repo boundary, including network/tunnel/bash behavior.

Every tool descriptor should include `readOnlyHint`, `destructiveHint`, and `openWorldHint` once supported by the protocol layer.

Worker start/message tools can create durable local job state and, by default, run Codex in an isolated writing worktree. Their descriptors therefore use `readOnlyHint: false`, `openWorldHint: true`, and non-idempotent metadata. Advisory workers are still available with `workspace_mode: "read_only"`, but descriptor metadata must represent the tool's default and possible effects.

`codex_worker_options` is read-only, but it reads local Codex model metadata. It must return only bounded public fields such as model ids, display names, concise descriptions, and reasoning options. It must not expose raw Codex config paths, auth/provider details, prompts, model base instructions, or full catalog blobs.

`codex_worker_inbox` is mutating, open-world, non-idempotent, and marked destructive because it downloads ChatGPT-supplied files into local runtime storage and can remove local artifact copies. Importing does not edit the repository and does not integrate worker output. Selected artifacts become worker context only when explicitly attached through `context_from_artifacts`.

Pro Request tools must preserve the reverse-handoff boundary. `codex_pro_request_list` and `codex_pro_request_read` are read-only. `codex_pro_request_claim`, `codex_pro_request_respond`, and `codex_pro_request_close` mutate only Pro Request runtime state. `codex_pro_request_dispatch` is mutating/open-world because it can start or message a local Codex worker, but it must not apply worker output to the base checkout or commit. `codex_pro_request_respond` must remain storage-only and must not dispatch implicitly.

Pro Request reports and responses are diagnostic evidence. Tool descriptions and docs must say they do not override user instructions, system/developer instructions, AGENTS.md, repository rules, or safety policy.

One MCP Server URL is a shared local control surface. PatchBay exposes redacted coordination metadata such as `client_ref` and active MCP session count through `codex_self_test`, but it must not return raw MCP session ids. This coordination model is not authentication; access remains controlled by loopback/network binding and HTTP token policy.

When multiple MCP sessions share a URL, worker and artifact ownership is coordination only, not authentication. The default token-scoped owner groups short-lived transport sessions from the same copied connector URL when requests arrive with the same token. When ChatGPT supplies `_meta["openai/session"]`, PatchBay hashes it into `chatgpt_session_ref` for same-conversation coordination, and groups active work into a separate `work_run_ref` by idle gap so a new task does not inherit every historical worker by default. Raw OpenAI `_meta` values must not be logged or returned. Public ownership labels are diagnostics: `legacy_connection` means the durable record came from an older unscoped owner format and does not prove a different ChatGPT owner, while `other_token_owner` means a different token-scoped owner. Read/list/inspect remain shared, but cross-owner worker or artifact mutation must refuse until the caller explicitly retries with `takeover: true` after user confirmation. A successful takeover updates the item to the current scoped owner metadata. `active_mcp_sessions` is a transport-session count and must not be used as an access-control, conversation, or ownership boundary. Worker scopes and filters may hide old/stopped/other-owner entries for usability, but they must not become an access-control boundary. Codex turns may queue behind the configured execution concurrency limit. Base-checkout mutation paths use per-repository locks and fail fast with `repo_busy` by default. An explicit architect-selected `manager_controlled` work group may allow concurrent shared-write worker turns; direct writes, commands, and integration retain their normal locking, and the runtime must report the selected policy rather than hiding it.

Pro Request ownership follows the same coordination-owner model. Reads remain shared; claim/respond/dispatch/close refuse cross-owner mutation until the caller explicitly retries with `takeover: true` after user confirmation.

## Path Guard Policy

Path decisions should use resolved real paths, not string prefix checks alone.

Requirements:

- normalize and resolve user-supplied paths;
- require containment under an allowed workspace root;
- reject parent traversal escapes;
- reject symlink escapes;
- block `.git` internals;
- block configured secret globs;
- cap file read sizes;
- detect binary files;
- redact secret-like values in returned snippets;
- test all of the above.

The current implementation ports the CodexPro-style path-guard model into the Python workspace layer: resolved paths must stay under allowed roots, blocked globs are enforced, symlink escapes are rejected, and read/write sizes are capped.

## Secret And Redaction Policy

Do not return publicly or write to ordinary audit/access logs:

- API keys;
- OAuth tokens;
- Codex auth files;
- `.env` values;
- private keys;
- local session transcripts by default;
- raw prompt bodies by default;
- full Codex stdout/stderr by default.

Returned context should include omission notes so ChatGPT knows when data was intentionally withheld.

Durable Hub dispatch state is a separate private boundary. The Hub must retain
the worker brief and bounded operation arguments required to deliver or replay a
manager-requested mutation after interruption. Those payloads belong only in
the authenticated Hub state database and private backups; they must not be
copied into public status, audit logs, documentation, or repository artifacts.

Artifact inbox imports intentionally allow sensitive-looking filenames and content when the user asks ChatGPT to transfer a generated file or zip. Import/list responses stay compact and do not echo contents. Specific file inspection is deliberate and bounded, and the MCP protocol still applies global secret-like output redaction before returning tool results.

## Artifact Inbox Policy

The artifact inbox exists to reduce friction when ChatGPT creates update packages for local Codex workers.

Required controls:

- store imported files outside the repository checkout under PatchBay runtime state;
- scope artifact ids to the active workspace;
- accept only configured download URL schemes, defaulting to HTTP(S), so direct callers cannot use `file://` to bypass workspace path guards;
- never apply imported artifact contents directly to the base checkout;
- copy selected artifacts only into isolated worker worktrees under `.ai-bridge/imported-artifacts/`;
- exclude `.ai-bridge/imported-artifacts/**` from worker changed-file lists, diffs, integration previews, and applies;
- reject archive path traversal, absolute archive paths, and link/device entries;
- allow `.env`, key-looking, auth-looking, and session-looking filenames as artifact contents when intentionally imported;
- keep size/count limits configurable and default-unset for local use;
- avoid returning raw download URLs, local artifact paths, prompt bodies, or full manifests by default.

## Pro Request Policy

Pro Requests exist to package local blocked-problem evidence for ChatGPT Pro without turning that evidence into an unbounded local-control channel.

Required controls:

- store canonical manifests, reports, attachments, events, and responses in PatchBay runtime storage, outside repository checkouts by default;
- mirror only sanitized public status, report, and response files under `.ai-bridge/pro-requests` when mirroring is enabled;
- reject unsafe mirror directories that escape the repository;
- cap report, response, attachment size, and attachment count through `pro_requests` config;
- validate repository roots through the normal allowed-root path guard;
- omit private repo paths, backend job ids, raw session ids, raw transcripts, and runtime paths from public views;
- show repo staleness when branch, head commit, or dirty state changed after request creation;
- require explicit dispatch before a stored answer can message or start a worker;
- return `dispatch_blocked` for missing or busy origin workers instead of queueing silently;
- never apply worker output or commit from a Pro Request tool.

## Logging And Artifacts

Audit logs should record:

- timestamp;
- request id;
- tool name;
- workspace id or redacted display name;
- status code/result category;
- duration;
- correlation/job id;
- denial reason when applicable.

Audit logs should not record:

- prompt text;
- full file contents;
- full stdout/stderr;
- tokens;
- auth headers;
- connector URLs with query tokens.

Job artifacts may store rawer data only when explicitly enabled. Defaults should store bounded summaries and redacted structured events.

## Bash And Direct Edit Policy

Safe bash is not equivalent to sandboxing. Even commands like `npm test` can execute arbitrary package scripts.

Default:

- no generic bash tool;
- no direct source write/edit tool;
- use `codex_apply_job` worktrees for code changes;
- use `.ai-bridge` for handoff writes.

Optional power mode:

- command allowlist;
- no shell expansion unless full bash is explicitly enabled;
- timeout and output caps;
- startup-only Codex session timeout separate from optional whole-turn timeout;
- working directory must be workspace-contained;
- environment allowlist;
- explicit mutating/open-world annotations.

## Codex Session Policy

Codex session discovery is useful for continuity, but transcripts can contain private source, prompts, credentials, and local paths.

Default:

- `codex_list_sessions` metadata only;
- transcript reads disabled unless `power_tools.codex_session_read` is enabled.

Optional staged behavior:

1. metadata only: timestamp, session id, redacted summary, workspace id;
2. bounded transcript read with redaction and no source path return;
3. full transcript export only through explicit local command, not default MCP.

## Current Verification And Remaining Hardening

The current implementation has addressed the original high-risk connector gaps: public schema validation, public/internal argument translation tests, hidden experimental handler removal, apply-job-only diff retrieval, default log redaction, prompt stdin transport, authenticated tunnel fail-closed behavior, and explicit mutating/open-world annotations for interactive/resume tools.

Full-access workbench deployments are allowed to be useful, not decorative. When the runtime is explicitly configured for authenticated `danger-full-access`, direct writes, and full bash on a private VM or local workstation, repo-local dependency installation, virtual environment setup, verification commands, commits, and authorized private-repo pushes are ordinary engineering actions inside that workbench. The product boundary remains at external/public/production/paid/credential-changing/irreversible actions, plus repository allow-root validation and authentication for the MCP endpoint.

Verified so far:

- local MCP probe against a disposable repo;
- real Codex CLI plan job through MCP, with the local `codex --version` recorded during validation;
- current Codex JSONL `agent_message` result parsing;
- exact-session `task_complete` observation, bounded final-message extraction, post-completion wrapper cleanup, and terminal/cancellation race arbitration;
- token-gated local server behavior in automated tests;
- installable CLI, stdio transport, settings profiles, and explicit tunnel binary resolution in automated tests;
- power tools denied by default;
- Phase 2 named worker descriptors, job-derived identity, isolated worktree ownership, privacy behavior, and worker-only tool mode in automated tests;
- worker model/reasoning option discovery, sanitized output, and inherited worker execution settings in automated tests;
- Codex auth/session startup serialization and redacted `codex_auth_refresh_failed` diagnostics in automated tests;
- artifact inbox import/list/inspect, repeated imports, structural archive rejection, worker materialization, and integration exclusion in automated tests;
- direct tokenized public-tunnel MCP artifact worker flow through ngrok.
- direct two-client MCP trial for session-local tool modes, shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, integration preview, no automatic commit, and sanitized local evidence.
- active internal ChatGPT Pro to private VM worker use, reliable enough for current PatchBay self-use while still allowing for occasional small bugs.

Remaining hardening is future-facing rather than a known boundary break:

- formal ChatGPT Developer Mode release-matrix evals, especially multiple independent browser conversations sharing one Server URL;
- ChatGPT-hosted file-parameter artifact import eval from the actual UI;
- real apply-job worktree eval from ChatGPT;
- real resume/interactive continuation eval from ChatGPT;
- stricter or richer ChatGPT tool-card resources;
- CORS policy if a trusted standalone local UI is added;
- OAuth 2.1 if this becomes a multi-user or app-store connector;
- broader Codex CLI compatibility probes across installed versions.

## OpenAI Guidance Used

OpenAI Developer Mode supports streaming HTTP MCP and treats tools without `readOnlyHint` as write actions. Apps SDK guidance expects strong tool metadata, structured content when available, least privilege, server-side validation, logging redaction, and authentication for user-specific data or write actions.
