# OSS Roadmap

This repository is the first public piece of a broader open-source direction around Codex, MCP, repository-context tooling, and safe agentic maintainer workflows.

## Phase 1 - Codex MCP Wrapper

- Add first-class Codex-specific MCP tool names while preserving compatibility aliases.
- Add examples for read-only planning, isolated apply jobs, review flows, and diff inspection.
- Add CI smoke tests and path/sandbox validation tests.
- Document safe local deployment assumptions.
- Publish a v0.1.x release.

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
- privacy-safe examples;
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
- safe resumability;
- release and security review workflows.

## Security Principles

- Owned repositories only.
- Localhost-first defaults.
- Explicit repository allowlists.
- No secret values in prompts or committed configs.
- Read-only planning before write-capable actions.
- Diff review before merge.
- Synthetic fixtures for public examples.
