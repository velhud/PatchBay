# Multi-Machine PatchBay Orchestration Options

Status: design investigation, not implemented.

This note captures the design space for running PatchBay across several local
computers and virtual machines, then letting ChatGPT coordinate those machines
and their Codex workers.

## Current Baseline

PatchBay today is a single-machine control plane. One Streamable HTTP MCP Server
URL exposes one local PatchBay process, one local runtime state directory, one
local Codex installation, local worker worktrees, local repository locks, local
artifact storage, and local Pro Request state.

It is already multi-client and multi-repository inside that one server:

- multiple ChatGPT conversations or MCP clients can connect to the same Server
  URL and see shared worker, job, artifact, and repository state;
- ownership is coordination, not authentication;
- token-scoped ownership normally lets short-lived ChatGPT transport sessions
  keep working with the same workers;
- `chatgpt_session_ref` and `work_run_ref` help distinguish conversation and
  task/run continuity;
- `scope=current`, `scope=conversation`, `scope=recent`, and `scope=history`
  keep worker status from becoming a historical archive;
- `codex_list_workspaces` discovers repositories within one configured server,
  not machines across a fleet.

The important single-machine assumptions are:

- worker execution is a local Codex CLI subprocess;
- Codex session resume depends on local `CODEX_HOME`, local auth, local session
  records, and local process state;
- isolated worker writes are local `git worktree` checkouts;
- integration applies a patch into the local base checkout;
- repository mutation locks are local to one server and host;
- runtime state lives under local `PATCHBAY_HOME` or `~/.patchbay`;
- MCP sessions and work-run grouping are process memory.

Those facts mean a future multi-machine system should route tasks to machines
and collect reports. It should not pretend a single Codex worker thread can
move freely between hosts.

## Problem To Solve

An operator may have several PatchBay-capable machines:

- local computers in front of him;
- one or more cloud VMs;
- other laptops or workbench machines.

The desired future behavior has two layers.

First, ChatGPT should be able to choose a machine naturally:

> Use the cloud VM for this.

or:

> Use the local Mac because the files are there.

Second, ChatGPT may become a fleet-level manager:

> Send investigation workers to two machines, ask each to inspect its local
> repository state, then compare the reports.

The more ambitious idea is multi-ChatGPT coordination: several ChatGPT
conversations connected to PatchBay could exchange messages, reports, and
worker evidence through a shared coordination layer.

## Topology Options

### Option 1: Independent Connectors Per Machine

Run one PatchBay server per machine and create one ChatGPT connector per
machine, for example:

- `PatchBay - Local Mac`
- `PatchBay - cloud VM`
- `PatchBay - Laptop`

Each machine keeps its own token, tunnel hostname, `PATCHBAY_HOME`, allowed
roots, Codex auth, workers, logs, artifacts, and repository locks.

This works with the current product. It is the lowest-risk immediate path.

Advantages:

- almost no backend change;
- preserves local control and local failure boundaries;
- easy to understand operationally;
- each machine can stay full-power within its own private workbench;
- no distributed locking or remote worker state yet.

Disadvantages:

- ChatGPT sees duplicate tool catalogs if several connectors are enabled;
- there is no single global machine status;
- there is no cross-machine `context_from_workers`;
- ChatGPT must remember which connector corresponds to which machine;
- cross-machine synthesis happens in the ChatGPT conversation, not PatchBay.

This is good as a practical bridge, but not the final orchestration product.

### Option 2: Machine Registry With Handoff

Add a small registry layer that lists known PatchBay machines, their labels,
capabilities, status, URLs, and recommended connector names. ChatGPT asks the
registry which machines exist, then uses the matching machine connector or
creates a handoff for that machine.

Example registry entries:

```yaml
machines:
  - machine_id: cloud-edge-a
    display_name: Cloud Edge A
    roles: [cloud, full-access, private-repos]
    status: online
    connector_name: PatchBay - Cloud
    workspaces: [Documents, SampleRepo, ExampleAPI]
  - machine_id: dev-workstation
    display_name: Local Mac Studio
    roles: [local, documents-canonical, high-storage]
    status: online
    connector_name: PatchBay - Mac
    workspaces: [Projects, Documents]
```

Advantages:

- adds machine discovery without proxying all worker calls;
- preserves one local authority boundary per machine;
- can be implemented as read-only first;
- makes ChatGPT less likely to guess paths or use the wrong machine;
- naturally fits the existing explicit handoff philosophy.

Disadvantages:

- still not seamless fleet orchestration;
- ChatGPT may need multiple connectors enabled;
- the registry itself must avoid exposing private tokens or raw URLs unless the
  operator explicitly wants copy/paste setup output.

This is the recommended first implemented feature.

### Option 3: Central Hub / Gateway

Run one central PatchBay Hub connector that ChatGPT talks to. Machines register
as edge nodes. The hub exposes a single tool catalog and routes commands to the
selected node.

Possible hub tools:

