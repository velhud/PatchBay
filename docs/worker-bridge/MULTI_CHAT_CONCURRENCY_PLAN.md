# Multi-Chat Concurrency Plan

Status: implemented design record. The runtime now supports session-local tool modes, safe shared-server coordination metadata, worker/artifact ownership flags, explicit takeover for cross-owner mutation, per-repository mutation locks, pending-plus-running job admission, and a direct multi-client MCP trial. The original issue analysis remains below as rationale and regression guidance.

## Implementation Result

Implemented behavior:

- one copied Server URL is one shared local state surface for all connected ChatGPT conversations or MCP clients;
- `codex_self_test` returns safe coordination metadata such as `client_ref`, `active_mcp_sessions`, and `shared_server` without exposing raw MCP session ids;
- `codex_tool_mode_switch` is session-local for MCP sessions, so one conversation broadening to `full` does not change another conversation's effective catalog;
- workers, jobs, and artifacts carry private owner metadata and expose only safe session-relative ownership fields;
- read/list/inspect remain shared, while cross-owner worker/artifact mutation refuses until the caller explicitly retries with `takeover: true` after user confirmation;
- base-checkout mutation paths use per-repository locks and return `repo_busy` instead of queueing hidden writes;
- global job admission counts pending plus running work;
- `scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json` verifies the shared-server path on disposable repos.

The remaining unverified item is real ChatGPT Developer Mode behavior across multiple browser conversations. That remains a release validation item because ChatGPT UI/tool-catalog refresh behavior is external to this repository.

## Purpose

This plan covers the eight multi-client risks found after the first real ChatGPT worker trial. The scenario is one local `patchbay` server exposed to more than one ChatGPT conversation or MCP client at the same time. The goal is to prevent accidental cross-chat interference while preserving the product philosophy:

- ChatGPT manages Codex through natural-language workers, not a low-level queue or ERP.
- PatchBay remains the local control plane for exact mechanics: sessions, worker state, worktrees, artifacts, locks, previews, and apply.
- Read/list/inspect stay easy; mutation becomes explicit when ownership or repository contention is ambiguous.
- Isolated worker worktrees remain the default. Base-checkout mutation remains explicit and serialized.
- Public outputs must not expose raw MCP session ids, private local paths, raw prompts, job ids, session ids, branch names, logs, tokens, or secrets.

## Vendor And Best-Practice Inputs

- OpenAI Apps SDK says MCP server `initialize.instructions` should carry cross-tool guidance such as required sequences, shared limits, and relationships between tools, and that the first 512 characters should stand alone. That means the shared-server and ownership rules belong in the server instructions, not only in external docs: <https://developers.openai.com/apps-sdk/build/mcp-server#add-server-instructions-for-cross-tool-guidance>.
- OpenAI Apps SDK requires accurate tool impact hints. Read-only tools must only retrieve/compute; mutating tools need write/open-world/destructive hints that match real behavior: <https://developers.openai.com/apps-sdk/build/mcp-server#tool-annotations-and-elicitation>.
- OpenAI Developer Mode metadata is refreshed through the connector refresh flow, and published apps snapshot tool names, descriptions, schemas, annotations, `_meta`, resources, and server instructions as a versioned contract. Concurrency-related prompt and schema changes must therefore be backward-compatible and documented: <https://developers.openai.com/apps-sdk/deploy/connect-chatgpt#refreshing-metadata> and <https://developers.openai.com/apps-sdk/deploy/submission#how-published-app-versions-work>.
- OpenAI state guidance says authoritative business state should live on the server/backend, with clients rendering snapshots after tool calls. For this app, worker ownership, artifact ownership, and repo locks must be server state, not assumptions kept only inside a ChatGPT conversation: <https://developers.openai.com/apps-sdk/build/state-management#1-business-state-authoritative>.
- OpenAI troubleshooting guidance says wrong tool choice should be fixed with clearer metadata, disallowed scenarios, or purpose-built tools. Shared-server warnings should be visible in descriptions/results, not hidden in long prose: <https://developers.openai.com/apps-sdk/deploy/troubleshooting#discovery-and-entry-point-issues>.
- MCP Streamable HTTP supports server-minted `MCP-Session-Id` values; clients must send the id on later requests, and unknown/expired sessions should receive a clear session error. That gives us a natural per-connection coordination key, but the id must be treated as sensitive and never echoed publicly: <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#session-management>.
- Python `asyncio.Lock` is the right in-process primitive for serializing async access to shared resources inside one server process. It is fair, but not thread-safe or process-wide: <https://docs.python.org/3/library/asyncio-sync.html#lock>.
- Python `fcntl.flock` can provide an advisory file lock for process-wide local serialization on POSIX systems when the server is ever run with more than one worker process: <https://docs.python.org/3/library/fcntl.html#fcntl.flock>.
- Uvicorn can run multiple worker processes with `--workers`; separate processes do not share in-memory locks. Any future multi-process launch needs file locks or another external lock provider: <https://uvicorn.dev/deployment/#built-in>.
- Git worktrees are the correct isolation primitive for independent worker edits, because Git supports multiple linked working trees for the same repository: <https://git-scm.com/docs/git-worktree#_description>.
- `git apply --check` is the correct preview mechanism before applying a patch, because it checks applicability without applying it. That supports the existing preview-before-integrate philosophy: <https://git-scm.com/docs/git-apply#Documentation/git-apply.txt---check>.

