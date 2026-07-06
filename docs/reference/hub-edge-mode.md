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
- Lets the selected edge poll, execute the local `codex_worker_*` command through the existing `ToolHandler`, and post the result back.

V1 uses HTTPS polling. WebSocket streaming, mailbox channels, campaign
coordination, and multiple ChatGPT conversations coordinating through one Hub
are future extensions. The multi-conversation idea is preserved in
[Multi-ChatGPT hub coordination idea](../architecture/multi-chatgpt-hub-coordination-idea.md).

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
- `patchbay_worker_options`
- `patchbay_worker_start`
- `patchbay_worker_message`
- `patchbay_worker_status`
- `patchbay_worker_wait`
- `patchbay_worker_inspect`
- `patchbay_worker_stop`
- `patchbay_worker_integrate`
- `patchbay_command_status`

ChatGPT should start with `patchbay_fleet_status`, choose a machine by
workspace/capability, then route worker commands with explicit `machine_id`.

## Boundaries

- Hub state is a compact projection, not the source of truth for local repos.
- Edge machines keep local Codex auth, repositories, worker state, worktrees,
  logs, and credentials.
- Hub does not receive raw Codex credentials or raw local logs.
- A node token controls one machine only.
- Single-machine `patchbay start` remains unchanged and should remain the
  default for ordinary use.

## Verification

Run:

```bash
python scripts/live_hub_edge_eval.py --json
```

That starts a temporary hub, enrolls a fake edge over HTTP, performs MCP
initialize/fleet status, queues a routed command, has the edge claim it, posts a
result, and verifies command completion.
