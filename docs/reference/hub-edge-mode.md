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

A private deployment may also mount the Hub at an existing copied ChatGPT
Server URL such as `/patchbay/mcp` so the operator does not have to recreate the
ChatGPT connector. In that rollout shape, keep the old single-machine runtime
available on a separate fallback path while `/patchbay/mcp` serves Hub tools.

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

### Windows WSL Edge Persistence

For a Windows laptop or workstation running the edge inside WSL, do not assume a
successful manual `systemctl --user start` means the edge will remain online
after the SSH/terminal session closes. Some WSL setups stop the user service
manager shortly after the last interactive WSL process exits. The symptom is:

```text
Hub briefly shows the WSL edge online
about 10-30 seconds later the edge goes offline
the edge service journal shows a clean stop, not a crash
```

Use a persistent Windows-side launcher for always-on edges:

1. Enable linger for the Linux edge user:

   ```bash
   sudo loginctl enable-linger patchbay
   ```

2. Keep WSL alive from Windows, for example with a per-user Scheduled Task that
   starts at logon and runs a small PowerShell keepalive script. The script
   should start the WSL user service and then keep one WSL process alive:

   ```powershell
   while ($true) {
     & wsl.exe -d Ubuntu-24.04 -u patchbay -- bash -lc "export XDG_RUNTIME_DIR=/run/user/1001; systemctl --user start patchbay-edge.service || true; exec sleep infinity"
     Start-Sleep -Seconds 10
   }
   ```

3. Verify from the Hub after waiting longer than the previous shutdown window.
   The edge should still show `online`, its heartbeat age should remain fresh,
   and `max_concurrent_jobs` / free slots should match the edge config.

This is an operating-system persistence requirement, not a Hub routing problem.
The Hub can only route to an edge that is actually heartbeating.

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

When a work group closes and no command is still queued or running, Hub settles
non-problem lane statuses to `idle`. The closed group still preserves worker
refs, command records, reports, and worktrees, but default status/count output
should not make completed lanes look like current active work.

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

Default fleet views hide retired or superseded machines. A retired machine is
an old edge enrollment preserved for audit/history after an operator deliberately
replaced or decommissioned it. ChatGPT should not treat retired entries as
current capacity and should not route work to them. Use
`patchbay_machine_list` with `include_retired: true` only when diagnosing why an
expected machine is missing.

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

`repo_path` can be a human repo name, a machine-local absolute path, or an
advertised workspace alias. When a machine advertises a non-git workspace root,
the Hub can resolve a safe relative repo name underneath that root. For example,
`RetailMind` can resolve to the pinned machine's local
`<advertised-workspace-root>/RetailMind`. The group stores both the requested
value and the resolved machine-local path. Edge preflight remains the source of
truth: it must prove the resolved path exists, is allowed, and is the intended
repo before grouped workers can start. If a machine advertises both a broad
workspace root and a specific repository alias, the specific advertised
repository wins. This keeps a request such as `PatchBay` pinned to the
advertised PatchBay checkout instead of a generic projects folder that happens
to be able to contain a child named `PatchBay`.

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
  max workers, free slots, queue flag, CPU pressure, memory pressure, and disk
  capacity numbers for the work/log/repo area. CPU, memory, and disk telemetry
  are source-labeled in hub recommendations. CPU uses `/proc/stat` deltas after
  the first sample and falls back to a one-minute load-average pressure
  estimate. Memory comes from `/proc/meminfo`, so on WSL it is the Linux edge's
  visible/usable memory, not necessarily the laptop's physical RAM.
- Disk telemetry is source-labeled. On WSL, Linux may report the virtual
  ext4/VHD capacity rather than real Windows-host free space; if PatchBay cannot
  read a Windows host disk or an operator-configured override, the edge marks
  disk telemetry as `virtualized` and does not present that virtual number as
  effective routing free space.
- A node token controls one machine only.
- Retiring a machine disables its old node token for heartbeat/claim calls,
  hides it from default fleet status/list/workspace views, and excludes it from
  recommendations. It does not delete history or rewrite old commands/groups.
- Single-machine `patchbay start` remains unchanged and should remain the
  default for ordinary use.

## Retiring Or Restoring Edge Enrollments

Use operator CLI commands for lifecycle administration; this is not part of the
normal ChatGPT manager workflow.

```bash
patchbay hub machine retire <machine-id> \
  --reason "superseded by replacement edge" \
  --superseded-by <replacement-machine-id> \
  --json
```

Retirement is for stale, replaced, or intentionally decommissioned edge IDs. It
preserves audit history while preventing old enrollments from confusing normal
fleet status or availability routing. If an edge was retired by mistake and the
same node token/profile should be allowed again:

```bash
patchbay hub machine restore <machine-id> --json
```

For a truly new machine identity, create a fresh enrollment code and enroll the
edge under the new `machine_id` instead of restoring an obsolete one.

For WSL or other virtualized edges where the host disk is not readable, set a
conservative explicit override on the edge:

```yaml
hub:
  edge:
    resource_overrides:
      disk_free_bytes: 250000000000
      disk_total_bytes: 1000000000000
      disk_source: windows-host-configured
```

The equivalent environment variables are
`PATCHBAY_EDGE_DISK_FREE_BYTES`, `PATCHBAY_EDGE_DISK_TOTAL_BYTES`,
`PATCHBAY_EDGE_DISK_USED_PERCENT`, and `PATCHBAY_EDGE_DISK_SOURCE`.

## Verification

Run:

```bash
python scripts/live_hub_edge_eval.py --json
```

That starts a temporary hub, enrolls fake edges over HTTP, performs MCP
initialize/fleet status, creates a work group, completes group preflight,
queues grouped workers, verifies the group stays pinned after machine load
changes, and verifies command completion.
