# Optional Hub/Edge Mode

Status: V2 implemented and live verified; optional fleet mode, with explicit
V1 compatibility.

Hub and Edge commands use the complete V2 manager control plane by default.
Set `hub.control_plane: v1` only for an intentional legacy deployment; invalid
values fail at startup instead of silently selecting another runtime. Design
and implementation evidence is in the
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

V2 uses HTTPS polling. Multiple ChatGPT conversations can already operate
independent durable groups through one Hub without sharing group-local workers.
WebSocket streaming, mailbox channels, and explicit cross-conversation campaign
coordination are future extensions. The broader multi-conversation idea is preserved in
[Multi-ChatGPT hub coordination idea](../architecture/multi-chatgpt-hub-coordination-idea.md).
Work groups are the current durable coordination object for one ChatGPT-managed
task; future campaigns/channels can build on the same state model.

Normal groups use `execution_mode=end_to_end` with an explicit
`definition_of_done`. Their status includes a derived `completion_contract`.
While that contract reports `manager_must_continue=true` or
`final_response_allowed=false`, the manager should continue the worker loop and
must not reinterpret a wait timeout as a task or platform execution limit.
`asynchronous_handoff` is available only for a deliberately backgrounded task.

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

After first creation, production deployments must pin their existing state:

```yaml
hub:
  require_existing_state: true
  expected_hub_id: "<the existing Hub id>"
  edge:
    require_existing_journal: true
```

Set the Hub values in the Hub config and the Edge guard in every Edge config.
Do not enable them before the first database/journal exists. Once enabled, a
wrong mount, path, Hub database, or missing Edge journal fails startup instead
of silently creating an empty fleet. Keep these guards enabled across ordinary
upgrades. For a schema upgrade, stop the relevant service, create and validate
the exact `--prepare-migration` backup/marker, then start the new release on the
same guarded path.

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

