# MCP interface and recovery contract

This describes repository behavior. A generated server catalog, a connector's
approved metadata snapshot, and the tools currently visible to a model are
separate observations. None alone qualifies an ordinary ChatGPT route.

## Catalog and role observations

The complete canonical Hub V2 export has 31 tools. Compare that count and its
contract identity against a **complete authenticated capture**. A named scoped
trial, permission-filtered view or incrementally loaded host subset is not such
a capture. Diagnose a missing required action using the selected plugin/profile,
permissions and supported contract readback. Repair an incomplete canonical
export; do not certify a scoped trial as canonical. The model need not be able
to read host-only `_meta` fields for the operator to capture them.

Author exposes 11 reader tools, 20 for a qualified author, or 22 for a separately
qualified operator. Reads do not create exports. A reader recovers existing
tasks, source, feedback, logs and operations, and uses existing artifacts where
available. A new context/feedback archive requires an authorized writer or
operator. Role and qualification checks also apply to direct calls of hidden
tools. Neither catalog visibility nor annotations grant authority.

## Source selectors and repairable arguments

`author_read_file` accepts exactly one source:

| Source | Required non-null fields | Fields absent or null |
| --- | --- | --- |
| Immutable context | `context_id` | `draft_id`, `draft_version` |
| Exact draft snapshot | `draft_id`, positive strict integer `draft_version` | `context_id` |

Use `author_recover_task(task_id=...)` to obtain existing contexts and
`drafts[].id` / `drafts[].version`. A task revision is not a draft version.
Schema acceptance does not establish that an ID/version exists or is current.
Unknown fields, booleans used as integers and unsafe paths remain rejected.
JSON Schema's [mathematical integer type](https://json-schema.org/understanding-json-schema/reference/numeric#integer) cannot distinguish the JSON numeric
representations `1` and `1.0`; strict runtime integer validation still applies.

Input-model errors retain `INVALID_REQUEST` and identify
`arguments_rejected_before_effect`. At most eight safe field errors name
declared locations and constraints, with an omitted count. Unknown property
names, submitted values, Pydantic input/context and arbitrary validator messages
are not echoed. Selector errors name the three fields and the recovery read.
Existing domain errors retain their own codes and effect/retry semantics.

## Version negotiation and HTTP

The existing implemented JSON tools/resource subset accepts `2025-03-26`,
`2025-06-18` and `2025-11-25`. A supported initialization request gets the same
version. An unsupported non-empty string gets the Manager preference
`2025-11-25`; Author retains its compatibility preference `2025-03-26`.
Malformed types and empty/blank strings get a parameter error. Selection does
not add optional tasks, sampling, subscriptions or other unadvertised features.

Subsequent HTTP POST requests with `MCP-Protocol-Version` must name a supported
version. Stateful local/Hub requests must match their session's negotiated
version. Missing headers retain compatibility using the stored negotiation,
or March 2025 on a stateless/uninitialized route. Author HTTP is stateless: it
validates the explicit supported header without inventing a session binding.
Deployed proxy behavior, all HTTP verbs, old-client lifecycles and metadata
refresh remain separate route-qualification checks.