## Repo Principles Preserved

- `AGENTS.md:20-35` requires no secrets/local identifiers, read-only defaults, localhost-first control, authentication before network exposure, clear mutating tool metadata, and no release-readiness claims before real ChatGPT validation.
- `AGENTS.md:105-118` requires the natural-language management model: read-only preview, explicit apply, no automatic commit, preserved worker worktrees, human worker names, and stateful worker instructions.
- `README.md:60-73` defines the product as a Streamable HTTP MCP bridge with durable worker facade, isolation, power modes, and token-gated connector UX.
- `README.md` says public tunnel runs require a token, first ChatGPT validation should use `--tool-mode worker`, and tool mode switching is session-local for MCP sessions.
- `README.md:261-278` documents the canonical worker tool surface and says worker mode hides low-level job/session controls while workers derive from persisted jobs/sessions.
- `docs/worker-bridge/05_END_TO_END_ALGORITHMS.md:5-19` says worker start creates a durable `interactive` job with private worker identity and returns a public pointer without backend internals.
- `docs/worker-bridge/05_END_TO_END_ALGORITHMS.md:55-69` says busy workers return `accepted: false`; PatchBay intentionally does not queue messages.
- Worker continuity must stay name-based without exposing backend job/session ids, local paths, branch names, logs, or transcripts.
- Isolated writing worktrees remain the default for writing workers, including separate worktrees for multiple writing workers.
- Multi-worker coordination is managed by ChatGPT through PatchBay's bounded context tools, not by a generic message bus or deterministic role engine.
- `docs/security/product-boundary.md:51-64` requires tool metadata to match behavior and classifies worker start/message/inbox as mutating/open-world where appropriate.

## Implemented Shared Design

Before solving individual issues, implement one common request context and one common lock/ownership language.

1. Add an internal `RequestContext` value passed from HTTP transport to protocol to tool handlers:
   - `transport_session_id`: raw MCP session id, private.
   - `client_ref`: short HMAC or salted hash derived from the session id, safe to log and return.
   - `client_label`: optional human label supplied by the caller through a low-friction tool argument or self-test/open-workspace call.
   - `tool_mode`: effective tool mode for this request.
2. Store raw session id only in private runtime/job metadata if needed for comparison. Public responses should expose only `owned_by_current_client`, optional `owner_label`, and a generic note such as `owned_by_another_connection`.
3. Prefer soft ownership over security claims. The MCP bearer/query token authenticates access to the server; owner context prevents accidental cross-chat interference. It is not a user identity system.
4. Permit read/list/inspect across visible state. Require explicit takeover/override only before mutating a worker, artifact, or base checkout that appears to belong to another current or previous connection.
5. Keep no queue. If a worker or repository mutation lock is busy, return a clear refusal with next safe actions.
6. Keep all changes backward-compatible. Add optional fields and optional override flags; do not rename existing tools.

## Issue 1: Current Shared-Server Rule Is Not Explicit

### Problem

Every ChatGPT conversation connected to the same local server sees the same runtime state, but the current ChatGPT-facing instructions do not state that plainly. A second conversation may assume it has a private app instance and mutate workers, artifacts, or base checkout state created by another conversation.

### Repo Evidence

