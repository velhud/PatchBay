# Changelog

## Unreleased

- Renamed the project, Python package, checked-in examples, launcher metadata, token environment variables, and public docs to PatchBay.
- Reorganized the implementation under `src/patchbay` and the public documentation under topic-based `docs/` sections, and removed the obsolete repository reorganization preplan.
- Repositioned the project as a hybrid ChatGPT-to-local-Codex bridge.
- Added ChatGPT-facing workspace context tools, handoff tools, tool metadata, Apps resource support, launcher/doctor/profile flow, token-gated tunnel controls, and compatibility aliases.
- Added optional direct write, command, and Codex transcript power modes that remain disabled by default.
- Added durable job/session state, cancellation, redacted artifacts, and current Codex CLI JSONL result parsing.
- Added durable natural-language workers with model/reasoning selection, artifact inbox transfer, isolated writing worktrees, multi-worker context relay, integration preview, and explicit accepted-result application.
- Added shared-server coordination for multiple ChatGPT/MCP sessions: session-local tool modes, safe ownership flags, explicit worker/artifact takeover, per-repository mutation locks, and multi-client trial coverage.
- Clarified multi-repository launcher behavior: `--root` narrows the allowed root set and every additional repository must be passed with `--allow-root` or configured under `repositories.allowed`.
- Verified local MCP probing, real worker phase evals, real `codex_plan_job` execution, and direct tokenized public-tunnel MCP artifact worker simulation with Codex CLI `0.142.2`.
- Added CodexPro attribution in `NOTICE` and README.
- Public release remains pending real ChatGPT Developer Mode natural tool selection, ChatGPT-originated worker flow through a token-gated tunnel when advertised, apply-job, and resume evals on disposable repos.

## v0.1.0

- Initial public release of `patchbay`.
- Added explicit PatchBay tool names.
- Added localhost-first security defaults and documentation.
- Added CI-friendly tests for tool surface, path validation, redaction, and security defaults.
- Added maintainer workflow examples and OSS roadmap.
