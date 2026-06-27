# Decisions, Risks, And Deferred Work

Status: Phase 4 worker integration and artifact inbox transfer implemented; app-server backend decisions remain pending.

## Confirmed Decisions

### Add A Facade, Do Not Replace The Runtime

The existing PatchBay remains the mechanical substrate. The worker layer composes it into a better product abstraction.

Rejected initially:

- rewrite from scratch;
- generic agent framework;
- immediate Codex fork.

### ChatGPT Is The Manager

No additional internal orchestrator model is introduced. ChatGPT owns delegation and synthesis.

Rejected initially:

- fixed manager/worker graph inside PatchBay;
- deterministic role pipeline;
- second LLM supervisor between ChatGPT and Codex.

### Durable Job Metadata First, Not A Worker Platform

Persist private worker identity, routing, and workspace metadata on durable job records. Add a separate worker record only when later retention policy, multi-worker UX, or integration behavior proves it is needed.

Rejected initially:

- full conversation database;
- employee profiles;
- generic mailbox service;
- event sourcing;
- separate artifact registry.

### Codex Session Is Conversation Truth

Do not duplicate the transcript. Reuse the session ID already persisted by the existing job runtime.

### JSON Files Later If Needed

The durable job store is enough through Phase 2. If later worker records are added, use private atomic JSON under runtime state first. Revisit SQLite only if measured concurrency or corruption problems appear.

### Stable Worker Worktree

A writing worker owns one external worktree across turns. Job cleanup does not own it; explicit worker cleanup does.

### Existing Exec Backend First

Use current `codex exec` and resume machinery for the first complete worker system. Add app-server only behind the same contract after a real spike.

### Six Public Worker Tools

Use six semantically distinct tools for start, message, list, inspect, integrate, and stop. Integration is separate because accepting a worker result is a distinct mutating act.

### Low-Level Tools Remain

The worker facade is the preferred future path, not the exclusive path. Existing tools remain for compatibility, debugging, and power use.

### No Automatic Commit

Worker integration applies accepted changes but does not create a git commit in V1.

### Evidence On Demand

Default response is a natural-language engineering report. Diffs, logs, and raw events are drill-down views.

### No Full A2A/ACP Adoption

Use simple local worker resolution and message delivery first. Keep future protocol compatibility possible without making it a dependency.

## Risks And Mitigations

### Codex Resume Workspace Semantics

Risk: current or future `codex exec resume` versions may not preserve the desired worker-owned worktree semantics.

Mitigation: Phase 2 command builder reasserts `--sandbox` and `--cd` before `resume`, and `scripts/worker_phase2_eval.py` tests real behavior. Add an app-server adapter if exec behavior becomes insufficient.

### Session ID Availability

Risk: session ID may not be available until the first turn completes.

Mitigation: return `accepted: false` for follow-ups until a session is available and avoid exposing session-ID mechanics to ChatGPT.

### Worker And Job Cleanup Conflict

Mitigation: ordinary cleanup skips worker-tagged durable jobs. Phase 2 adds explicit worktree ownership and cleanup.

### ChatGPT May Prefer Old Low-Level Tools

Mitigation: worker-first tool mode, precise worker descriptors, worker-first instructions, and real ChatGPT selection eval.

### Tool Confirmation Friction

Mitigation: keep descriptors truthful, reduce mutating call count through long-running workers, and test actual Developer Mode behavior.

### Patch Construction Risk

Mitigation: Phase 4 builds a bounded patch, rejects blocked paths, checks the base checkout before applying, uses `git apply --check`, and avoids target mutation on conflict.

### Multi-Worker Resource Pressure

Mitigation: reuse existing global concurrency limits and reject busy-worker follow-ups instead of adding a second scheduler.

### Private Path Or Prompt Leakage

Mitigation: separate public worker views from private records, keep private files under runtime state, reuse redaction, and keep transcript reads behind explicit power mode.

## Deferred Capabilities

Do not implement before core worker behavior proves need:

- rich standalone worker dashboard;
- remote multi-user worker hosting;
- distributed workers across machines;
- progressive tool-surface compression so ChatGPT can discover and use more controls while spending fewer tokens on `tools/list`;
- full A2A or ACP server compatibility;
- long-term semantic worker memory beyond Codex sessions;
- fixed role preset library;
- automatic worker hiring/routing;
- semantic automatic quality scoring;
- mandatory reviewer chains;
- automatic commits or pull requests;
- background autonomous task orchestration;
- worker scheduling by cost/model tier;
- voice or real-time UI;
- Codex fork.

## Open Questions For Spikes

1. Does app-server materially improve worker steering/forking enough to justify a second backend?
2. What exact ChatGPT confirmation behavior appears for start/message/stop/integrate tools?
3. What default concurrency is practical on typical local machines?
4. Can integration use one patch format reliably across tracked, untracked, and binary changes?
5. What is the best progressive-disclosure pattern for large MCP tool catalogs: mode switching, menu tools, tool bundles, dynamic discovery, or descriptor compression?

These are implementation/runtime questions. Phase tests should answer them.