- `src/patchbay/server.py:88-96` creates one process-wide `JobManager`, `JobExecutor`, `ToolHandler`, `MCPProtocol`, and `sessions` dict.
- `src/patchbay/server.py:221-240` creates or updates MCP transport sessions, but those sessions currently only live at the HTTP layer.
- `src/patchbay/protocol/mcp.py:15-31` provides server instructions, but does not warn that multiple ChatGPT chats share one local server.
- `README.md:79-84` shows ChatGPT entering through one connector layer into one local runtime.
- `docs/user/chatgpt-instructions.md:27-68` explains tool mode and worker flow, but does not define multi-chat sharing behavior.

### Vendor Guidance

OpenAI says cross-tool rules belong in server instructions, and server state should be authoritative. MCP gives each connection a session id. Therefore shared-server semantics should be declared by the server and reflected in tool results, not assumed by a single ChatGPT conversation.

### Resolution Steps

1. Update `SERVER_INSTRUCTIONS` in `src/patchbay/protocol/mcp.py` to state in the first short block that this is a shared local server for every client using the same URL.
2. Add concise instructions: read/list tools can see shared state; mutating another connection's worker requires explicit takeover; base-checkout writes/integration are serialized per repository.
3. Add the same operator-facing rule to `README.md`, `docs/user/chatgpt-instructions.md`, `docs/reference/public-tool-surface.md`, and `docs/security/product-boundary.md`, because `AGENTS.md:30-32` requires docs updates when connector behavior or ChatGPT prompt surface changes.
4. Extend `codex_self_test` or `codex_open_workspace` output to include `shared_server: true`, `client_ref`, `active_mcp_sessions`, and a one-sentence `coordination_note`.
5. Keep the note short and action-oriented so it does not crowd out the worker-first instructions.

### Philosophy Check

This preserves the natural-language worker facade. It adds awareness, not a workflow manager. It also supports local control and privacy because no raw session ids or local paths are exposed.

### Validation

- Unit test `initialize` instructions include the shared-server rule near the beginning.
- Unit test `codex_self_test` returns a public `client_ref` and no raw session id.
- Direct MCP two-client test verifies both clients see the same shared note.

## Issue 2: Tool Mode Switching Is Global

### Problem

Before the fix, `codex_tool_mode_switch` mutated process-wide config. If one ChatGPT chat broadened from `worker` to `full`, every other connected chat that re-listed tools could inherit the broader surface. That was surprising and could expose low-level controls to a chat that expected a smaller worker-first catalog.

### Repo Evidence

- Pre-implementation evidence: `src/patchbay/protocol/mcp.py` computed tool availability from shared config.
- `src/patchbay/protocol/mcp.py:2016-2039` reports inventory for the shared configured mode.
- `src/patchbay/protocol/mcp.py:2042-2061` writes `config["app"]["tool_mode"]`, changing mode for the whole process.
- `src/patchbay/protocol/mcp.py:2146-2159` lists and validates tools against the shared config.
- `tests/test_protocol_initialize.py` currently treats mode switching as one protocol-wide behavior.
- Current `README.md` and `docs/user/chatgpt-instructions.md` describe the switch as session-local for MCP sessions, with a ChatGPT connector-refresh caveat.

### Vendor Guidance

OpenAI treats tool metadata as a contract and tells operators to refresh ChatGPT metadata after tool changes. Since ChatGPT may cache the visible tool catalog for a conversation, a runtime switch cannot be relied on to instantly change the UI, and it should not surprise other clients.

### Resolution Steps

1. Keep checked-in default behavior unchanged for compatibility, but introduce an internal per-session effective mode:
   - default from config at session creation;
   - override stored under `sessions[session_id]["tool_mode"]`.
2. Change `configured_tool_mode(config)` usage in protocol handlers to accept an optional `RequestContext` or explicit mode.
3. Change `tools/list` and `tools/call` to resolve availability from the current request's mode.
4. Change `codex_tool_mode_info` to report both `default_mode` and `current_session_mode`.
5. Change `codex_tool_mode_switch` to update only the current MCP session unless no session context exists, in which case it returns a clear error or uses a legacy internal process-wide path only for non-HTTP tests.
6. Keep `TOOL_MODE_REFRESH_NOTE`, but rewrite it to say a session-local switch affects the server's next `tools/list` for this same MCP session and ChatGPT may still need connector refresh.
7. Add a config escape hatch only if needed: `app.tool_mode_switch_scope: session | process`, defaulting to `session`. The public docs should recommend `session`.

