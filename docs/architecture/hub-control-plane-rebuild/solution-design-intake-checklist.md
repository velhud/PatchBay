# Solution Design Intake Checklist

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Prepared: 2026-07-09

## Input Artifacts

- [x] PatchBay `AGENTS.md` and product purpose
- [x] Current Hub protocol, runtime, store, Edge runner, and server
- [x] Canonical worker tool descriptors and runtime
- [x] Canonical Pro Request descriptors
- [x] Public tool-surface and Hub/Edge documentation
- [x] Hub protocol/runtime tests and live-eval scripts
- [x] Prior live-verification campaign evidence
- [x] PatchBay manager operating instructions
- [x] The operator's agent, natural-language management, context, and evidence doctrine
- [x] Six independent read-only architecture review lanes

## Evidence Readiness

Status: `READY_FOR_DESIGN`

Root-cause confidence: high for the public-surface, operation-state,
group-lifecycle, ownership, reassignment, preflight, and verification gaps.

Reproduction status:

- source inspection confirmed all core paths;
- independent reviewers reproduced accepted-false start results becoming active
  Hub lanes;
- independent reviewers reproduced expired leases remaining unrecoverable;
- independent reviewers reproduced closed-group commands remaining claimable;
- existing focused tests pass but encode incomplete V1 semantics;
- existing live Hub evaluation is synthetic below the MCP routing boundary;
- prior remote evidence proves real read-only worker starts on two Edges, but not
  full message, report, write, integration, interruption, or recovery behavior.

## Scope

Covered:

- Hub MCP public surface;
- Hub-to-Edge operation transport;
- durable operation state;
- work groups and lanes;
- fleet worker identity and projection;
- cross-session ownership and visibility;
- logical workspace identity and preflight;
- availability-only routing and reassignment;
- worker, artifact inbox, workspace inspection, and Pro Request parity;
- tool schemas, annotations, validation, and output contracts;
- unit, integration, failure-injection, local live, two-Edge, and real ChatGPT
  acceptance requirements.

Not covered:

- semantic task-complexity routing;
- automatic distribution of one normal group across machines;
- moving live Codex sessions between machines;
- cross-machine write merging without explicit branches/integration ownership;
- campaign/channel collaboration beyond work groups;
- redesigning the underlying Codex worker runtime when its existing behavior can
  be reused;
- deployment of the rebuild.

## Authority And Side Effects

Task class: architecture-significant distributed runtime redesign.

Side-effect level for this pack: documentation only.

High-risk boundaries for later implementation:

- persistent Hub state and migration;
- concurrency, leases, idempotency, and retries;
- public MCP schemas and connector manifests;
- cross-session ownership;
- worker integration and worktree cleanup;
- compatibility with existing Edge installations and deployed Hub state.

## Design Decision Readiness

All critical behavior choices needed for WorkPacket planning are selected in
this pack. Cross-solution conflict review remains mandatory before code changes.
