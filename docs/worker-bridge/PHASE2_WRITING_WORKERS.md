# Phase 2 — Isolated Writing Codex Workers

## Outcome

Phase 2 turns a named Codex colleague into a durable local writing worker:

```text
start a named worker
→ create one external git worktree
→ run Codex with workspace-write authority in that worktree
→ continue the same Codex session by worker name
→ reuse the same worktree across turns and wrapper restarts
→ inspect changed files or one-file diffs on demand
```

The base checkout remains unchanged until a later explicit integration phase.

## Workspace Modes

- `isolated_write`: default. The wrapper creates one external git worktree and private branch for the worker, then reuses it for every continuation.
- `read_only`: advisory mode for investigation, review, architecture, and planning. The wrapper forces a read-only Codex sandbox.
- `shared_write`: explicit direct-workspace mode. It can modify the base checkout and its change view may include pre-existing local edits.

Worker worktrees are created outside the source checkout under `workers.worktree_root`, or under `$CODEX_MCP_HOME/worktrees` / `~/.codex-mcp-wrapper/worktrees` when no root is configured.

## Public Tool Changes

At the Phase 2 snapshot, the public worker surface remained five tools. Later phases add integration and model/reasoning option discovery; see `README.md` and `03_PUBLIC_MCP_CONTRACT.md` for the current surface.

- `codex_worker_start`: gains `workspace_mode`; defaults to `isolated_write`.
- `codex_worker_message`: resumes the same Codex session in the same workspace. Worker display names are scoped by base repository.
- `codex_worker_list`: shows worker state without backend ids or paths.
- `codex_worker_inspect`: supports `view=report`, `view=status`, `view=changes`, `view=file` with `file_path` for worker-side file content, and `view=diff` with `file_path`.
- `codex_worker_stop`: can discard an isolated worktree with `cleanup_workspace=true`.

No worker database, queue, event bus, role engine, automatic reviewer chain, merge command, or integration tool is added in Phase 2.

## Resume Invariant

Every continuation reasserts the same worker workspace and sandbox before the `resume` subcommand:

```text
codex exec --sandbox <mode> --cd <worker-workspace> ... resume <session> -
```

A missing or discarded isolated worktree causes a clear refusal. The wrapper does not silently fall back to the base checkout.

## Privacy Boundary

Normal worker output still omits:

- absolute repository and worktree paths;
- low-level job ids;
- Codex session ids;
- branch names;
- raw transcripts;
- raw process logs.

Change evidence is returned only when requested through `codex_worker_inspect(view="changes")`, `codex_worker_inspect(view="file", file_path="...")`, or `codex_worker_inspect(view="diff", file_path="...")`. `codex_read_file` reads the base checkout and will not see an isolated worker's new files until after explicit integration.

## Verification

Phase 2 adds:

- deterministic unit tests for isolated worktree creation, same-worktree resume, change/diff views, cleanup, and command ordering;
- `scripts/worker_phase2_eval.py`, a real-Codex disposable-repo eval that proves external worktree writing, restart continuity, same-session continuation, on-demand diffs, and base checkout cleanliness.

Real ChatGPT Developer Mode and public tunnel worker flows remain release gates, not claims made by this phase alone.