### Philosophy Check

This strengthens the worker-first surface. It does not add tools or a role engine. It reduces accidental power-surface expansion while leaving explicit power-user control available.

### Validation

- Two MCP sessions start in `worker`.
- Session A switches to `full`; Session B still receives `worker` tools.
- Session A can call a `full`-only tool after re-listing; Session B receives "tool unavailable" for the same call.
- Existing tests for mode inventory are updated to cover `current_session_mode`.

## Issue 3: MCP Session Id Is Not Passed Into Tool Handling

### Problem

Before the fix, the server created MCP transport sessions but the id stopped at the HTTP handler. The protocol and tool layers could not know which ChatGPT conversation made a call, so they could not provide session-local mode, ownership labels, or cross-chat mutation checks.

### Repo Evidence

- `src/patchbay/server.py:221-240` creates/updates `session_id`.
- `src/patchbay/server.py:277-289` calls `mcp_protocol.handle_message(message)` without passing the session.
- `src/patchbay/protocol/mcp.py:2083-2180` handles all JSON-RPC messages without request context.
- `src/patchbay/tools/handler.py:79-132` dispatches tools without request context.
- `src/patchbay/tools/handler.py:191-243` starts/messages/integrates workers without caller context.

### Vendor Guidance

MCP Streamable HTTP sessions are designed for logically related interactions. OpenAI state guidance says server-side state is authoritative. Passing session context is therefore the proper way to make tool calls state-aware without asking ChatGPT to manually send session ids.

### Resolution Steps

1. Add a small internal module, for example `src/patchbay/protocol/context.py`, with `RequestContext`.
2. In `server.py`, create `RequestContext` after session validation:
   - support both `Mcp-Session-Id` and `MCP-Session-Id` header spellings because HTTP headers are case-insensitive but specs/docs use different casing;
   - derive `client_ref` from a server-local salt and session id;
   - never include the raw session id in ordinary outputs.
3. Change `MCPProtocol.handle_message(message, context=None)`.
4. Change `_handle_initialize`, `_handle_tools_list`, and `_handle_tools_call` to accept context.
5. Change `ToolHandler.handle_tool_call(tool_name, arguments, context=None)`.
6. Pass context into worker runtime, artifact store, direct write/edit, command execution metadata, and job creation sites where needed.
7. Keep tests and direct internal callers working with `context=None` by constructing an anonymous request context.

### Philosophy Check

This is plumbing, not product complexity. It keeps ChatGPT operating by human worker name and avoids requiring the model to manage raw session ids.

### Validation

- Protocol tests cover HTTP-session context and anonymous context.
- Redaction tests prove raw session ids are not present in public JSON results, logs configured for public output, or worker views.
- Existing non-HTTP unit tests still pass.

## Issue 4: Workers, Jobs, And Artifacts Needed Owner Metadata

### Problem

Worker identity is durable, but caller ownership is not. If multiple chats create workers or import artifacts in the same repo, PatchBay cannot distinguish "my worker" from "another connection's worker" except by human name.

### Repo Evidence

- `src/patchbay/workers/runtime.py:74-130` creates worker id, workspace, options, and job, but no owner fields.
- `src/patchbay/jobs/manager.py:106-147` creates jobs with `options`, but has no owner argument.
- `src/patchbay/jobs/manager.py:250-255` persists updated job options, so private owner metadata can reuse the existing durable job record.
- `src/patchbay/artifacts.py:35-97` imports artifacts under a workspace id, but not a caller owner.
- `src/patchbay/artifacts.py:102-148` lists/cleans artifacts by workspace only.
- `docs/worker-bridge/05_END_TO_END_ALGORITHMS.md:26-40` says public worker view derives from durable jobs and returns public fields only.

### Vendor Guidance

OpenAI state guidance says authoritative state belongs on the server. MCP session ids can identify a transport connection. OpenAI security guidance says user-visible outputs must not embed secrets, tokens, or sensitive implementation details.

### Resolution Steps

1. Add private owner option keys to worker/job metadata:
   - `_mcp_owner_session_hash`;
   - `_mcp_owner_client_ref`;
   - `_mcp_owner_label`;
   - `_mcp_owner_created_at`;
   - `_mcp_owner_last_seen_at`.
