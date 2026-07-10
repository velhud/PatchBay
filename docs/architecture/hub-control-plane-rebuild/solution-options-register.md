# Solution Options Register

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

## Option Summary

| ID | Name | Family | Decision |
| --- | --- | --- | --- |
| O1 | Add missing Hub tool names and fields | Local patch | Rejected |
| O2 | Generate canonical schemas but retain current command/state model | Boundary patch | Rejected |
| O3 | Transparent Hub adapter plus authoritative durable control plane | Systemic lifecycle repair | Selected |
| O4 | Hide groups/routing and make worker start create everything implicitly | Product simplification | Rejected as complete solution; selected only for reducing ordinary steps |
| O5 | Preserve current Hub and rely on prompting | No-change/prompt repair | Rejected |

## O1: Add Missing Names And Fields

Mechanism: expand `HUB_TOOLS` with worker list, inbox, direct inspection, Pro
Requests, and copied old fields.

Benefits:

- fast visible parity;
- limited initial code movement.

Why rejected:

- retains duplicate descriptor ownership and future drift;
- tools still return queue receipts;
- group state remains non-authoritative;
- leases, ownership, reassign, and close remain broken;
- produces a larger illusion rather than a functioning control plane.

## O2: Generate Schemas, Keep Current Queue Model

Mechanism: mechanically derive descriptors from canonical Edge tools but retain
the JSON command queue, current `finish_command`, machine reassignment, and group
projection.

Benefits:

- solves schema parity;
- preserves most current code;
- easier staged rollout.

Why rejected as final architecture:

- transport and operation state remain conflated;
- no authoritative fleet worker identity/projection;
- recovery and close semantics remain unsafe;
- long waits still block the Edge loop;
- generated schemas alone do not make tools semantically truthful.

Reusable part: canonical descriptor generation is included in O3.

## O3: Transparent Adapter And Authoritative Control Plane

Mechanism:

- generate routed tools from canonical contracts;
- add stable group/lane/workspace/fleet-worker routing envelopes;
- introduce transactional versioned state;
- separate dispatch, operation, worker, readiness, activity, and integration
  state;
- wait briefly for real Edge results and expose opaque operations only when
  genuinely pending;
- implement Edge-side idempotency and Hub reconciliation;
- derive group truth from Edge worker projections;
- create successor groups for machine moves;
- prove the whole lifecycle with real Edge and Codex runs.

Why selected:

- repairs root causes at their owners;
- preserves mature worker behavior;
- keeps Hub complexity behind natural manager tools;
- supports safe retries and multi-session continuity;
- is observable and testable;
- creates a base for future multi-conversation coordination without pretending
  that future channels/campaigns already exist.

Tradeoff: broader implementation than O1/O2, including state migration and
protocol versioning. This is accepted because the current boundaries are the
cause of repeated failures.

## O4: Fully Hide Groups And Routing

Mechanism: `worker_start` implicitly creates or finds a task and chooses a
machine; ChatGPT sees only workers.

Benefits:

- smallest ordinary sequence;
- low tool-selection burden.

Why rejected as complete solution:

- hides the durable task object needed for cross-session resumption;
- makes explicit machine placement, collision review, group close, and recovery
  harder;
- weakens the user's desired group/team mental model.

Selected portion: group creation should do routing and preflight internally and
return a useful result, so ordinary work does not require machine recommendation
or command polling.

## O5: Prompting And No Runtime Change

Mechanism: strengthen ChatGPT instructions and ask it to interpret queue receipts
carefully.

Why rejected:

- prompts cannot expose missing tools;
- prompts cannot make group status authoritative;
- prompts cannot implement idempotency, recovery, visibility, or schema truth;
- repeats the failed assumption that manager behavior can compensate for an
  incomplete harness.