Hub V2 exposes exactly 31 tools in six manager-facing families:

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
explicit patchbay_worker_stop and any needed workspace disposal
patchbay_work_group_close or explicitly report what remains active
```

Hub worker monitoring uses a 20-second minimum and 30-second recommended
cadence. `patchbay_worker_list` and `patchbay_worker_status` share a cached
snapshot per manager/work group during the minimum interval and return
`poll_too_early` guidance instead of repeatedly querying projections.
`patchbay_worker_wait` clamps smaller requests to 20 seconds. This policy does
not throttle worker creation, natural-language follow-up, inspection,
integration, stopping, or workspace tools.

`patchbay_worker_list`, `patchbay_worker_status`, and `patchbay_worker_wait`
are work-group scoped: each call requires `work_group_id` and may narrow only
by lane, active/stopped state, and pagination. Hub does not expose
single-machine `repo_path`, ownership/history `scope`, `owned_only`,
`created_after`, or ignored refresh filters on these tools. Group close records
an outcome and every worker disposition; it never stops workers or cleans
workspaces. Stop workers explicitly and complete any workspace disposal before
close. The exact dispositions are `integrated`, `no_changes`,
`reviewed_failure`, `stopped_preserved`, `discarded`, and `leave_running`;
`discarded` requires `discard_unintegrated_changes=true`. Choosing
`reviewed_failure` in the close call is itself the manager's durable review of
that failed advisory lane; it does not depend on a private Edge-only flag.

Grouped worker calls are inseparable from the repository resolved by the
group's strict preflight. Omit `repo_path` or repeat that exact resolved path;
use a new/reassigned group for another repository. Before a worker exists,
active group preflight recommends waiting through
`patchbay_work_group_status`, not the worker-only wait tool.

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
Repeating the exact same call with the same key reuses both its durable
operation and its domain object (group, worker, or batch child); it must not
create a second group, preflight, or worker. Reusing a key with different
arguments fails with `idempotency_payload_conflict`.

The `patchbay_worker_start_batch` parent is a Hub-side aggregate, not an Edge
command. While child starts are active it reports `aggregate_running` with
`wait_for_child_operations`, has no Edge attempt of its own, and preserves each
child's terminal outcome before becoming complete. New batches commit the
parent, compact child manifest, every child operation, and every durable Edge
dispatch in one SQLite transaction. A historical or damaged batch missing its
manifest, a child, or a child dispatch returns `recovery_required` with exact
missing item IDs. Do not wait forever and do not invent an internal recovery
tool: inspect the returned recovery details, preserve the old operation ID for
evidence, and submit deliberate replacement work only when the manager-level
guidance says replacement is safe.

Transport reconciliation is automatic. ChatGPT does not receive and must not
need a lease-transition or `complete_reconciliation` tool. For nonterminal
operations, `patchbay_operation_status` returns a callable bounded status wait
for the same operation. Hub and Edge use durable attempt fences to recover an
exact result or create one idempotent successor only when Edge proves that the
original effect never began.

The production Hub owns a bounded background recovery dispatcher. It reoffers
durable operations left dispatchable by a process crash even when no manager is
polling. Read/status calls do not dispatch unrelated writes, and a remote read
dispatches only its own newly created operation. This separation prevents
monitoring from becoming an accidental mutation scheduler while still making
restart recovery independent of ChatGPT activity.

`patchbay_work_group_status` computes completion counts from indexed durable
state, then returns workers, operations, and integration records as separate
bounded pages. Each page defaults to 100 records and exposes its own cursor,
limit, total, and truncation metadata. `include_integrations=false` really
omits integration detail without weakening aggregate completion truth.

The Edge keeps reconciliation memory bounded independently of retained journal
history. Pending work is coalesced by immutable operation/attempt/fence
identity, full records are hydrated from SQLite only for the current bounded
batch, acknowledged receipts release retry metadata, and restart traversal is
paged fairly. Terminal `acknowledged` and `manual_recovery` attempts are not
replayed as pending work. Repeated 409 responses therefore cannot append a new
copy of the same historical reconciliation graph on every control-loop pass.

Hub MCP transport sessions are also bounded: idle sessions expire after 24
hours by default and the process retains at most 1,024 session records, without
evicting in-flight requests. Worker polling snapshots are capped at 1,024
manager/group identities. These are memory-retention limits, not limits on
durable work groups, workers, or historical Hub state.

The Edge's current session contract and an attempt's immutable contract are
separate. Heartbeats and request authentication use the current contract;
attempts and result receipts retain the contract from their original claim.
This lets an in-flight or retained result complete safely during a rolling
upgrade without accepting a result for a different attempt.

Do not confuse a compatible V2 patch rollout with the first V1-to-V2 cutover.
The first cutover follows the atomic migration runbook and permits no mixed V1/
V2 mutation intake. For an already-V2 fleet, take transactionally consistent
Hub and Edge backups, pause new mutations during version skew, update the Hub,
then update Edges sequentially. Reopen mutation intake only after contract
hashes match, retained receipts reconcile, and fleet status is current.

For a state-preserving V2 rollout:

1. Stop appointing new workers and inspect every open work group. Let active
   Codex turns finish; do not equate a quiet turn with a drained turn.
2. Record the Hub revision, enrolled machine generations, active operations,
   workers, unintegrated workspaces, and retained reconciliation receipts.
3. Use SQLite's backup API for the Hub database and each Edge journal. A plain
   file copy of the main database is not sufficient while WAL mode is active.
   Run `PRAGMA integrity_check` against every backup.
   On an already-upgraded V2 Hub, `patchbay hub backup create` coordinates with
   the running process through the private admission-lock directory beside the
   database: new mutations and Edge claims pause, admitted dispatch sections
   drain, and result/reconciliation traffic continues. The first deployment
   from a version that does not implement this shared gate must briefly stop
   the old Hub before taking the rollout backup.
4. After turns are drained, preserve Edge job-state files, Codex session state,
   worktree metadata, and the worktree roots as one consistent filesystem
   snapshot. Record hashes and the deployed commit beside the backup manifest.
5. Update and restart the Hub first without changing its public MCP URL. Update
   one Edge at a time, retaining the same machine id, generation, mounted state,
   Codex home, and workspace roots.
6. Before admitting new work, require current fleet heartbeats, matching
   contracts, an empty or understood retained-receipt queue, and authoritative
   status for every pre-existing open group.
7. Resume one pre-existing worker by sending a natural-language follow-up after
   the restart. Confirm that the same worker/session/workspace continues and
   that no duplicate worker or operation was created.

If an active turn cannot be drained, postpone that Edge. Do not deploy through
it and do not delete or rewrite its durable state merely to make the rollout
look clean.

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
`CatalogApp` can resolve to the pinned machine's local
`<advertised-workspace-root>/CatalogApp`. The group stores both the requested
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
- Edge worker projection preserves full-history continuity and recomputes
  terminal entries from durable worker/workspace state. PatchBay deliberately
  does not keep a terminal cache that could hide repository changes made
  outside PatchBay. A malformed worker projection is represented by a compact,
  sanitized error entry while all other workers remain visible.
- When one worker projection is absent, Hub can route focused inspect/message
  through the durable fleet-worker identity scoped to the same group, machine,
  and generation. The response marks `projection_missing: true`; Edge remains
  authoritative and workers from other groups are never substituted.
- Hub receives and durably stores manager-supplied worker briefs and the bounded
  operation arguments needed to dispatch and replay work after interruption.
  This is private authenticated runtime state, not public audit output. Hub does
  not receive raw Codex credentials, raw local logs, repository file contents,
  or private paths beyond already-advertised workspace projections. Operators
  must protect, back up, and retire Hub state with the same care as task history.
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
python scripts/live_hub_v2_eval.py --json
```

The first command checks availability routing. The V2 evaluator runs the exact
Hub behind loopback TCP, enrolls and drives two Edge runners across that network
boundary, verifies the exact 31-tool manager catalog, independent manager
groups, aggregate batch semantics, same-worker continuation, stale-preview
replacement, integration, receipt/reconciliation recovery, restart persistence,
and authoritative closure. The availability evaluator starts a temporary hub,
enrolls fake edges over HTTP, performs MCP
initialize/fleet status, creates a work group, completes group preflight,
queues grouped workers, verifies the group stays pinned after machine load
changes, and verifies command completion.
