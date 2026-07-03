# Worker State Visibility Implementation

Status: implemented locally, verified through unit tests, live local MCP eval,
and a temporary token-gated ngrok MCP call. Not deployed to the VM yet.

Source input: user-provided state-visibility investigation and proposed design.

## Problem

ChatGPT can misread long-running workers as stuck when PatchBay only exposes
`working`, sparse event labels, and no final report yet. The VM evidence showed
that some workers had live processes, Codex sessions, JSON events, and partial
assistant messages, but the manager-visible surface did not make that obvious.
That caused premature stopping and fallback to manual file reading.

## Implemented Solution

PatchBay now has a compact worker liveness layer.

- `codex_worker_status` returns the pull-based team status bar:
  counts for `active`, `quiet`, `stale`, `lost`, `completed`, `failed`, and
  `cancelled`; deltas since the last status/list check for the same coordination
  owner/client; suggested action; and one short status line per worker.
- `codex_worker_list` includes the same `team_status` plus the existing
  `team_report`.
- `codex_worker_inspect(view="compact")` returns a bounded one-worker status
  snapshot for repeated polling.
- Running worker views expose `status_line`, `activity_since_last_check`,
  normalized `liveness.status`, `liveness.phase`, `latest_partial_note`,
  `latest_checkpoints`, `report_artifacts`, and read-only report-file notes.
- The executor records event count, stdout/stderr bytes seen, last output times,
  current phase, command preview, and command start/completion timestamps while
  Codex JSONL is streaming.
- Read-only workers explicitly say repo report files are unavailable because the
  source checkout is read-only; their reports/checkpoints remain PatchBay runtime
  artifacts.
- Cancellation still preserves captured partial results/checkpoints instead of
  reducing stopped workers to an empty state.

## Design Boundaries

The layer is pull-based. PatchBay does not push chat messages every few seconds
and does not expose raw JSONL/stdout logs by default.

The status categories are runtime telemetry categories, not prompt/content
classification:

- `starting`
- `active`
- `quiet`
- `stale`
- `lost`
- `completed`
- `failed`
- `cancelled`

No whole-turn timeout was added. Fresh/quiet windows remain display guidance
only.

Queued worker-message delivery and active-turn steering are not implemented in
this pass. The official Codex App Server supports active-turn `turn/steer`, but
the current PatchBay worker backend still uses Codex CLI `exec` / `exec resume`,
where follow-up is a later turn. A separate app-server/steering design should
integrate that without weakening the passive status layer.

## Expected Manager Behavior

When workers are running, ChatGPT should first call `codex_worker_status`.

- If events/output/partial notes changed: wait.
- If a worker is active: wait.
- If a worker is quiet: recheck later before stopping.
- If a worker is stale: inspect deliberately.
- If a worker is lost: treat it as a PatchBay runtime recovery issue.
- If no repo report files exist for a read-only worker: use runtime report,
  latest partial note, checkpoints, and report artifacts instead of assuming
  failure.

This keeps ChatGPT in manager mode without flooding its context window.

## Verification

- `python -m pytest tests -q`: `312 passed, 4 warnings`.
- `python -m compileall src scripts tests`: passed.
- `git diff --check`: passed.
- `python scripts/live_mcp_eval.py --json`: passed, `tool_count: 29`.
- Temporary local server on port `8765`: `codex_worker_status` appeared in
  `tools/list`, showed baseline/no-change/changed deltas while a disposable
  worker ran, and showed `cancelled` after stop.
- Temporary ngrok tunnel to that local server: MCP `initialize`, `tools/list`,
  and `codex_worker_status` succeeded through the public URL with Bearer token
  auth. The tunnel and local server were stopped after verification.
