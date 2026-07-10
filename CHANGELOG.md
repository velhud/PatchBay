# Changelog

## Unreleased

- Made Spark the preferred first choice over GPT-5.4 Mini for bounded small-worker assignments, with an explicit immediate Mini fallback when Spark is unavailable, quota-depleted, or context-constrained.

- Rewrote public positioning docs around PatchBay as a powerful ChatGPT-to-local-Codex control plane that eliminates copy-paste between ChatGPT context and local Codex execution.
- Expanded README and architecture diagrams to cover the current service graph: MCP sessions, tool modes, descriptors, Apps card, workspace context, workers, artifact inbox, job execution, power tools, repo locks, and runtime state.
- Reworked Quick Start around the real ChatGPT connector flow: start PatchBay with a tokenized HTTPS `/mcp` tunnel, create the ChatGPT connector, add it in a new chat, and verify `codex_self_test` plus `codex_open_workspace`.
- Renamed the project, Python package, checked-in examples, launcher metadata, token environment variables, and public docs to PatchBay.
- Reorganized the implementation under `src/patchbay` and the public documentation under topic-based `docs/` sections, and removed the obsolete repository reorganization preplan.
- Repositioned the project as a hybrid ChatGPT-to-local-Codex bridge.
- Added ChatGPT-facing workspace context tools, handoff tools, tool metadata, Apps resource support, launcher/doctor/profile flow, token-gated tunnel controls, and compatibility aliases.
- Added optional direct write, command, and Codex transcript power modes that remain disabled by default.
- Added durable job/session state, cancellation, redacted artifacts, and current Codex CLI JSONL result parsing.
- Added durable natural-language workers with model/reasoning selection, artifact inbox transfer, isolated writing worktrees, multi-worker context relay, integration preview, and explicit accepted-result application.
- Added compact worker state visibility: `codex_worker_status`, per-worker status lines, active/quiet/stale/lost liveness categories, activity deltas since the last check, latest partial notes, read-only report-file explanations, and live event/output counters.
- Added shared-server coordination for multiple ChatGPT/MCP sessions: session-local tool modes, session-relative ownership flags, explicit worker/artifact takeover, per-repository mutation locks, and multi-client trial coverage.
- Added installable onboarding/transport commands: `patchbay`, `patchbay-stdio`, `patchbay setup`, `patchbay settings`, stdio MCP transport, explicit `patchbay install-cloudflared`, ngrok/stable tunnel shortcuts, and URL copy/open controls.
- Clarified multi-repository launcher behavior: `--root` narrows the allowed root set and every additional repository must be passed with `--allow-root` or configured under `repositories.allowed`.
- Fixed worker lifecycle reconciliation so a job is not falsely marked failed while its executor task is still parsing a just-exited Codex process, successful completion clears stale transient error text, and completed durable job records are cleaned on load.
- Added Hub V2's exact 31-tool manager surface, durable work groups and lanes,
  availability-only machine placement with one-machine group pinning, routed
  workspace inspection, full natural-language worker lifecycle parity, signed
  integration, operation recovery, and durable Hub/Edge restart continuity.
- Fixed deterministic Edge refusals such as duplicate worker names so they
  return terminal actionable errors instead of uncertain outcomes; reruns can
  use unique names or `auto_suffix`.
- Made Hub worker waits state-sensitive: ordinary machine heartbeats and
  resource telemetry no longer wake worker waits, and an omitted
  `since_revision` now waits from the worker's current projection rather than
  returning historical state immediately.
- Added Codex CLI `0.144.1` model guidance for GPT-5.6 Luna, Terra, and Sol,
  accepted live-catalog `ultra` reasoning on supported models, and clarified
  when explicit PatchBay lanes remain preferable to automatic delegation inside
  one worker.
- Strengthened Hub and single-machine manager instructions with colleague-quality
  worker briefs, configured-capacity parallelism, minor-versus-serious failure
  handling, durable continuation notes for ChatGPT limits, and terminal
  operation/group verification before completion claims.
- Verified local MCP probing, real worker phase evals, real `codex_plan_job` execution, and direct tokenized public-tunnel MCP artifact worker simulation with Codex CLI `0.144.1`.
- Verified the production public Hub connector outside-in with the exact 31
  exposed tools, a durable group on a real Edge, two parallel Codex workers,
  same-worker continuation, signed isolated-worktree integration without an
  automatic commit, base-checkout verification, cleanup, and group closure;
  a separate single-worker start-to-integration scenario passed as well.
- Added CodexPro attribution in `NOTICE` and README.
- Independent ChatGPT browser-conversation behavior remains deployment-specific
  operational evidence; the generic public connector acceptance contract is
  documented under `docs/testing/public-hub-acceptance.md`.

## v0.1.0

- Initial public release of `patchbay`.
- Added explicit PatchBay tool names.
- Added localhost-first security defaults and documentation.
- Added CI-friendly tests for tool surface, path validation, redaction, and security defaults.
- Added maintainer workflow examples and OSS roadmap.
