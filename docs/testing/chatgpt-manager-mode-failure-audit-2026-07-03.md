# ChatGPT Manager-Mode Failure Audit - 2026-07-03

## Scope

This audit investigates why a real ChatGPT + PatchBay session at about 16:00
Europe/Paris on 2026-07-03 still behaved like a manual repository reader even
after manager-first instructions were added to PatchBay.

The user intent is not to remove read/search tools. Direct context tools must
remain available for orientation, worker briefing context, focused verification,
exact line/diff checks, reviewing worker evidence, specific doubts, and tiny
tasks. The intended behavior is that ChatGPT uses PatchBay primarily as a
manager of named Codex workers for non-trivial repository/Documents/codebase
work.

## Evidence

VM journal logs for the 16:00-16:35 Paris window showed the following tool-call
shape:

- direct orientation/read/search/tree calls: 265;
- worker-related calls: 19;
- `codex_worker_start`: 2;
- `codex_worker_message`: 0;
- one worker completed, one worker was cancelled;
- aliases such as `read`, `search`, `tree`, `workspace_snapshot`,
  `open_workspace`, and `open_current_workspace` appeared in the call stream.

That pattern is not manager-first. It means ChatGPT used workers as side checks
while continuing to investigate primarily through direct file tools.

The live VM service was launched with:

```text
--tool-mode full
```

The checked-in config also used:

```yaml
app:
  tool_mode: full
```

In `full` mode PatchBay advertises compatibility aliases including `read`,
`search`, `tree`, `workspace_snapshot`, `show_changes`, and power-user tools
where runtime settings allow them. In `worker` mode PatchBay keeps the context
tools needed for orientation and verification, keeps the worker tools, and hides
compatibility aliases plus low-level job/session/power tools.

## Root Cause

The main root cause was not absence of manager instructions. ChatGPT did see
worker tools and used them. The failure was that the deployed ChatGPT app exposed
the wrong product surface: full mode.

OpenAI Apps SDK guidance treats tool metadata and the visible tool set as a
major part of model tool selection. Tool descriptions should make it clear when
to use a tool, and the tool surface should be sanity-checked against the prompts
the model will receive. PatchBay had manager-first prose, but it also advertised
many attractive manual-reader tools and short generic aliases. For broad
document/repository work, that surface made the manual loop too natural.

In other words:

```text
Good instructions + wrong visible tool surface = unreliable manager behavior.
```

The failure was intensified by a documentation/config contradiction:

- active docs recommended `--tool-mode worker` for first real ChatGPT
  validation;
- a stale worker-bridge note said not to switch checked-in defaults to worker
  mode until real ChatGPT eval passed;
- the VM service and checked-in config still used `full`;
- the live MCP eval proved full-power mechanics rather than proving the
  worker-first ChatGPT surface.

## What Changed

PatchBay now defaults missing and checked-in tool mode to `worker`.

`full` mode still exists. It is the deliberate compatibility/power-user surface,
not the default ChatGPT manager surface.

The distinction is:

- runtime authority may remain full-authority: broad roots, direct write enabled,
  full bash enabled, Codex session reads enabled;
- ChatGPT-facing default catalog is `worker`: manager-first tools plus focused
  context/verification tools, without compatibility aliases and low-level power
  controls.

This preserves the philosophy that PatchBay should be powerful and should not
babysit AI with primitive prompt filters. It changes the product surface so the
model naturally starts from worker management.

## Regression Standard

A default live MCP eval must prove:

- `tool_mode` is `worker`;
- worker tools are advertised;
- direct context tools such as `codex_read_file` and `codex_search_repo` remain
  advertised;
- compatibility aliases such as `read`, `search`, `tree`, and `workspace_snapshot`
  are absent;
- low-level power tools are absent from the default ChatGPT surface even when
  runtime authority exists underneath.

Full-mode compatibility remains testable by explicitly running:

```bash
python scripts/live_mcp_eval.py --tool-mode full --json
```

## Deployment Rule

Production ChatGPT connector deployments should start with:

```bash
--tool-mode worker
```

Use `codex_tool_mode_info` before broadening. Use `codex_tool_mode_switch` or a
separate full-mode endpoint only when ChatGPT truly needs power-user controls.
Switch back to `worker` afterward when the host can see the refreshed catalog.

If a real ChatGPT session again performs broad work with many direct reads/searches
and few worker starts/messages, treat that as a product-surface regression first,
not as a reason to remove reader tools.