The selection follows the MCP [version negotiation specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#version-negotiation).
HTTP header handling follows the [transport contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#protocol-version-header).

## Compact Manager results

Compact text independently preserves bounded outcome flags, exact short
worker/task/operation references, candidate/preview/hash references, source
coordinates, partial/truncation/continuation fields, cleanup/confirmation state,
and monitoring freshness/cadence when returned. Hub also retains its outer
status, operation state and a complete first next-action object when that object
fits the summary budget. These fields distinguish refusal from admission,
application from stale preview, and partial evidence from a complete read even
when normal prose is identical.

Oversized identity/cursor strings are explicitly listed in
`summary_omitted_fields`; a shortened cursor is never presented as usable.
Bulk source, logs and all pages remain in unchanged structured results and
existing paging/artifact routes. A summary alone cannot qualify text-only
source/log inspection. Author's `compat-full-v1` remains the default and
`structured-summary-v2` still requires its own observed qualification.

A stop-confirmation response says that stopping has not occurred. It requires
the authorized stop decision or continued observation; it grants no new force
or discard authority. Cancellation acknowledgement and cleanup proof remain
different facts.

## Result faults after dispatch

Output validation never turns a malformed result into success. Author tool
errors keep `INVALID_OUTPUT` (or `RESOURCE_LIMIT` for an oversized envelope).
Hub V2 returns JSON-RPC `-32603` with safe `error.data.code=INVALID_OUTPUT`.
Both include `success_asserted=false` and a boundary:

| Boundary | Meaning |
| --- | --- |
| `arguments_rejected_before_effect` | Author input model rejected the request before domain dispatch |
| `effect_may_exist_result_invalid` | A mutating handler returned, but its output failed validation |
| `effect_result_too_large` | A mutating Author handler returned, but the envelope exceeded 8 MiB |
| `read_result_invalid` / `read_result_too_large` | A read returned an invalid/oversized result; no mutation was attempted |

Author retains the compatibility fields `effect_returned` (a mutating handler
returned, not success) and `result_identity`, restricted to trusted readback.

Recovery uses already-validated request identities and existing authorized
durable readback. Author ignores all identities in the bad result. Hub accepts
an output operation identity only after matching its durable principal, tool,
original key and any requested group association. Unknown identities are not
fabricated: use the returned task/group discovery read. Errors include an
original caller key where available and complete read-only `recovery_actions`.
Read the original admission before another mutation. An identical retry keeps
its original key and payload; a corrected terminal rejection is a different
request under the existing domain rules. No second journal or new success
state is introduced.

## Author effects and unchanged annotation hints

The table classifies possible effects, not just the usual happy path. Existing
hints remain conservative; no native-client confirmation benefit has been
measured. All tools retain `idempotentHint=true` under their keyed/readonly
contracts. `readOnlyHint=false` and `destructiveHint=true` remain on every write.
`openWorldHint=true` remains only on native package retrieval and policy check
execution; the other tools operate within configured state/repository/remotes.

| Tools | Possible effects | Role |
| --- | --- | --- |
| `author_capabilities`, `author_list_tasks`, `author_get_task` | Read capability/task/candidate/qualification state | Reader |
| `author_read_file`, `author_search` | Read pinned context or exact draft source; no export creation | Reader |
| `author_get_feedback`, `author_read_log` | Read retained evidence/log pages | Reader |
| `author_operation_status`, `author_recover_task`, `author_recent_operations` | Read existing receipts, uncertainty, ownership and history; no retry/reconcile/launch | Reader |
| `author_publication_preview` | Read publication prerequisites; no approval or push | Reader |
| `author_create_task` | Add durable task/operation, pin source and Git refs | Qualified author/operator |
| `author_claim_task` | Claim/renew lease; explicit takeover fences older writers | Qualified author/operator |
| `author_export_context`, `author_export_feedback` | Add durable archives/blobs, refs and replay receipts | Qualified author/operator |
| `author_submit_package` | Fetch external native file, validate/materialize isolated candidate; policy may queue checks | Qualified author/operator |
| `author_apply_edits`, `author_seal_draft` | Replace/delete draft content or accept a candidate; invalidate stale evidence | Qualified author/operator |
| `author_run_checks` | Queue approved subprocess/container checks, logs and receipts | Qualified author/operator |
| `author_cancel_operation` | Request owned cancellation; cleanup remains separately proven | Qualified author/operator |
| `author_approve_publication` | Record exact candidate approval under actor/lease authority | Qualified operator |
| `author_publish` | Apply approved publication to registered remote under policy | Qualified operator |

Permission/annotation tests enumerate every catalog entry and deny every writer
tool through a reader role before an effect. A future annotation experiment must
keep these outcomes unchanged, vary one family in an identified test profile,
and retain the original hints unless actual client evidence demonstrates benefit.

## Qualification still required

Before enabling an upgraded route, capture its build, full authenticated catalog,
approved snapshot/profile, role, instructions, negotiated version and output
profile. Separately capture the model/client label and per-message tool visibility.
Use a disposable task with explicit authority and the existing qualification
mechanism. Check discovery, one permitted action, one denied action, exact source
and log pages (including Unicode/long lines and a failure beyond the summary),
same-chat follow-up with explicit app selection, lost-response readback and
fresh-chat recovery of the same task. Keep full canonical and scoped trial
results separate. Retain every failed/inconclusive attempt and raw safe receipts.

Only after an observed ordinary-Chat write should an operator record Author
qualification through the supported command. Do not copy an old receipt or
hand-edit a pass:

```bash
python scripts/author_interface_eval.py --json > "$EVIDENCE_DIR/interface-source.json"
patchbay-author --home "$PATCHBAY_AUTHOR_HOME" qualify-client \
  "$EVIDENCE_DIR/observed-client-evidence.json" --actor OPERATOR_ID --key QUALIFICATION_KEY
```

The evidence JSON must describe the actual observed model/client/account, role,
ordinary-Chat mode, protocol, ingress, plugin version, observation time and write
trial. Run the qualification command only after that trial under its authorized
operator scope; the source evaluator does not supply those observations.
See [qualification](../authoring/qualification.md),
[ordinary Chat preparation](../authoring/ordinary-chat-trial.md), and
[TESTING.md](../../TESTING.md). Local tests, generated catalogs and source
publication do not establish native Pro, Linux execution, deployment, reboot,
power-loss or macOS worker-cleanup qualification.
