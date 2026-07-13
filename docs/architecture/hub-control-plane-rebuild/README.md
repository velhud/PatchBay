# Hub Manager Control Plane Rebuild

Status: `IMPLEMENTED_AND_LIVE_VERIFIED`

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

This directory is the canonical design and implementation record for the
PatchBay Hub manager-facing control plane. It records the evidence,
invariants, rejected alternatives, selected architecture, exact public tool
surface, state model, implementation sequence, and verification gates.

The rebuild is the default Hub/Edge runtime. Hub V1 remains available through
the explicit `hub.control_plane: v1` compatibility path and as a regression
fixture, but its reduced public control semantics are not the V2 contract.

## Why The Rebuild Is Necessary

The mature single-machine worker facade lets ChatGPT manage named Codex workers
through natural language. The current Hub V1 instead exposes a reduced,
separately handwritten tool catalog whose worker-looking calls normally return
Hub command receipts rather than worker results.

The investigation confirmed several shared root causes:

- transport completion is confused with operation success;
- group and lane state are not derived from authoritative Edge worker state;
- a group can be closed while Codex workers are still active;
- worker schemas lose inbox, list, peer context, artifact context, takeover,
  pagination, cleanup, accepted-dirty-base, and status-scope behavior;
- command leases and idempotency fields do not implement recovery;
- reassignment mutates a group's machine while old workers remain elsewhere;
- cross-conversation ownership depends too heavily on ephemeral session identity;
- routed waits can block the Edge control loop;
- Hub tool annotations, validation, and output schemas are weaker than the
  established single-machine contracts;
- the checked-in Hub live evaluation proves routing mechanics with synthetic
  Edge results, not one consequential real worker lifecycle.

## Product Contract

PatchBay Hub must preserve this simple user model:

```text
ChatGPT is a manager.
A work group is one durable task.
The group is placed on one machine.
Lanes are responsibilities inside that task.
Workers are durable named Codex colleagues inside lanes.
ChatGPT talks to workers naturally and repeatedly.
Hub routing and command transport stay behind those managerial actions.
```

Hub is an additional coordination layer. It must not make ordinary worker
management less capable or more technical than single-machine PatchBay.

## Selected Direction

Rebuild Hub as a transport-transparent adapter around the canonical Edge
`ToolHandler` and worker runtime:

1. Keep machine enrollment, heartbeats, availability-only routing, Edge-local
   Codex execution, worktrees, path guards, repository locks, and explicit
   integration.
2. Replace the handwritten reduced Hub worker schemas with descriptors generated
   from the canonical worker, workspace, and Pro Request contracts.
3. Add only the routing envelope required by Hub: work group, lane, stable
   fleet worker reference, logical workspace reference, and exceptional explicit
   machine selection.
4. Make Hub operations return the real Edge domain result after a bounded wait.
   Use an opaque operation reference only when completion is genuinely delayed
   or uncertain.
5. Give Hub authoritative durable projections of groups, lanes, workers,
   operations, integrations, and ownership, while Edge remains authoritative
   for local Codex process/session/worktree state.
6. Use transactional durable state and explicit retry, lease, idempotency, and
   unknown-outcome semantics.

## Canonical Artifact Order

1. [Solution design intake](solution-design-intake-checklist.md)
2. [Root-cause evidence](root-cause-evidence-brief.md)
3. [Purpose and invariants](app-purpose-and-invariant-map.md)
4. [Affected surfaces and ripple risks](affected-surface-and-ripple-map.md)
5. [Solution options](solution-options-register.md)
6. [Option comparison](option-comparison-matrix.md)
7. [Selected solution](selected-solution-design.md)
8. [Exact public tool contract](hub-manager-tool-contract.md)
9. [Resolved contract addendum](resolved-contract-addendum.md)
10. [Implementation and verification plan](implementation-verification-plan.md)
11. [Design decision record](design-decision-record.md)
12. [Conflict-review handoff](solution-to-conflict-review-handoff.md)

## Authority And Current-State Boundary

For the target Hub V2 architecture, this pack supersedes target-state claims in:

- `docs/architecture/multi-machine-hub-implementation-plan.md`;
- `docs/reference/hub-edge-mode.md`;
- the Optional Hub Tool Surface section of
  `docs/reference/public-tool-surface.md`.

Those files remain useful as current V1 behavior and historical implementation
evidence until the rebuild is complete. Implementation and operator docs must
not be rewritten as if V2 exists before tests and live acceptance pass.

## Implementation Evidence

The implementation completed the following gates:

- this design pack receives a cross-solution conflict review;
- every blocking ambiguity is closed in
  `resolved-contract-addendum.md` and carried into WorkPackets;
- the final 31-tool manifest and omissions are accepted as one coherent surface;
- ownership, payload retention, operation reconciliation, and state migration
  decisions are preserved in WorkPackets;
- the current deployed Hub is treated as a compatibility/runtime concern rather
  than proof of the target behavior.

- exact ordered 31-tool MCP catalog and strict schemas;
- transactional SQLite/WAL state, durable identity, groups, projections,
  operations, attempts, leases, receipts, and migration utilities;
- production pull transport and independently scheduled Edge control loops;
- worker start/batch/message/list/status/wait/inspect/integrate/stop parity;
- two real local Edge runners with availability placement and one-machine group
  pinning;
- same-thread follow-up, isolated worktree inspection, signed integration,
  explicit no-commit behavior, group close, and restart history;
- lost result-response recovery without duplicate execution;
- V1 Hub/Edge compatibility and mature single-machine MCP regression passes.

Verification commands:

```bash
python -m compileall src scripts tests
python -m pytest tests -q
python scripts/live_mcp_eval.py --json
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```
