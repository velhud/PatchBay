# OSS Roadmap

This repository is the first public piece of a broader open-source direction around ChatGPT-to-local-Codex bridging, MCP, repository-context tooling, and controlled agentic maintainer workflows.

## Phase 1 - PatchBay

- Add first-class Codex-specific MCP tool names while preserving ChatGPT-friendly compatibility aliases.
- Add ChatGPT-facing workspace context, handoff, and Codex job control flows.
- Add examples for read-only planning, isolated apply jobs, review flows, and diff inspection.
- Add CI smoke tests and path/sandbox validation tests.
- Document local-control deployment assumptions, token-gated tunnel behavior, and pre-release eval status.
- Finish CodexPro-derived onboarding and transport polish without replacing PatchBay's worker-first architecture:
  - public Python CLI entry points for `patchbay setup/start/doctor/settings/ngrok/stable/stdio`;
  - interactive first-run setup and saved profile management;
  - stdio MCP transport for local MCP hosts that do not use Streamable HTTP;
  - stronger Cloudflare/ngrok binary checks, stable-domain guidance, copy/open ChatGPT controls, and tunnel-specific failure hints;
  - optional localhost setup/status helper after the terminal flow is solid.
- Publish a v0.1.x release.

Parked after v0.1.x unless explicitly pulled forward:

- CodexPro-style `loop-handoff`; if implemented, make it PatchBay-native around workers, review, tests, and integration preview instead of copying the CLI loop as the main product path.
- A pure direct-edit simplicity profile; direct write/edit tools already exist, but a later `patchbay start --mode direct` or setup preset can expose the smallest direct coding surface for users who want that workflow.

## Phase 2 - Repository Context Tooling

Release a cleaned OSS subset of local repository-context tooling:

- goal-directed context pack generation;
- token-aware repository exports;
- source filtering and diagnostics;
- audit-friendly file boundary rendering;
- examples for feeding reliable context to Codex and other code agents.

## Phase 3 - Agent Memory Infrastructure

Release cleaned components for provenance-aware agent memory:

- document ingestion;
- structured memory records;
- hybrid retrieval;
- evidence bundles;
- synthetic fixtures;
- sanitized examples;
- memory update and verification patterns.

## Phase 4 - OSS Search And Evaluation

Release a tool for discovering and comparing public GitHub repositories from a natural-language brief:

- README and source inspection;
- evidence-backed ranking;
- persisted run artifacts;
- reproducible finalist comparisons.

## Phase 5 - Agent Control And Verification

Release documentation and reusable patterns for controlled multi-agent development:

- task state;
- verification evidence;
- handoff packets;
- durable traces;
- reliable resumability;
- release and security review workflows.

## Security Principles

- Owned repositories only.
- Localhost-first defaults.
- Explicit repository allowlists.
- No secret values in prompts or committed configs.
- Read-only planning before write-capable actions.
- Diff review before merge.
- Synthetic fixtures for public examples.
