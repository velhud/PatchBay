# Architectural Overview

Status: Phase 2 isolated writing worker facade implemented; integration phases pending.

## Executive Decision

`patchbay` remains the release repository and the local control point between ChatGPT and Codex. The worker bridge is a semantic layer above the existing job, session, workspace, and worktree primitives.

The transition is:

```text
Current primary abstraction:
ChatGPT operates Codex jobs.

Phase 1 primary abstraction:
ChatGPT manages named read-only Codex workers through natural language.

Phase 2 primary abstraction:
ChatGPT manages named isolated writing workers through natural language.
```

The existing PatchBay continues to own exact mechanics. Codex continues to own local engineering judgment. ChatGPT continues to own user-facing management, context selection, delegation, and final synthesis.

## Problem Being Solved

The current PatchBay can launch and inspect Codex work, but ChatGPT must currently reason in low-level concepts:

- job IDs;
- plan/apply job mode;
- polling;
- session references;
- resume jobs;
- worktree paths;
- branch names;
- per-file diff retrieval;
- process cancellation.

Those are valid implementation primitives. They are not the ideal default interface between ChatGPT and local Codex.

The worker interface is managerial:

- start a worker;
- give that worker a brief;
- continue or redirect the conversation;
- see what workers are doing;
- inspect evidence when useful;
- stop work;
- integrate an accepted result in a later phase.

## Change Tracks

### Natural-Language Worker Facade

Add a small worker-oriented MCP surface optimized for ChatGPT tool selection. The task and feedback remain English. Tool arguments carry only the metadata needed to identify the worker, workspace, requested report, or exact side effect.

### Durable Worker Continuity

Workers are linked through private durable job metadata:

```text
private worker id/name on job options
-> authorized repository
-> Codex session/thread
-> current or latest job
-> latest natural-language report
```

The Codex session remains conversation truth. Job records remain execution truth. Git remains code truth. Phase 2 adds private worker worktree metadata to the same durable records instead of adding a separate worker database.

### Parallel Work And Integration

Allow independent writing workers to use separate worktrees. Let ChatGPT inspect and compare reports, route natural-language feedback, and integrate accepted changes through exact git mechanics.

This is not a workflow graph. ChatGPT decides dynamically how many workers to use and what each should do.

## Responsibilities

ChatGPT owns:

- understanding the user's objective;
- writing natural-language worker briefs;
- deciding when one or several workers are useful;
- interpreting reports;
- deciding when evidence is needed;
- comparing alternatives;
- accepting or rejecting results.

Codex workers own:

- repository investigation;
- local implementation planning;
- file and command selection inside the assignment;
- coding, testing, debugging, and revision;
- concise natural-language reporting.

PatchBay owns:

- MCP transport and descriptors;
- authorization and allowed roots;
- worker identity and persistence;
- Codex session/job invocation;
- process state and cancellation;
- worker worktree creation and reuse;
- bounded artifacts and diffs;
- exact integration and conflict detection in later phases.

## Non-Goals

The worker bridge does not create:

- a replacement Codex runtime;
- a Codex fork;
- a generic agent framework;
- a fixed role hierarchy;
- a deterministic task planner;
- a workflow graph;
- a distributed message broker;
- a full agent-to-agent protocol implementation;
- a permanent employee-management system;
- a browser dashboard;
- mandatory reviewer or verifier chains for every task;
- automatic commits or pull requests.
