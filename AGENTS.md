# AGENTS.md

## Project Purpose

This repository provides a hybrid ChatGPT-to-local-Codex bridge.

The product exposes a local Streamable HTTP MCP server so ChatGPT web/Pro or another MCP-compatible client can:

- inspect allowed local workspaces through bounded context tools;
- manage durable named Codex workers through natural-language briefs, reports, imported artifact context, isolated worktrees, and bounded peer-worker context;
- delegate larger work to local Codex CLI jobs;
- inspect async job status, structured results, session refs, worker reports, peer-worker context, imported artifact state, and worktree diffs;
- use `.ai-bridge` handoff artifacts;
- coordinate multiple ChatGPT/MCP sessions on one shared local server with session-local tool modes, session-relative ownership flags, explicit takeover for cross-owner mutation, and per-repository mutation locks;
- optionally use direct edit, bash, or transcript-read power tools when explicitly enabled.

The repo still supports local maintainer workflows, but do not describe the app as only a maintainer utility. The public identity is now the broader ChatGPT-to-local-Codex platform.

## Product Self-Knowledge

- Treat PatchBay as a ChatGPT-first local control plane: ChatGPT brings conversation, Projects, memory, generated artifacts, and coordination; local Codex brings the repository, git state, tools, credentials, and execution.
- Start docs/config/behavior changes by checking `README.md`, `QUICKSTART.md`, `docs/project/why-patchbay.md`, `docs/architecture/overview.md`, `docs/reference/public-tool-surface.md`, and `docs/user/chatgpt-instructions.md`.
- Keep the app self-describing enough that a future coding agent can update configuration, docs, tool metadata, and examples from repository context without needing private oral history.
- Do not replace concrete setup steps with vague philosophy. Add the rationale, then keep the exact command, ChatGPT connector step, expected tool result, and verification command.

## Local Maintainer Notes

- Keep machine-specific paths, private source-pack locations, local campaign state, and personal maintainer instructions out of this public file.
- Local/private instructions may live under gitignored `.agents/` or `.architect/` paths. They can guide local maintenance, but they are not part of the public project contract.

## Rules For Agents