2. Use a salted/HMAC hash rather than raw session id where practical. If raw session id is needed for exact comparison in memory, keep it in the process session map, not persisted public output.
3. Stamp `codex_worker_start`, worker continuation jobs, `codex_plan_job`, `codex_apply_job`, `codex_resume`, `codex_interactive`, and `codex_interactive_reply` with owner metadata.
4. Add similar private owner metadata to artifact records in `ArtifactStore.import_file`.
5. Public worker/artifact views may include:
   - `owned_by_current_client: true | false | unknown`;
   - `owner_label` only if caller supplied a non-sensitive label;
   - `ownership_note` when an action needs takeover.
6. Do not hide other workers by default. The app is a local operator tool, not multi-tenant SaaS.
7. After server restart, treat old owner as `unknown_previous_connection` unless the same durable client token design is explicitly added later. Require takeover before mutation if the caller does not match the stored owner.

### Philosophy Check

This reuses existing durable job/artifact state. It does not create a separate worker database, message bus, or deterministic role system. It protects the human operator from accidental cross-chat edits while keeping local authority with the user.

### Validation

- Worker start stores owner metadata in private job options.
- Worker list/inspect never returns raw owner session/hash fields.
- Artifact import/list/inspect marks current ownership without leaking raw ids.
- Durable reload preserves enough owner metadata to require explicit takeover after restart.

## Issue 5: Cross-Chat Mutation Needs Explicit Takeover

### Problem

Before the fix, any connected chat that could name a worker could message, stop, cleanup, or integrate it. That was useful for intentional collaboration but dangerous if several conversations were open.

### Repo Evidence

- `src/patchbay/workers/runtime.py:144-175` resolves and messages a worker without owner checks.
- `src/patchbay/workers/runtime.py:358-410` previews and applies integration without owner checks.
- `src/patchbay/tools/handler.py:245-251` stops a worker without owner checks.
- `src/patchbay/artifacts.py:136-148` cleans up an artifact without owner checks.
- Worker continuity must not expose backend ids, which means ownership checks must still allow human-name operation.

### Vendor Guidance

OpenAI troubleshooting guidance recommends clarifying disallowed scenarios in tool descriptions when the model might pick the wrong control. OpenAI tool annotation guidance requires mutating tools to describe real side effects.

### Resolution Steps

1. Add optional arguments to mutating worker/artifact tools:
   - `takeover: boolean`, default `false`;
   - `takeover_reason: string`, optional and bounded.
2. Apply owner checks to:
   - `codex_worker_message`;
   - `codex_worker_integrate`;
   - `codex_worker_stop`;
   - `codex_worker_inbox(action="cleanup")`;
   - later, any artifact operation that can mutate local artifact state.
3. Permit read/list/inspect without takeover.
4. If owner differs and `takeover` is false, return:
   - `accepted: false` or `applied: false`;
   - `owned_by_current_client: false`;
   - `required_action: "call again with takeover=true after user confirms this is intentional"`;
   - no raw session ids.
5. If `takeover=true`, update owner metadata on the new durable job or artifact record and include a concise public note.
6. Update tool descriptions to say "Use takeover only when the user explicitly wants this chat to take control of a worker/artifact created by another connection."
7. Do not require takeover for same-owner continuation.

### Philosophy Check

This keeps ChatGPT in control through normal worker tools and human names. It adds one explicit safety confirmation rather than a queue, role system, or permission matrix.

### Validation

- Two sessions: Session A starts worker; Session B can list/inspect it.
- Session B message without takeover refuses cleanly.
- Session B message with takeover succeeds and updates owner metadata.
- Session A later sees `owned_by_current_client: false` and needs takeover to mutate.
- Stop and artifact cleanup follow the same rule.

## Issue 6: Base-Repo Mutations Needed Per-Repo Locks

### Problem

Isolated worker worktrees can run in parallel, but base checkout mutations cannot safely overlap. Integration, direct write/edit, shared-write workers, and full-power commands can race each other if multiple chats act on the same repository.

### Repo Evidence

