# Changelog

## Unreleased

- Repositioned the project as a hybrid ChatGPT-to-local-Codex bridge.
- Added ChatGPT-facing workspace context tools, handoff tools, tool metadata, Apps resource support, launcher/doctor/profile flow, token-gated tunnel controls, and CodexPro-compatible aliases.
- Added optional direct write, command, and Codex transcript power modes that remain disabled by default.
- Added durable job/session state, cancellation, redacted artifacts, and current Codex CLI JSONL result parsing.
- Verified local MCP probing and real `codex_plan_job` execution through the wrapper with Codex CLI `0.141.0`.
- Added CodexPro attribution in `NOTICE` and README.
- Release remains pending real ChatGPT Developer Mode, public tunnel, apply-job, and resume evals on disposable repos.

## v0.1.0

- Initial public release of `codex-mcp-wrapper`.
- Added explicit Codex MCP tool names.
- Added localhost-first security defaults and documentation.
- Added CI-friendly tests for tool surface, path validation, redaction, and security defaults.
- Added maintainer workflow examples and OSS roadmap.