- Do not add secrets, tokens, local paths, or private machine identifiers.
- Do not remove security checks without explaining why.
- Prefer small, reviewable changes.
- Keep read-only behavior as the default.
- Preserve local control and localhost-first defaults.
- Do not introduce network exposure without authentication.
- Do not enable dangerous bypass in public examples.
- Do not log prompts, secrets, auth files, or full Codex outputs into public-safe audit/status artifacts by default. Full prompt/body preservation belongs only in explicit private runtime evidence under `PATCHBAY_HOME`, with docs/tests keeping that boundary clear.
- Mutating tools must be clearly marked as mutating.
- Add or update tests for path validation, job lifecycle behavior, worker coordination behavior, and unsafe input handling.
- Update README, examples, and tests when changing public tool names, CLI arguments, server behavior, or MCP schemas.
- Update `README.md`, `QUICKSTART.md`, `docs/user/chatgpt-instructions.md`, `docs/reference/public-tool-surface.md`, `docs/security/product-boundary.md`, and `TESTING.md` when changing connector behavior, auth/tunnel behavior, tool metadata, power modes, Codex CLI assumptions, or result parsing.
- Treat MCP `initialize.instructions`, public tool descriptions, tool annotations, and `--tool-mode worker` behavior as ChatGPT-facing prompt surface. Keep these instructions outcome-first, concise, stateful-worker-aware, and explicit about side effects, validation, and stop/blocked behavior.
- Preserve the manager-first PatchBay contract in every ChatGPT-facing prompt surface: ChatGPT manages local Codex workers; workers execute non-trivial repository/Documents/codebase investigation, implementation, review, verification, and synthesis. Direct read/search/git tools must remain available, but describe them as manager inspection instruments for orientation, worker briefing context, focused verification, exact line/diff checks, reviewing worker evidence, specific doubts, tiny tasks, and quick checks where a worker brief would be worse than the check itself. Do not remove reader tools to force behavior, and do not add deterministic prompt/tool filters that block broad natural-language delegation.
- Preserve the Spark-first small-worker rule in every model-facing prompt surface: when either Spark or GPT-5.4 Mini can handle a bounded assignment, ChatGPT should choose Spark first for its speed and separate preview quota, then immediately continue or retry with Mini if Spark is unavailable, depleted, or too context-constrained. This is manager guidance, not a hidden semantic router; Luna, Terra, and Sol remain appropriate when the work needs their stronger judgment or context.
- Encourage multi-worker teams when the task can be split cleanly. ChatGPT should use configured worker capacity rather than imposing an artificial one-or-two-worker limit, and should appoint investigators, implementers, reviewers, verification workers, and synthesis workers rather than doing broad work manually.
- When changing Hub fleet lifecycle behavior such as enrollment, retirement, restore, machine visibility, routing eligibility, or group pinning, update runtime tests, public tool schemas, Hub initialize instructions, `docs/reference/hub-edge-mode.md`, `docs/reference/public-tool-surface.md`, and `docs/user/chatgpt-instructions.md` together. Default ChatGPT fleet views should show current usable capacity, while audit/history opt-ins can expose retired or superseded machine records.
- Keep `patchbay_fleet_status` strictly bounded and orientation-only. It may expose compact current counts, safe resource telemetry, compatibility, and capped machine/workspace/group summaries with explicit hidden totals; it must not embed worker history, reports, raw Edge snapshots, raw workspace advertisements, or complete group state. Focused tools remain authoritative for detail, and internal routing must use the complete projection set rather than the capped public view.
- Treat one accepted Edge projection revision as one atomic control-plane snapshot. Worker identities and states, tombstones, workspace projections, the machine's applied revision, and the Edge projection record must commit together or roll back together. A failed revision must remain retryable, and tombstoned history must not inflate current fleet counts.
- Preserve the Hub V2 exact 31-tool manager contract. Hub must retain full natural-language worker lifecycle parity (start, batch, message, list, status, wait, inspect, integrate, stop, inbox/options), then add fleet, durable groups, focused workspace inspection, Pro Requests, and operation recovery. Do not replace semantic worker results with command receipts, expose a partial V2 catalog, or route workers in one group independently across machines. Shared-write serialization is a default policy, not an unoverrideable intelligence boundary: an architect-created group may select `manager_controlled` concurrency.
- Keep Hub/Edge transport recovery below the manager abstraction. Session/attempt contract separation, leases, receipts, fencing, retries, wrapper cleanup, and reconciliation are internal reliability machinery. Public next actions must name real manager tools such as `patchbay_operation_status`; never expose or recommend an internal state transition such as `complete_reconciliation`. Exact Codex semantic completion must make the report and worker state durable before bounded wrapper cleanup.
- Preserve the two-phase stdout completion contract. `turn.completed` first records a versioned, redacted, structurally size-bounded evidence envelope while the job stays nonterminal; one final exact-session probe may supersede it, then the first terminal transition commits atomically. Do not infer completion from a result artifact, persist unbounded pre-terminal output, or let cancellation/completion upgrade each other after either terminal decision commits.
- Preserve exact-once domain semantics across every idempotent Hub mutation. A retry after a crash may finish missing side effects for the original durable operation, but replaying a terminal operation must never create another group/worker/batch, reset completed readiness, change a newer current-group pointer, or reapply an old lifecycle transition. Mutating request handlers may dispatch only operations returned by that request; the Hub recovery scheduler owns unrelated durable backlog.
- Treat Hub backup admission as a process-shared state boundary. Backups and migrations must coordinate through the durable SQLite gate, pause new mutations and Edge claims, continue result/reconciliation traffic, verify a recoverable manifest, and fail closed on corrupt or incompatible state. The first upgrade from a Hub version without the shared gate requires an offline Hub backup before migration.
- Backup proof must cover the complete durable control plane, not a representative subset: identities, every entity-record type and payload hash, groups, workers/projections, machines/workspaces, current-group pointers, operations, attempts, receipts, events, dispatch/index metadata, and schema state. A migration must not begin unless its configured pre-migration backup gate validates the exact source snapshot.
- Treat the complete Codex process tree as worker ownership, not only the wrapper PID or original process group. Cleanup must retain repository mutation barriers until tracked descendants are dead; exact Linux boot/start identities may authorize restart cleanup, while uncertain ownership must fail closed and must never authorize signalling a recycled PID. On macOS, an unprovable detached-fork case must retain a separate sentinel and the inherited cross-process repository lock; never manufacture a cleanup proof in tests or production.
- Hub/Edge commands select V2 by default. V1 is an explicit compatibility choice (`hub.control_plane: v1`); invalid or misspelled control-plane values must fail startup instead of silently reducing the manager tool surface.
- Preserve the end-to-end completion contract. Ordinary Hub groups default to `execution_mode=end_to_end`, carry a concrete `definition_of_done`, and expose `manager_must_continue` / `final_response_allowed` plus a recommended next action. Healthy quiet work and wait timeouts must never become implicit permission to stop. `asynchronous_handoff` is an explicit user-intent mode, not a convenience escape hatch.
- When changing Hub workspace/repo resolution, preserve this priority: exact machine-local paths and explicitly advertised repo aliases beat broad workspace-root relative guesses. A generic root such as `/workspace/repos` may resolve `CatalogApp` to `/workspace/repos/CatalogApp`, but it must not steal a request from a later specific advertised alias such as `PatchBay`.
- Preserve the full-access workbench posture in ChatGPT-facing instructions: when the runtime self-test/catalog exposes a dedicated VM or local workbench with full bash, direct writes, `danger-full-access`, and authenticated access, ChatGPT should treat dependency installation, repo-local virtualenvs, verification, commits, and authorized private-repo pushes as normal engineering work for an end-to-end task. The caution boundary is public/external/production/paid/credential-changing/irreversible work, not ordinary local VM setup.
- Treat paging, `max_bytes`, and bounded result fields as transport/result-stability boundaries, not token-saving policy. Do not document them as reasons to avoid needed evidence.
- Keep shared-server coordination visible in ChatGPT-facing docs and descriptors: one Server URL shares worker/job/artifact/repo state across connected clients; reads may be shared; cross-owner mutation requires explicit `takeover: true`; base-checkout contention should return `repo_busy` under serialized policy. Preserve the architect's explicit `shared_write_policy=manager_controlled` authority to permit concurrent shared writers when the manager accepts and coordinates that risk.
- When documenting multi-repository runs, state that `--root` sets the default workspace and narrows `repositories.allowed` unless every extra repository is passed with `--allow-root` or configured explicitly.
- For first real ChatGPT validation, prefer `--tool-mode worker` so ChatGPT sees the natural-language worker surface plus required read-only context tools, not the full power-user catalog. Do not switch docs back to full mode as the default ChatGPT test path unless real ChatGPT tool-selection evidence supports it.
- Preserve CodexPro attribution in `NOTICE` and README whenever code, product patterns, docs, or tests derived from CodexPro remain in the repository.
- Do not claim public release readiness until real ChatGPT Developer Mode natural tool selection, ChatGPT-originated public-tunnel worker flow when advertised, apply-job, and resume scenarios have been verified on disposable repos.