- `patchbay_machine_list`
- `patchbay_machine_status`
- `patchbay_machine_workspaces`
- `patchbay_worker_start_on_machine`
- `patchbay_worker_status_across_machines`
- `patchbay_worker_message_on_machine`
- `patchbay_collect_worker_reports`

Advantages:

- best ChatGPT user experience;
- one connector instead of many duplicate connectors;
- one fleet status view;
- the hub can present machine capabilities and current load clearly;
- eventually supports cross-machine reports, campaigns, and coordination.

Disadvantages:

- the hub becomes a real security and availability boundary;
- every edge node needs stable identity, heartbeat, auth, and capability
  metadata;
- routing mutating calls through a hub requires strong authority boundaries;
- distributed state, queueing, retries, and partial failure become product
  concerns;
- cross-machine repository conflicts need branch/remote discipline, not only
  local locks.

This is the likely long-term architecture, but it should begin as read-only
fleet status and then grow into mutating routing.

### Option 4: Full Mesh / Federation

Every PatchBay instance discovers peers and can route to them. ChatGPT connects
to any node and sees the federation.

Advantages:

- no single hub;
- resilient if one node is unavailable;
- conceptually interesting for local networks.

Disadvantages:

- hardest model for ChatGPT to understand;
- version skew, peer discovery, duplicate events, routing loops, NAT, split
  brain, and trust become core issues;
- too complex before a hub/registry model proves useful.

This should not be the MVP.

### Option 5: Shared Mailbox / Event Bus

Add a general coordination mailbox that ChatGPT conversations, machines, and
workers can use to exchange messages and reports.

Existing primitives that can inspire it:

- Pro Requests already have a runtime store, report, response, event history,
  ownership, sanitized mirror, and explicit dispatch;
- worker reports and `context_from_workers` already pass bounded peer context;
- artifact inbox already transfers files into worker context without editing a
  repository;
- work-run and ChatGPT session refs already separate current task continuity
  from historical state.

Missing primitives:

- stable participant model: machine, ChatGPT conversation, work run, worker,
  human;
- mailbox or channel addressing;
- message id, reply-to, claim, read cursor, delivery status, close/supersede;
- event log and compact projections;
- TTL and stale-message handling;
- idempotency keys;
- cross-machine node id and workspace identity.

This is required for the "three ChatGPT tabs can coordinate with each other"
idea. It can start on one shared PatchBay server before becoming distributed.

## Recommended Path

### Phase 1: Name Machines Explicitly

Add stable machine identity to PatchBay config and self-test output.

Suggested config:

```yaml
machine:
  id: cloud-edge-a
  display_name: Cloud Edge A
  role: cloud-workbench
  tags: [vm, cloud, private-repos, full-access]
  location_hint: cloud-region-a
  owner_label: operator
```

`machine.id` should be stable and operator-chosen. It should not be derived from
hostname, tunnel URL, token, local path, or IP address.

Expose only safe metadata through `codex_self_test`, `codex_inventory`, and
workspace discovery. Do not expose raw tokens, raw tunnel URLs, raw Codex auth
state, absolute private paths beyond existing local workspace responses, or
runtime directories.

### Phase 2: Document Independent Connector Fleet Mode

Support the immediate real-world workflow:

- one PatchBay process per machine;
- one stable hostname per machine where possible;
- one high-entropy token per machine;
- one `PATCHBAY_HOME` per machine/instance;
- one ChatGPT connector per machine with clear naming;
- `--tool-mode worker` by default;
- explicit `--allow-root` for every approved repo root.

This can be used before any hub exists.

### Phase 3: Read-Only Registry

Implement a registry that lets ChatGPT ask:

- which machines exist;
- which are online;
- what each machine is good for;
- what workspaces each machine advertises;
- what connector name or endpoint should be used;
- whether a machine is private, remote, full-power, storage-heavy, CPU-limited,
  or tied to a canonical document root.

This should be read-only first. It should not proxy worker start/message or
integration yet.

### Phase 4: Same-Server Mailbox

Before solving distributed messaging, solve same-server coordination:

- `patchbay_mail_send`
- `patchbay_mail_list`
- `patchbay_mail_read`
- `patchbay_mail_claim`
- `patchbay_mail_reply`
- `patchbay_mail_close`

Messages should be scoped by machine, workspace, ChatGPT session/work run, and
channel. This gives multiple ChatGPT conversations a shared place to coordinate
without dumping every historical worker into status.

Use compact list/status first and full read only on request.

### Phase 5: Hub With Edge Nodes

Add a hub process and edge-node registration:

- each machine runs PatchBay Edge;
- Edge connects outbound to Hub, so local laptops do not need inbound public
  exposure;
- Hub stores node heartbeats, capability summaries, workspace aliases, current
  worker summary, and message/event projections;
- ChatGPT connects to Hub as one connector;
- Hub routes selected calls to a machine node.

Start with read-only status and report collection. Add mutating worker routing
only after registry and mailbox behavior are proven.

### Phase 6: Cross-Machine Worker Teams

Once hub routing is stable:

