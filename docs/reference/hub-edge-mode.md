# Optional Hub/Edge Mode

Status: V2 implemented and live verified; optional, with V1 compatibility.

Set `hub.control_plane: v2` on the Hub and every Edge to use the complete
manager control plane. Omitting the setting preserves the V1 compatibility
runtime. Design and implementation evidence is in the
[Hub Manager Control Plane Rebuild](../architecture/hub-control-plane-rebuild/README.md).

PatchBay normally runs as one local MCP server connected to one machine. Hub/edge
mode adds an optional fleet layer:

```text
ChatGPT -> PatchBay Hub -> PatchBay Edge machine(s) -> local Codex workers
```

Use it when one ChatGPT connector should see several machines and route Codex
worker tasks to the right one. Do not enable it for ordinary single-machine
PatchBay use.

## What V2 Does

- Runs a separate `patchbay hub start` MCP server.
- Enrolls machines with short-lived one-use pairing codes.
- Stores transactional SQLite/WAL state privately under `PATCHBAY_HOME`, or
  `hub.state_db` when configured.
- Stores edge profiles privately under `PATCHBAY_HOME/runtime/hub/edge-profile.json`.
- Lets each edge advertise local capabilities, allowed workspaces, and compact worker status.
- Exposes the exact 31-tool manager surface and returns semantic worker results
  or durable pending operations, not synthetic command-success receipts.
- Adds durable work groups: one user task becomes one group, lanes are workers
  inside that group, and the group is pinned to one machine.
- Lets the architect choose each group's shared-checkout policy: serialized by
  default, or manager-controlled concurrent `shared_write` when explicitly
  selected and coordinated.
- Optionally chooses the least-busy eligible online machine when a work group is
  created and no explicit `machine_id` is supplied.
- Lets the selected Edge claim fenced operations, execute the mature local
  `codex_worker_*` action through the existing `ToolHandler`, publish worker
  projections, and durably upload results.
- Keeps heartbeat, projection, claim, execution, lease renewal, result upload,
  and reconciliation independently scheduled.

V2 uses HTTPS polling. WebSocket streaming, mailbox channels, campaign
coordination, and multiple ChatGPT conversations coordinating through one Hub
are future extensions. The multi-conversation idea is preserved in
[Multi-ChatGPT hub coordination idea](../architecture/multi-chatgpt-hub-coordination-idea.md).
Work groups are the current durable coordination object for one ChatGPT-managed
task; future campaigns/channels can build on the same state model.

## Start A Hub

```bash
export PATCHBAY_HOME="$HOME/.patchbay-hub"
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
# Set `hub.control_plane: v2` in config.yaml.
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
# Set `hub.control_plane: v2` in config.yaml.
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

Hub V2 exposes exactly 31 tools in five manager-facing families:

- fleet and discovery: `patchbay_fleet_status`, `patchbay_workspace_list`;
- groups: `patchbay_work_group_create`, `patchbay_work_group_list`,
  `patchbay_work_group_status`, `patchbay_work_group_resume`,
  `patchbay_work_group_reassign`, `patchbay_work_group_close`;
- workers and artifacts: `patchbay_worker_options`, `patchbay_worker_inbox`,
  `patchbay_worker_start`, `patchbay_worker_start_batch`,
  `patchbay_worker_message`, `patchbay_worker_list`, `patchbay_worker_status`,
  `patchbay_worker_wait`, `patchbay_worker_inspect`,
  `patchbay_worker_integrate`, `patchbay_worker_stop`;
- exceptional manager inspection: `patchbay_workspace_open`,
  `patchbay_workspace_tree`, `patchbay_workspace_search`,
  `patchbay_workspace_read_file`, `patchbay_workspace_changes`;
- Pro Requests and recovery: `patchbay_pro_request_list`,
  `patchbay_pro_request_read`, `patchbay_pro_request_claim`,
  `patchbay_pro_request_respond`, `patchbay_pro_request_dispatch`,
  `patchbay_pro_request_close`, `patchbay_operation_status`.

Normal lifecycle:

```text
patchbay_fleet_status
patchbay_workspace_list
patchbay_work_group_list
patchbay_work_group_resume or patchbay_work_group_create
patchbay_work_group_status until Edge preflight is ready
patchbay_worker_start_batch and/or patchbay_worker_start inside named lanes
patchbay_worker_wait / patchbay_worker_status
patchbay_worker_message for corrections and follow-up turns
patchbay_worker_inspect for reports, files, changes, and integration preview
patchbay_worker_integrate only after accepting a signed preview
patchbay_work_group_close or explicitly report what remains active
```

One user task equals one durable group. Do not create one group per worker.
Group creation performs availability-only placement once and pins the group to
that machine. Worker starts infer the machine from `work_group_id`; they do not
route independently. If the pinned machine is unavailable, wait or explicitly
create successor work with `patchbay_work_group_reassign`. Never pretend that a
live Codex process moved between machines.

Mutating tools require a caller-stable `idempotency_key`. A `pending` result
means Hub accepted the durable operation but Edge/Codex has not yet produced a
semantic result. Continue with `patchbay_operation_status` or the relevant
worker/group wait tool; do not repeat the mutation with a new key.

Work groups accept `shared_write_policy=serialized|manager_controlled`.
`serialized` keeps the per-repository mutation lock. `manager_controlled`
permits multiple workers to write directly in the same base checkout because
the architect has accepted responsibility for ownership boundaries and
conflict reconciliation. PatchBay reports the policy and concurrency; it does
not semantically second-guess the architect. `isolated_write` remains the
recommended default for independent parallel implementation.

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
- Edge preflight reports bounded repository-local Python environments such as
  `.venv/bin/python` and `.venv/bin/pytest` when present. This is discovery and
  guidance, not a restriction: workers may create a repo-local environment and
  install missing development dependencies when required by the task.
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