- `src/patchbay/workers/runtime.py:358-410` runs `git apply` into the base repo without a repo mutation lock.
- `src/patchbay/workers/runtime.py:1188-1280` checks dirty base and `git apply --check`, but another request can change the repo between preview and apply.
- `src/patchbay/tools/handler.py:327-337` calls direct file write/edit immediately.
- `src/patchbay/workspace/context.py:732-814` performs direct writes/edits to the base checkout.
- `src/patchbay/workers/runtime.py:735-795` executes `shared_write` and `read_only` workers in the base repo path rather than an isolated worktree.
- `README.md:332-341` documents optional direct write/edit/bash power tools.

### Vendor Guidance

Python `asyncio.Lock` handles in-process serialization. Uvicorn can run multiple processes, so file locks are needed if this server is ever launched with more than one process. Git docs support using worktrees for isolated work; Git apply docs support preview, but preview and apply need to happen under one mutation lock to close the race.

### Resolution Steps

1. Add `RepoMutationLockManager` keyed by normalized base repo path.
2. In single-process mode, use `asyncio.Lock` and `async with`.
3. Add a future-compatible lock provider that can use `fcntl.flock` on lock files under the runtime directory, for example `runtime/locks/repo_<hash>.lock`.
4. Lock these operations:
   - `codex_worker_integrate`;
   - `codex_write_file`;
   - `codex_edit_file`;
   - `codex_run_command` when command mode can write;
   - starting or messaging a `shared_write` worker, with a job-owned lease that releases on terminal state;
   - low-level apply/resume/interactive jobs when their options run directly against the base checkout.
5. Keep isolated worktree worker start/message outside the base mutation lock.
6. For `codex_worker_integrate`, acquire the lock before recomputing integration preview, then apply under the same lock.
7. If the lock is held, return a refusal with `repo_busy: true`, the safe public operation category, and next actions. Do not queue.
8. Ensure lock output does not expose local paths. Use repo display name or allowed workspace label.

### Philosophy Check

This preserves parallel isolated workers while making base mutation exact and explicit. It does not create a generic scheduler; it is a narrow critical-section guard around concrete filesystem/git writes.

### Validation

- Two integrations against same repo: one succeeds or holds lock; the other refuses cleanly.
- Direct write during integration refuses or waits only if an explicit short wait is configured.
- Isolated worker start is not blocked by another integration.
- Multi-process smoke test, if supported, verifies file lock behavior or marks multi-process mode unsupported.

## Issue 7: Global Concurrency Limit Needed Pending-Plus-Running Admission

### Problem

Before the fix, the global job limit counted only `RUNNING` jobs. Multiple pending jobs could be created before the executor marked them running. With multiple clients, this could over-admit work even when `max_concurrent_jobs` was set to one.

### Repo Evidence

- `src/patchbay/jobs/manager.py:119-124` counts only `JobState.RUNNING`.
- `src/patchbay/jobs/manager.py:140-147` creates new jobs in `JobState.PENDING`.
- `src/patchbay/tools/handler.py:440-441`, `470-471`, `729-730`, `754-755`, and `784-785` create jobs and schedule execution asynchronously.
- `src/patchbay/workers/runtime.py:121-130` creates a worker job then schedules execution asynchronously.
- `docs/worker-bridge/10_DECISIONS_RISKS_AND_DEFERRED.md` already prefers reusing global concurrency limits over a second scheduler.

### Vendor Guidance

OpenAI server-instruction guidance supports clearly documented shared rate/concurrency limits. Python locks can protect the count and job creation critical section. OpenAI state guidance says this should be server-authoritative, not left to clients.

### Resolution Steps

1. Change admission counting from `RUNNING` to `PENDING + RUNNING`.
2. Protect `JobManager.create_job` with a manager-level lock so two simultaneous requests cannot both pass the count check.
3. Return a clear public error when the limit is reached:
   - `Maximum active jobs (N) reached; active includes pending and running jobs. Inspect or wait before starting another worker.`
4. Keep no queue. The caller should retry after a worker/job reaches terminal state.
5. Update status output to show `active_jobs = pending + running`.
6. Update docs where max concurrency is described.

### Philosophy Check

This follows the existing "no message queue" decision. It is a resource guard, not a scheduler or ERP.

### Validation

- Unit test creates one pending job and proves a second job is refused at limit one.
- Race test launches two job creations concurrently and proves only one succeeds.
- Worker start rollback still removes a prepared isolated worktree when admission fails.

## Issue 8: Multi-Client Tests Were Missing

### Problem

Before the fix, the test suite validated single-client worker lifecycle and direct MCP trials, but it did not prove multiple MCP sessions could operate safely against one server process.