- ChatGPT starts workers on selected machines;
- workers produce reports on their local machines;
- Hub collects report summaries and artifacts;
- synthesis workers can receive bounded reports from multiple machines;
- machine-level worker status becomes one fleet status view.

Do not migrate Codex sessions across machines. Start a new worker on the target
machine instead.

### Phase 7: Cross-Machine Integration Discipline

Cross-machine writes should use branch and remote discipline:

- machine workers write in isolated local worktrees;
- integration creates patches or branches;
- final base checkout mutation happens on one selected integration machine;
- conflicting machine outputs are resolved by an integration worker;
- GitHub/private remote push is the durable exchange layer when repos are the
  same project on different machines.

Distributed repository locking should not be faked with the current local lock
model.

## Identity Model

Do not overload one "session" concept. Use separate identities:

| Identity | Meaning |
| --- | --- |
| `machine_id` | Stable PatchBay node identity chosen by the operator. |
| access principal | Authenticated caller or token/OAuth principal. |
| `client_ref` | Hashed MCP transport/session reference. |
| `chatgpt_session_ref` | Hashed ChatGPT conversation/session hint when provided. |
| `work_run_ref` | Current task/run grouping inside a conversation. |
| `worker_id` | Durable worker identity on one machine/workspace. |
| workspace identity | Local workspace plus optional canonical alias and repo remote. |

For multi-machine, workspace identity needs more than local path hash. It should
include machine id, local path, optional canonical alias, Git remote URL hash,
branch, and repo root fingerprint.

## Tool Surface Direction

Avoid making ChatGPT choose from several duplicate full tool catalogs if there
is a better single-hub option. Duplicate catalogs increase wrong-tool risk.

MVP independent connectors are acceptable, but the long-term ChatGPT-facing
surface should be one fleet connector with a small number of fleet tools and
machine-targeted worker tools.

Potential hub tool names:

- `patchbay_machine_list`
- `patchbay_machine_status`
- `patchbay_machine_workspaces`
- `patchbay_machine_choose`
- `patchbay_worker_start`
- `patchbay_worker_message`
- `patchbay_worker_status`
- `patchbay_worker_collect_reports`

If hub worker tools wrap current machine worker tools, they should accept
`machine_id` and `repo_alias` rather than raw paths where possible.

## Security Boundary

Each machine is its own authority domain.

Minimum rules:

- separate high-entropy PatchBay token per machine;
- stable machine id is not a secret;
- query-token URLs remain private copy/paste material;
- raw tokens, raw URLs, raw MCP session ids, raw OpenAI metadata, raw Codex
  session ids, backend job ids, absolute runtime/worktree paths, full logs, and
  raw transcripts stay out of normal tool results;
- hub-to-edge registration uses signed node tokens or mTLS later;
- hub never treats soft ownership as authentication;
- edge node enforces local allowed roots and power-tool policy even when the
  hub routes the call;
- public/external/production/paid/credential-changing/irreversible actions
  remain explicit escalation boundaries.

For private workbench machines, full local power is still the intended
mode when authenticated. The security boundary exists to keep power controlled,
not to make PatchBay timid.

## Practical First Deployment Shape

Before implementing a hub, the practical setup is:

```bash
export PATCHBAY_HOME="$HOME/.patchbay-cloud-edge-a"
export PATCHBAY_HTTP_TOKEN="<unique-long-token>"
patchbay start \
  --root /srv/repos \
  --allow-root /srv/documents \
  --tunnel-mode cloudflare-named \
  --hostname patchbay-cloud.example.com \
  --tool-mode worker \
  --save-profile \
  --reveal-token
```

Repeat per machine with a different `PATCHBAY_HOME`, token, hostname, and
machine identity.

In ChatGPT, create connectors with unambiguous names:

- `PatchBay - UCL VM`
- `PatchBay - Local Mac`
- `PatchBay - Laptop`

This is not the final product, but it is already useful and keeps each machine
clear.

## Feasibility

The idea is feasible.

The fastest usable version is easy: independent connectors per machine plus
better machine naming and docs.

The best product version is medium to high complexity: central hub with edge
nodes, stable machine identity, read-only fleet status, then routed worker
operations.

The "multiple ChatGPT tabs coordinate" idea is also feasible, but it needs a
proper mailbox/event model. Do not force it through historical worker lists or
raw logs. Build explicit channels, messages, claims, replies, and compact event
projections.

## Recommendation

Do not build full mesh first.

Build in this order:

1. machine identity and connector naming;
2. independent connector fleet documentation;
3. read-only machine registry;
4. same-server mailbox/event log;
5. central hub with edge registration and read-only fleet status;
6. routed worker start/status/message;
7. cross-machine report collection and synthesis;
8. controlled cross-machine integration workflow through branches/patches.

This path preserves PatchBay's current philosophy: ChatGPT remains the manager,
Codex workers remain real local employees on their machines, natural-language
delegation stays central, and deterministic infrastructure handles only exact
mechanical boundaries such as identity, routing, storage, status, locks, and
audit trails.

