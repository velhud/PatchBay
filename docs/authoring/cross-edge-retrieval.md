# Cross-Edge handoff retrieval record (C5)

`patchbay_work_group_reassign` links a predecessor group to a successor on a
different Edge generation. `carry_context="reports_and_changes"` stages up to
50 bounded report metadata/summary records. The result explicitly reports
`artifacts_transferred: false` and `changes_transferred: false`; files, diffs,
worktrees, and live sessions do not transfer. The mode name does not change
that behavior. The record is a retrieval index, not an artifact transport.

## Retrieval manifest

Before inspecting predecessor content, retain this identity tuple from the
successor's `handoff_context` and group status:

```json
{
  "predecessor_work_group_id": "exact predecessor group reference",
  "successor_work_group_id": "exact successor group reference",
  "mode": "reports_and_changes",
  "artifacts_transferred": false,
  "changes_transferred": false,
  "reports_truncated": false,
  "reports": [
    {
      "fleet_worker_ref": "exact stable worker reference",
      "machine_id": "predecessor machine identity",
      "edge_generation": "predecessor generation",
      "edge_worker_id": "Edge-local worker ID",
      "name": "worker name",
      "turn_state": "observed state",
      "integration_state": "observed disposition",
      "report_summary": "bounded summary only"
    }
  ]
}
```

This is an exact retrieval manifest for IDs, not a content integrity manifest:
it does not contain a blob hash, immutable candidate ID, complete report,
artifact list, or portable export handle. If `reports_truncated` is true, the
metadata list is incomplete and must not be treated as the worker inventory.

## Current explicit retrieval procedure

1. Keep the successor group pinned to its own machine. Do not imply that
   reassigning the group moved predecessor files or changed the predecessor's
   state.
2. Route read-only calls directly to the exact predecessor group ID supplied in
   each action. Confirm the returned machine and Edge generation match the
   manifest; do not try to make the superseded predecessor current again.
3. Use `patchbay_worker_inspect` for the exact predecessor worker reference.
   Request `view="report"` for the report; request `view="changes"` or
   `view="diff"` for changes. Follow returned pagination/cap fields, preserve
   every cursor and revision, and record whether any result is truncated. Do
   not infer complete coverage from a short response.
4. Read any selected `report_artifacts`/`worker_report_files` and every needed
   changed text file using `view="file"`, starting at line 1 and following
   `next_start_line` until the service reports `truncated=false`. Keep the
   returned file SHA-256 and total line count; reread the first page after the
   final page and require the digest to remain stable. This file view is
   text-only. If a required artifact is binary or inaccessible, mark retrieval
   incomplete instead of treating the report as a substitute.
5. Record the exact worker reference, projection content revision/hash,
   predecessor generation, view, page/cursor, returned byte count and
   truncation status. File reads include a content SHA-256; report and changes
   summaries are live reads tied to the worker projection revision and are not
   an immutable artifact bundle.
6. Inspect and review the retrieved content in the controller. Pass selected
   report and file content into successor work through the ordinary controller
   context, preserving the exact source identity and SHA-256. Where the client
   supports a native file attachment, attach the exact retrieved file and
   verify that the successor receives the same content. A report summary alone
   is not a replacement for a required change set.
7. Start or continue successor work only after the controller has decided what
   content is authoritative and provided the required exact context. Inspect
   the successor's resulting diff and run its own required checks. Retrieval
   never authorizes applying or integrating a change.

If the predecessor Edge is offline, the route is unavailable, or a result is
truncated without a supported continuation, report the exact blocker and stop
cross-Edge application. Do not guess a path, fetch from a different worker, or
claim that a metadata record is a recoverable copy.

## Boundary with automatic transfer

The supported path is explicit controller retrieval followed by deliberate
attachment into successor context; it is not automatic Edge-to-Edge transfer.
The returned retrieval actions make the predecessor route and content-read
sequence available to the successor controller. An automatic content-addressed
transfer protocol would require separate Edge export, Hub storage, destination
import, authorization, receipts and retention. It is outside this retrieval
contract unless explicitly required. For each actual handoff, retain observed
identities, hashes, pagination and successor context before claiming the
selected content was successfully retrieved and attached.