### Repo Evidence

- `scripts/real_mcp_worker_trial.py` validates direct MCP and worker safety flows, but its client flow is single-session oriented.
- `scripts/manual_mcp_smoke.py:62-112` stores and reuses one `Mcp-Session-Id`.
- `tests/test_protocol_initialize.py` validates initialize/tools/list/tool-mode behavior but not two simultaneous sessions with different modes.
- `tests/test_worker_runtime.py`, `tests/test_worker_integration.py`, `tests/test_worker_coordination.py`, and `tests/test_worker_artifacts.py` cover worker runtime semantics but not cross-session ownership.
- `docs/security/product-boundary.md:194-204` lists remaining real ChatGPT and broader compatibility hardening.

### Vendor Guidance

OpenAI says to retest updated flows after connector metadata changes and to collect logs/tool traces for discovery and tool-selection issues. MCP sessions are an official protocol feature, so tests should use actual HTTP headers rather than only direct class calls.

### Resolution Steps

1. Add unit-level tests for `RequestContext`, owner metadata, session-local mode, and repo lock manager.
2. Add protocol tests with two session contexts:
   - separate `tools/list` mode after one session switches;
   - raw session ids absent from responses.
3. Add FastAPI/TestClient or direct HTTP tests that perform two `initialize` calls and then tool calls with two `MCP-Session-Id` headers.
4. Add worker ownership tests:
   - A starts worker;
   - B lists and inspects;
   - B mutate without takeover refuses;
   - B mutate with takeover succeeds.
5. Add repo-lock tests:
   - integration/direct write conflict;
   - shared-write worker lock refusal;
   - isolated worker parallelism unaffected.
6. Extend `scripts/real_mcp_worker_trial.py` with a `--multi-client` scenario for disposable repos.
7. Add a validation report template section for "multi-chat concurrency".
8. Do not add real ChatGPT Developer Mode multi-chat as a required unit test. Keep it as a release gate/manual eval because ChatGPT UI behavior and connector refresh timing are external.

### Philosophy Check

Testing proves PatchBay remains simple. The tests should verify refusals and explicit takeover rather than expecting a queue, automatic merge, or hidden coordination service.

### Validation

- `python -m compileall src scripts tests`
- `python -m pytest tests -q`
- `python scripts/live_mcp_eval.py --json`
- `python scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker`
- Manual ChatGPT Developer Mode retest after metadata refresh, recorded under `validation-reports/`.

## Recommended Implementation Order

1. Add `RequestContext` and pass it through HTTP, protocol, and tool handler layers with no behavior change.
2. Make tool mode session-local while keeping existing default mode and backward-compatible descriptors.
3. Add public shared-server notes to server instructions, docs, self-test/open-workspace output, and tool descriptions.
4. Stamp workers/jobs/artifacts with owner metadata and return session-relative ownership flags in public views.
5. Enforce explicit takeover for cross-owner mutating worker/artifact tools.
6. Add repo mutation lock manager and lock base-checkout writes/integration/shared-write entry points.
7. Fix concurrency admission to count pending plus running jobs under a manager lock.
8. Add multi-client tests and the disposable direct-MCP trial scenario.
9. Run the full connector verification suite and update public docs with the final implemented behavior.

## Non-Goals

- No automatic queue for worker messages or repository writes.
- No worker ERP, deterministic role system, workflow graph, or generic message bus.
- No hidden per-chat private universe. This remains a local operator server; read/list visibility is intentional.
- No automatic commit, merge, or cleanup after integration.
- No claim that MCP session ownership is authentication. Authentication remains bearer/query-token and local network policy.

## Open Questions For Implementation

1. Should cross-session takeover require a plain boolean only, or should it also require a short `takeover_reason` for audit readability? Recommendation: support both, require only the boolean.
2. Should client labels be supplied through every tool or only through `codex_self_test` / `codex_open_workspace` session setup? Recommendation: accept `client_label` on setup and store it in the session map.
3. Should process-wide tool mode switching remain available for direct local debugging? Recommendation: keep a private/internal fallback for tests or CLI-only direct clients, but ChatGPT-facing behavior should be session-local.
4. Should the first implementation include `fcntl` locks? Recommendation: implement the interface now; use `asyncio.Lock` for current single-process launch; add `fcntl` when a multi-process launch mode is documented or tested.
