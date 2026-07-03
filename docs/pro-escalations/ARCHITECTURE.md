# PatchBay Pro Escalations Architecture

Pro Escalations are PatchBay's reverse handoff path: local Codex or the local operator can package a blocked problem for ChatGPT Pro, ChatGPT stores a durable answer through MCP, and PatchBay can explicitly dispatch that answer back to an idle origin worker or a new isolated worker.

This is not the artifact inbox and not a generic task queue. The artifact inbox moves ChatGPT-generated files into local worker context. Pro Escalations move local diagnostic reports to ChatGPT Pro and store the answer as local runtime state.

## Runtime Model

`ProRequestStore` is the canonical store. It writes manifests, reports, responses, attachments, and events under PatchBay runtime storage, outside repository checkouts by default. Repository mirrors under `.ai-bridge/pro-requests/<request-id>/` are sanitized convenience artifacts for local visibility; they are not the source of truth.

Each Pro Request records:

- a stable `proreq_...` id;
- compact repo metadata, branch, head commit, and dirty summary;
- origin metadata, including an optional worker name;
- bounded report Markdown and optional attachments;
- owner metadata for shared MCP-session coordination;
- response metadata and optional worker-ready message;
- routing status for explicit dispatch;
- event history for create, claim, respond, dispatch, close, cancel, stale, and supersede operations.

Public views deliberately omit local repository paths, backend job ids, raw Codex session ids, raw transcripts, and raw runtime file paths.

## Tool Surface

The public MCP tools are:

| Tool | Role |
| --- | --- |
| `codex_pro_request_list` | Read open or recent requests. |
| `codex_pro_request_read` | Read one bounded report, response, attachments index, and staleness check. |
| `codex_pro_request_claim` | Claim ownership for the current MCP connection. |
| `codex_pro_request_respond` | Store ChatGPT Pro's answer only. No worker is messaged, no files are edited, no code is applied. |
| `codex_pro_request_dispatch` | Explicitly message an idle origin worker or start a new isolated worker with the stored answer. |
| `codex_pro_request_close` | Close, cancel, or supersede a request. |

The tools are installed in `standard`, `full`, and `worker` modes because Pro Escalations are part of the normal ChatGPT-to-local coordination surface. `list` and `read` are read-only. `claim`, `respond`, `dispatch`, and `close` mutate runtime state. `dispatch` is open-world because it can start or message a local Codex worker.

## Dispatch Boundary

`codex_pro_request_respond` and `patchbay pro-request response` only store text. They do not resume workers, start workers, apply patches, write source files, or commit.

`codex_pro_request_dispatch` is the explicit boundary crossing:

- `target: "origin_worker"` messages the recorded origin worker only when it is available and idle.
- `target: "new_worker"` starts a named worker, defaulting to `isolated_write`.
- busy or missing origin workers return `dispatch_blocked`; PatchBay does not silently queue work.
- dispatch never integrates worker output into the base checkout and never commits.

## CLI Surface

Local operators can use:

```bash
patchbay pro-request create --repo /path/to/repo --title "Blocked issue" --report report.md
patchbay pro-request list
patchbay pro-request show proreq_...
patchbay pro-request response proreq_...
patchbay pro-request dispatch proreq_... --target origin_worker
patchbay pro-request close proreq_... --reason done
```

The CLI uses the same store and handler boundary as MCP, so dispatch semantics and validation stay aligned.
