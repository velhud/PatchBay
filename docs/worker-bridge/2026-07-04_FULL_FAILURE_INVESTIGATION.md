# PatchBay Failure Investigation - 2026-07-04

## Scope

This investigation reviewed the latest SampleRepo/PatchBay VM session, prior reported ChatGPT complaints, and the current PatchBay code paths for worker lifecycle, workspace discovery, tool schemas, ownership, tool-card rendering, and manager-first prompting.

The goal was not to restrict ChatGPT or remove direct tools. The goal was to preserve the PatchBay philosophy: ChatGPT should manage competent Codex workers through natural language, use several workers when useful, and use direct file/search/diff tools only for orientation, tiny work, verification, or concrete escalation.

## VM Run Reality

The latest inspected VM run showed a mixed picture:

- PatchBay processed hundreds of MCP tool calls in one session.
- Worker execution mostly worked: fourteen worker jobs were created, every created job started a Codex process, twelve completed, and two were manually stopped/cancelled.
- The most common warnings were path-discovery failures, not worker execution failures. ChatGPT repeatedly tried guesses such as a bare repository name, a root-level repository path, and other wrong paths before finding the real multi-repo checkout path.
- Broad direct searches timed out because ChatGPT searched large trees through `codex_search_repo` instead of delegating broad search to workers or narrowing the path/glob.
- Some stopped workers had live output and command activity, but no final structured report at the moment ChatGPT stopped them. This can look like a worker failure even when the worker was still working normally.

## Real Problems Found

1. Workspace discovery was too weak.
   `codex_list_workspaces` only listed configured roots and aliases. A repo could be valid and authorized but not discoverable by name, so ChatGPT guessed paths repeatedly.

2. Broad search failures looked like tool failures.
   `codex_search_repo` used a hard `rg` timeout and raised an error. It did not return partial matches, timeout metadata, or a recovery suggestion.

3. Worker-mode lacked the workspace discovery tool.
   `codex_list_workspaces` was useful for the new flow but was not exposed in worker mode.

4. Cancellation evidence could return too early.
   `codex_worker_stop` waited for partial artifacts, but an existing checkpoint could satisfy the wait condition before the actual cancelled partial result was attached.

5. Public worker activity timestamps were too stale.
   `last_activity_at` used completion/start time, not latest heartbeat/stdout/stderr/checkpoint activity. A live worker could look older than it really was.

6. `context_from_workers` was capped below the intended team size.
   The cap was six workers, while PatchBay's ChatGPT-facing philosophy and runtime target now use up to ten concurrent worker lanes.

7. Full-mode compatibility aliases had weaker descriptions.
   Aliases like `read`, `search`, `tree`, and `bash` did not inherit the canonical manager-first warnings. In full mode that made direct-tool drift easier.

8. The setup guide did not strongly remind ChatGPT to be a manager.
   The connector setup steps named self-test and workspace opening but did not include the strongest worker-manager instruction.

## Misunderstandings Or Expected Behavior

1. No final report while a worker is active is not failure.
   A worker can be running, emitting events, executing commands, or still reasoning before producing the final structured result.

2. `item.completed` followed by `working` can be normal.
   One command/item completed; the full Codex turn may still be active.

3. Read-only workers do not create repo report files.
   They still produce PatchBay runtime reports, partial notes, checkpoints, and report artifacts.

4. `active_mcp_sessions` is not ownership.
   ChatGPT Apps can create many short-lived transport sessions. Ownership is token/coordinator-scoped, not the raw MCP session count.

5. Repeated `resources/read` for tool cards is likely ChatGPT host behavior.
   The widget is static and has no internal loop that calls tools.

## Fixes Applied

1. Added configured workspace discovery.
   `repositories.discovery_roots`, `max_discovery_depth`, and `max_discovery_results` let operators expose multi-repo folders for shallow discovery. `codex_list_workspaces` now supports `query`, `discover`, `max_depth`, and `max_results`, and returns discovered roots that ChatGPT can pass as `repo_path`.

2. Added workspace suggestions on bad paths.
   When a requested workspace does not exist or resolves outside allowed roots, PatchBay can suggest matching discovered workspaces instead of leaving ChatGPT to keep guessing.

3. Made search timeout structured.
   `codex_search_repo` now accepts `timeout_ms` and returns `timed_out`, `timeout_ms`, `searched_path`, partial matches when available, and `suggested_next` instead of turning broad search timeout into an opaque tool error.

4. Exposed `codex_list_workspaces` in worker mode.
   Worker mode now includes the discovery tool so manager-mode ChatGPT can resolve repo paths without switching to full mode.

5. Fixed cancellation artifact waiting.
   `codex_worker_stop` no longer treats pre-existing checkpoints as sufficient proof that cancellation artifacts are ready. It waits for the cancelled event or partial result, with a short fallback for untracked test executors.

6. Improved low-level cancel/get-result surfaces.
   Worker-tagged low-level cancellation now reports whether partial artifacts are ready or pending, and `codex_get_result` returns cancelled partial result data when present.

7. Updated public activity timestamps.
   Worker `last_activity_at` now reflects the latest meaningful heartbeat/stdout/stderr/command/checkpoint/completion/start timestamp.

8. Raised peer worker context cap to ten.
   `context_from_workers` now matches the intended ten-worker team scale, with schema text explaining batching/synthesis for larger campaigns.

9. Strengthened prompt and schema surfaces.
   Initialize instructions now explicitly say not to precompute paths/folder maps for workers. Full-mode aliases inherit canonical manager-first descriptions. Connector setup steps now instruct ChatGPT to act as manager of local Codex workers.

10. Updated docs and live evals.
   Documentation now distinguishes real tool failures from expected long-running worker states and describes workspace discovery/search-timeout recovery.

## Verification

Local verification passed:

- `python -m compileall src scripts tests`
- `python -m pytest tests -q`
- `python scripts/live_mcp_eval.py --json`
- `python scripts/live_mcp_eval.py --tool-mode full --json`

The full test suite passed with only existing FastAPI deprecation warnings.

## Remaining Follow-Up

Active-turn steering is still not implemented. PatchBay currently supports follow-up messages after a worker turn completes, not live mid-turn steering. That should be designed separately against the Codex CLI's real conversation-steering capabilities.

The VM should configure local `repositories.discovery_roots` such as the multi-repo folder used on that machine. This is intentionally deployment-local rather than hard-coded into the open-source default config.
