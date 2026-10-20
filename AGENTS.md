# AGENTS.md

## Repository purpose and scope

PatchBay supports two complementary workflows:

1. **Manager/worker mode** — ChatGPT Pro is the manager; local Codex workers investigate, implement, execute, review, and verify.
2. **Author Mode** — ChatGPT Pro is the source author; PatchBay deterministically transports packages, validates paths and manifests, materializes isolated candidates, runs authorized checks, and returns evidence. Author Mode must not initialize or delegate to Codex workers.

This root file is manager/worker-first because most of the repository implements the MCP, worker, Hub, and Edge control planes. For all files under `src/patchbay/authoring/` and `tests/authoring/`, the nested `src/patchbay/authoring/AGENTS.md` contract is authoritative where it is more specific.

Do not collapse the two workflows into one hidden routing layer. Shared Git/process/storage primitives may be reused, but implementation authority, startup, state, tools, and recovery boundaries must remain explicit.

## Current model policy

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

## Core engineering rules

- Do not add secrets, tokens, private paths, or machine identifiers.
- Preserve localhost-first and authenticated-public-access defaults.
- Keep read-only orientation available. Mark every mutating/destructive tool accurately.
- Validate repository paths, package paths, export destinations, object identities, and manifest inputs before side effects.
- Do not weaken package-path validation, maintenance locking, lease checks, unknown-execution fencing, supervisor-launch handling, JSON-RPC validation, safe exports, or honest CLI exit codes.
- Public evidence must stay bounded and sanitized. Private instance identity must not leak through qualification responses.
- Update code, schemas, tests, prompt surfaces, README/quickstart, and testing instructions together when public behavior changes.

## Manager/worker invariants

- `runtime.WorkerRuntime` remains the effective runtime design. Do not activate an older `ReliableWorkerRuntime` monkey-patch or mixins that override the critical integration/source-reading implementation.
- Preserve complete Git candidate construction, delivered-snapshot baselines, stale-acceptance invalidation, cleanup preservation, read-only enforcement, lossless pagination, signal delivery, complete Pro response dispatch, cross-process request serialization, and projection corrections.
- Existing `*_legacy.py` modules are compatibility implementation layers used by public wrappers. Do not delete or bypass them merely because of their names.
- Default writing workers use isolated worktrees. Integration is preview-first, explicit, no-commit, and must preserve the worker worktree.
- Cleanup requires complete delivery/discard proof. An incomplete patch hash is not proof that all candidate content was delivered.
- Treat paging and `max_bytes` as result-stability boundaries, not reasons to omit required evidence.
- Continue workers by human name. Use worker reports and follow-up first; use direct read/search/diff tools for orientation, focused verification, concrete contradictions, risk, failure, or explicit user requests.
- Quiet work and wait timeouts are not implicit failure or permission to stop.
- Preserve per-repository mutation locks, explicit takeover, session-local tool modes, and visible `repo_busy` outcomes.
- Pro Request `respond` stores a response; `dispatch` is the explicit worker-message/start boundary. Neither may imply hidden apply, commit, or model escalation.

## Hub/Edge invariants

- Hub/Edge commands use V2 by default. V1 is an explicit compatibility selection; invalid control-plane values fail startup.
- Preserve the complete Hub manager catalog and natural-language worker lifecycle. Do not replace semantic worker results with command receipts or expose a partial catalog as a valid reduced mode.
- One ordinary user task is one durable group. Workers in a group remain pinned to one machine unless explicitly reassigned.
- Edge projection revisions are atomic across worker/workspace projections, tombstones, machine revision, and projection record.
- Read-only Edge action errors must become terminal sanitized results, never uncertain side-effect outcomes.
- Keep transport recovery below the manager abstraction. Public recovery names real tools such as `patchbay_operation_status`, never internal transitions.
- Preserve exact-once idempotent semantics, attempt fencing, receipt replay, terminal-state monotonicity, and backup/migration fail-closed behavior.
- Process ownership covers the complete Codex process tree. Unknown or recycled identities must never authorize signalling.

## Author Mode invariants

- No imports from manager/worker runtime modules in Author entrypoints or services.
- No hidden Codex calls, worker initialization, model-based implementation, or model-based recovery.
- Packages are deterministic content inputs. Validate archive paths, manifest paths, object formats, hashes, base revisions, and policy before materialization.
- Keep Author state under its independent home: repositories, tasks, leases, revisions, operations, approvals, publications, blobs, and maintenance state.
- Reject symlinked lease caches and unsafe export destinations.
- Checks are policy-authorized subprocess executions with explicit Linux/WSL2 qualification. Do not remove the platform guard to make macOS tests pass.
- Unknown execution remains fenced until recovered or explicitly resolved. Do not infer success from missing process state.
- Approval requires an explicit actor; token creation requires an explicit role.
- HTTP serving disables access logs by default to reduce accidental token/path exposure.
- Qualification output may expose a qualification ID but not the private Author instance ID.

## Required verification

At minimum:

```bash
python -m compileall -q src scripts tests
python -m pytest tests -q
python -m pytest tests/test_audit_regressions_20261019.py -q
python -m pytest tests/authoring -q
python scripts/live_mcp_eval.py --json
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```

Run `python scripts/production_entrypoint_restart_eval.py --json --rehearse-old-schema` for Hub/Edge persistence, migration, backup, or restart changes. Run the optional real-Codex worker trial only when local credentials and explicit worker execution are appropriate.

Use [TESTING.md](TESTING.md) as the complete candidate qualification matrix. Report exactly what ran; never substitute historical branch counts.

## Documentation map

- `README.md`: public product and two-workflow overview.
- `QUICKSTART.md`: separate startup paths.
- `docs/reference/workflow-modes.md`: authority and state boundaries.
- `docs/user/chatgpt-instructions.md`: current manager instructions and Author distinction.
- `docs/reference/public-tool-surface.md`: current public tool families and model policy.
- `docs/reference/tool-surface-and-worker-details.md`: operational details.
- `src/patchbay/authoring/AGENTS.md`: Author-specific implementation contract.
- `TESTING.md`: exact qualification commands.
- `docs/repairs/2026-10-20-unified-reconciliation.md`: source pins and reconciliation disposition.

## Whole-scope requests

Treat “whole repository”, “every file”, “across the project”, and equivalent wording literally. Inventory the complete relevant surface, state exclusions explicitly, and do not claim completion from a representative subset.
