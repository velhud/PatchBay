# Root-Cause Evidence Brief

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

## Root-Cause Summary

The current Hub was designed as a lightweight durable command router and later
presented as a manager-facing replacement for the mature single-machine worker
surface. The abstraction boundary is wrong: transport commands, worker
operations, and long-lived worker state are collapsed into one incomplete Hub
record, while the public tool schemas are a reduced handwritten copy of the
real Edge capabilities.

This produces a system that can route a start request but cannot authoritatively
represent or naturally control the full worker lifecycle.

## Root-Cause Clusters

### RC-1: Handwritten descriptor fork

`src/patchbay/hub/protocol.py` defines a separate `HUB_TOOLS` list instead of
deriving routed tools from canonical descriptors. Missing fields and tools are
therefore inevitable and already present.

Lost capabilities include:

- worker inbox and worker list;
- `context_from_workers`, `context_from_artifacts`, and `context_detail`;
- `include_untracked_from_base` and `auto_suffix`;
- message model/reasoning overrides;
- takeover fields;
- worker scopes and filters;
- inspect waiting and file pagination;
- `accepted_dirty_base`;
- `cleanup_workspace`;
- Pro Requests;
- direct manager orientation and exact inspection.

### RC-2: Transport state is treated as operation state

Hub accepts and queues a command, then returns `accepted: true` to ChatGPT.
Later, any Edge response without a transport exception marks the command
completed. Domain refusals such as `accepted: false`, `applied: false`,
`repo_busy`, or `stop_confirmation_required` are not represented as distinct
operation outcomes.

Consequences:

- tool names promise reports, options, waits, stops, or integration but initially
  return queue receipts;
- ChatGPT must manage command IDs;
- a refused worker start can create an active lane with an empty worker ID;
- transport success can be reported as domain success.

### RC-3: Group state is not derived from worker truth

The Hub marks a worker-start command complete when Edge returns the newly
scheduled worker. The Codex turn may still be starting or working. Later Edge
heartbeats update machine summaries but do not reconcile group lanes and worker
records authoritatively.

Consequences:

- group status counts Hub commands, not actual workers;
- lane state can remain stale;
- group close checks active commands, not active workers;
- group close can relabel an active lane idle while Codex still runs;
- pending integration and worktree disposition are not authoritative.

### RC-4: Incomplete delivery and recovery protocol

Commands receive leases and idempotency hashes, but the implementation does not
renew, expire, reclaim, reconcile, or safely deduplicate operations. A lost Edge
or result response can leave a command running forever or make a future retry
dangerously duplicate start, message, stop, or integration.

### RC-5: Ownership conflates durable owner and conversation session

The Hub manager reference prioritizes `chatgpt_session_ref`. A new ChatGPT
conversation can therefore lose discoverability of the same owner's private
groups. When session metadata is absent, conversations can instead collapse onto
one token-level current-group pointer.

The durable owner, current conversation, work run, and group participation are
different concepts and require separate fields.

### RC-6: Machine reassignment mutates the wrong object

Current reassignment changes the machine pinned on the existing group while old
workers and commands remain on the prior machine. Subsequent grouped operations
route only to the new pin, which can strand old worker inspection, messaging,
integration, and cleanup. It also reuses a machine-local resolved path on a
different machine.

### RC-7: Workspace preflight is weaker than its contract

Current preflight can consider an existing path ready without proving:

- the path is inside allowed roots;
- it is the intended logical repository;
- Git state is acceptable;
- branch/HEAD expectations hold;
- worktree storage has capacity;
- active or unintegrated local workers create a conflict.

### RC-8: Verification tested the router, not the product workflow

`scripts/live_hub_edge_eval.py` manually claims Hub commands and posts synthetic
successful results. It does not execute the consequential chain:

```text
HubProtocol -> Hub operation broker -> real EdgeRunner -> ToolHandler
-> WorkerRuntime -> Codex -> report/message/integration -> Hub projection
```

Earlier real remote trials proved that read-only workers could start and finish
through two Edges. They did not prove schema parity, repeated messages, worker
list/inbox, write worktrees, integration, recovery, or natural ChatGPT tool
selection.

## Evidence Table

| Claim | Primary evidence | Confidence |
| --- | --- | --- |
| Hub tools are reduced copies | `src/patchbay/hub/protocol.py`, `src/patchbay/workers/tool_surface.py` | High |
| Worker calls return queue receipts | `HubProtocol._dispatch`, `HubRuntime.queue_worker_command` | High |
| Domain refusal becomes completed command | `HubRuntime.finish_command` | High |
| Group close ignores active workers | `HubRuntime.close_work_group` and its regression test | High |
| Leases are not recovered | `HubRuntime.claim_next_command` and absence of reconciliation paths | High |
| Reassignment strands old-machine state | `HubRuntime.reassign_work_group`, grouped routing enforcement | High |
| Session identity harms continuity | `_manager_ref`, Hub session metadata handling | High |
| Preflight does not prove the documented contract | `edge_preflight` | High |
| Hub eval is synthetic | `scripts/live_hub_edge_eval.py` | High |

## Rejected Hypotheses

| Hypothesis | Why rejected |
| --- | --- |
| The failure is only a stale ChatGPT manifest | The live manifest problem revealed the issue, but source inspection proves missing capabilities and incorrect state semantics server-side. |
| Adding the eleven missing names fixes Hub | Names alone would still return queue receipts and use non-authoritative group state. |
| ChatGPT is simply selecting tools badly | The required tools and arguments are absent or semantically misleading, so prompting cannot repair the runtime contract. |
| More polling fixes status | Status lacks authoritative group-worker projection; polling stale state more often adds load without truth. |
| Reusing the current JSON queue with more fields is sufficient | Recovery, concurrency, idempotency, ownership, result revisions, and event waiting require transactional semantics. |

## Design Readiness

Status: `READY_FOR_DESIGN`

The evidence supports a systemic control-plane rebuild while preserving the
existing Edge execution runtime.