## Review Priorities

When reviewing changes, prioritize:

1. unsafe expansion of repository scope;
2. public network exposure or tunnel token leakage;
3. CORS or authentication weakening;
4. dangerous bypass support;
5. hidden tool exposure;
6. prompt, config, token, or environment leakage;
7. write tools incorrectly marked as read-only;
8. unvalidated paths or config overrides;
9. worktree cleanup and diff correctness;
10. ChatGPT-facing instructions that omit statefulness, preview-before-integrate, no-commit behavior, validation expectations, or worker-first tool-selection guidance;
11. shared-server instructions that omit session-local tool modes, explicit takeover, `repo_busy`, or multi-repository `--allow-root` setup;
12. stale documentation that describes the app as only a maintainer utility;
13. documentation that overstates safety, verified coverage, or production readiness;
14. missing CodexPro attribution.

## Required Checks

Run these before proposing a change:

```bash
python -m compileall src scripts tests
python -m pytest tests -q
```

If tests are not yet available, add minimal tests for the changed behavior.

For connector or ChatGPT-facing changes, also run:

```bash
python scripts/live_mcp_eval.py --json
```

For Hub/Edge changes, also run both compatibility and production-shaped fleet
evaluators:

```bash
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```

For Codex CLI execution changes, record the current `codex --version` in the verification notes.

