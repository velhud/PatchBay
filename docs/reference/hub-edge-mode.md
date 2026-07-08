# Optional Hub/Edge Mode

Status: V1 implemented, optional, not the default runtime.

PatchBay normally runs as one local MCP server connected to one machine. Hub/edge
mode adds an optional fleet layer:

```text
ChatGPT -> PatchBay Hub -> PatchBay Edge machine(s) -> local Codex workers
```

Use it when one ChatGPT connector should see several machines and route Codex
worker tasks to the right one. Do not enable it for ordinary single-machine
PatchBay use.

## What V1 Does

- Runs a separate `patchbay hub start` MCP server.
- Enrolls machines with short-lived one-use pairing codes.
- Stores hub state privately under `PATCHBAY_HOME`, or `hub.state_file` when configured.
- Stores edge profiles privately under `PATCHBAY_HOME/runtime/hub/edge-profile.json`.
- Lets each edge advertise local capabilities, allowed workspaces, and compact worker status.
- Lets ChatGPT queue worker commands for a selected `machine_id`.
- Adds durable work groups: one user task becomes one group, lanes are workers
  inside that group, and the group is pinned to one machine.
- Optionally chooses the least-busy eligible online machine when a work group is
  created and no explicit `machine_id` is supplied.
- Lets the selected edge poll, execute the local `codex_worker_*` command through the existing `ToolHandler`, and post the result back.

V1 uses HTTPS polling. WebSocket streaming, mailbox channels, campaign
coordination, and multiple ChatGPT conversations coordinating through one Hub
are future extensions. The multi-conversation idea is preserved in
[Multi-ChatGPT hub coordination idea](../architecture/multi-chatgpt-hub-coordination-idea.md).
Work groups are the current durable coordination object for one ChatGPT-managed
task; future campaigns/channels can build on the same state model.

## Start A Hub

```bash
export PATCHBAY_HOME="$HOME/.patchbay-hub"
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
patchbay hub start --config config.yaml --host 127.0.0.1 --port 8000
```

Connect ChatGPT to the hub `/mcp` URL, not to each edge machine. If the hub is
behind a tunnel, use the same tokenized URL pattern as normal PatchBay:

```text
https://example.com/patchbay-hub/mcp?patchbay_token=<token>
```

## Enroll An Edge Machine

On the hub machine:

```bash
patchbay hub enroll-code create --name "Dev Mac Studio" --tag local --tag documents
```

On the edge machine:

```bash
export PATCHBAY_HOME="$HOME/.patchbay-edge"
patchbay edge enroll \
  --hub https://example.com/patchbay-hub \
  --code PB-ABCD-1234 \
  --machine-id dev-mac-studio \
  --machine-name "Dev Mac Studio" \
  --tag local \
  --tag documents
```

Then start the edge loop:

```bash
patchbay edge start --config config.yaml
```

For a one-cycle diagnostic:

```bash
patchbay edge run-once --config config.yaml --json
```

## ChatGPT-Facing Hub Tools

Hub mode exposes fleet-native tools, not every direct local file tool from every
machine:

- `patchbay_fleet_status`
- `patchbay_machine_list`
- `patchbay_machine_workspaces`
- `patchbay_machine_recommend`
- `patchbay_work_group_create`
- `patchbay_work_group_list`
- `patchbay_work_group_status`
- `patchbay_work_group_resume`
- `patchbay_work_group_close`
- `patchbay_work_group_reassign`
- `patchbay_worker_options`
- `patchbay_worker_start`
- `patchbay_worker_start_auto`
- `patchbay_worker_message`
- `patchbay_worker_status`
- `patchbay_worker_wait`
- `patchbay_worker_inspect`
- `patchbay_worker_stop`
- `patchbay_worker_integrate`
- `patchbay_command_status`

For non-trivial tasks, ChatGPT should use this lifecycle:

```text
patchbay_fleet_status
patchbay_work_group_list
patchbay_work_group_resume or patchbay_work_group_create
patchbay_work_group_status
patchbay_worker_start_auto or patchbay_worker_start with work_group_id/lane
patchbay_work_group_status until work completes
patchbay_work_group_close or report what remains active
```

Hard rules for ChatGPT:

- one user task equals one work group;
- do not create one group per worker;
- do not call `patchbay_worker_start_auto` before a group exists;
- do not treat `patchbay_machine_recommend` as permission to scatter workers;
- do not start grouped workers until group preflight is `ok`;
- if the pinned machine is full/offline, wait, queue there when allowed, or
  explicitly reassign the group;
- use separate groups/branches/integration owners for deliberate cross-machine
  same-repo work.

`patchbay_worker_start_auto` is no longer a per-worker scatter router. It
requires `work_group_id`, `lane`, and `auto_routing_ok: true`, then queues the
worker on the group's pinned machine. `patchbay_worker_start` with explicit
`machine_id` still exists for tiny checks, operator-requested work, and legacy
compatibility, but ungrouped starts must supply `ungrouped_reason`.

If `hub.routing.enabled` is true, ChatGPT may use
`patchbay_work_group_create` without `machine_id`. The Hub then chooses one
eligible machine by compact availability telemetry and pins the group there.
This router is deliberately simple and mechanical: it uses online state, worker
slots, CPU, memory, disk feasibility, workspace projections, allow-lists, and
explicit `required_tags`. It does not classify task meaning, complexity, model
choice, or coding-vs-documentation intent.

Once a group is pinned, later grouped workers stay on that machine even if
another machine becomes less busy. If the pinned machine is full or offline,
Hub returns a capacity/unavailable block or queues there when policy permits; it
does not silently fail over. Use `patchbay_work_group_reassign` only when the
user explicitly wants successor work on a different machine. Reassigning does
not migrate live Codex processes.

Public/default config keeps routing disabled:

```yaml
hub:
  routing:
    enabled: false
    min_disk_free_bytes: 2147483648
    allow_queue_when_full: false
    weights:
      worker_ratio: 0.60
      memory_ratio: 0.20
      cpu_ratio: 0.20
```

Private deployments can enable only:

```yaml
hub:
  routing:
    enabled: true
```

## Boundaries

- Hub state is a compact projection, not the source of truth for local repos.
- Hub work groups are coordination objects, not security boundaries.
- Hub preflight is required before grouped worker starts: the edge confirms the
  repo/workspace can be resolved, reports compact git/capacity facts, and then
  worker starts are allowed unless an operator recovery override is used.
- Edge machines keep local Codex auth, repositories, worker state, worktrees,
  logs, and credentials.
- Hub does not receive raw Codex credentials, raw local logs, prompts, file
  contents, or private paths beyond already-advertised workspace projections.
- Edge heartbeat resource telemetry is compact: active worker count, configured
  max workers, free slots, queue flag, CPU percent when cheaply available,
  memory pressure, and disk capacity numbers for the work/log/repo area.
- A node token controls one machine only.
- Single-machine `patchbay start` remains unchanged and should remain the
  default for ordinary use.

## Verification

Run:

```bash
python scripts/live_hub_edge_eval.py --json
```

That starts a temporary hub, enrolls fake edges over HTTP, performs MCP
initialize/fleet status, creates a work group, completes group preflight,
queues grouped workers, verifies the group stays pinned after machine load
changes, and verifies command completion.