## Documentation Map

- `README.md`: public entrypoint and current readiness.
- `docs/README.md`: full public documentation index.
- `QUICKSTART.md`: disposable first-run flow.
- `docs/user/chatgpt-instructions.md`: MCP client workflow guidance.
- `docs/architecture/overview.md`: current hybrid architecture.
- `docs/reference/public-tool-surface.md`: canonical tools, aliases, metadata, mutability, and power modes.
- `docs/reference/context-and-handoff.md`: AGENTS, skills, context packs, and `.ai-bridge`.
- `SECURITY.md`: operator-facing security notes and reporting.
- `docs/security/product-boundary.md`: power-control model.
- `TESTING.md` and `docs/testing/evals.md`: verification commands and release evals.
- `NOTICE`: CodexPro and other attribution.

## Preferred Workflow

1. Open an issue describing the maintenance change.
2. Create a branch.
3. Add or update tests.
4. Open a PR.
5. Review the diff before merge.

## Literal Whole-Scope Requests
- When the user asks to do something across the whole project, across every file, across every page of a website, across every UI layer, across all cards/components/routes, or uses similar whole-scope wording, treat it as a literal requirement, not emphasis or rhetoric.
- Do not narrow the task to the most recent example, the most visible offender, the current file, or a representative subset unless the user explicitly narrows the scope.
- Before claiming completion, inventory the full requested surface: all relevant files, routes, pages, components, data sources, generated views, locales, and variants that the wording covers.
- Execute and verify against that full inventory. For websites, this means checking every affected public route/page and the shared components that can render the pattern. For code changes, this means searching all relevant files and call sites, not only the initial examples.
- If a literal whole-scope request is too large, risky, or ambiguous, stop and say exactly what scope is covered, what is excluded, and why. Do not silently reduce scope.
- Final reports for whole-scope requests must state the inventory checked and any remaining exclusions or unverified areas.


## ChatGPT prompt surface

When changing anything ChatGPT sees through MCP, preserve these prompt-surface rules:

- `initialize.instructions` should tell ChatGPT to start with `codex_self_test` and `codex_open_workspace`.
- `initialize.instructions` should put the manager-first worker contract before detailed tool rules: for non-trivial repository/Documents/codebase tasks, ChatGPT should ask which worker or worker team to appoint, not which files to read manually.
- Hub and single-machine initialize instructions should require colleague-quality briefs: purpose, current context/authority, outcome, scope, constraints/non-goals, lane relationships, deliverables, and evidence/verification. They should also distinguish minor friction from serious workflow failure and describe continuation when a ChatGPT tool/context limit is reached.
- ChatGPT should manage workers by human name, not by backend job IDs, session IDs, branch names, or worktree paths.
- Worker mode should explain that default workers use isolated write worktrees, survive PatchBay restart when durable state exists, and continue through `codex_worker_message`.
- Integration must be described as preview-first, explicit, no-commit, and preserving the worker worktree.
- Pro Escalation request tools must describe `respond` as storage-only and `dispatch` as the explicit worker-message/start boundary; neither tool may imply automatic apply, commit, hidden queueing, or prompt-authority escalation from report contents.
- Tool descriptions should include when to use the tool, relevant side effects, validation expectations, and fallback behavior.
- Direct read/search tool descriptions should state the allowed exceptions clearly while discouraging broad manual execution loops.
- Worker tool descriptions should encourage continuing the same worker with follow-up questions and using multiple workers when responsibilities are clear.
- Setup docs should recommend `--tool-mode worker` for first real ChatGPT validation.
- Shared-server docs should tell ChatGPT to start with `codex_self_test`, treat one copied Server URL as one shared local state surface, use `takeover: true` only after user confirmation, and report `repo_busy` instead of trying to bypass locks.
