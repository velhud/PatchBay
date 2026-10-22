# Using a high-capability ChatGPT model with PatchBay

Choose the primary model in ChatGPT, then select the relevant PatchBay plugin
in the conversation. PatchBay does not select or switch the conversation model.
GPT-5.6 Pro or GPT-6 Pro also needs to expose the connected plugin's tools in
that conversation; appearing in the model picker alone does not prove this.
The recorded live trials used ordinary GPT-6 Extra High, so Pro compatibility
still needs its own client check. Ask the model to write the code itself with
Author Mode, or to manage local Codex workers with Manager mode. Those are
different execution paths.

Use one mode and its relevant plugin per conversation. This keeps unrelated
tool catalogs and conflicting author/manager instructions out of that task.
For a fresh conversation, recover the existing task or named workers rather
than pasting the whole old transcript or starting replacement work.

## Set up the scope once

Before a writable Author task, ask Codex to prepare it through the existing
`patchbay-author` operator workflow. Codex should initialize the separate
Author home, register the exact repository, pin the base revision, and create a
least-privilege task policy: explicit writable paths, a pinned Docker image,
the required checks, and bounded CPU, memory, disk, process and time limits.
Keep publication disabled unless it is separately needed and authorized.
Have Codex create the task and bind a task-scoped Author principal to the
qualified writable MCP route. It should report the task ID, base revision,
allowed paths and check names without printing credentials. Do not hand-edit
the Author database, lease files or tokens, or shuttle source ZIPs between
apps. Give Codex the repository and task goal. Codex should infer the relevant
base, writable paths, checks and runtime profile from the repository and your
existing authorization, perform registration through the supported operator
commands, and report the resulting scope. Ask for a new decision only if a
material scope or authority question remains unresolved.

For Manager mode, Codex instead starts the existing manager server with only
the intended repository roots, checks the complete installed Codex CLI and
Luna availability, and verifies the worker sandbox and authenticated route.
Use Linux execution for unattended worker follow-ups until Mac cleanup is
qualified. The existing disposable trial connections remain scoped to their
fixtures; they cannot silently open a different repository. A production
connection also needs a persistent service and authenticated stable ingress;
a temporary qualification tunnel is not that deployment.

The ChatGPT browser is the conversation surface. Author check execution runs
on the qualified Linux/WSL2 host, in the task's approved Docker profile; the
MCP connection does not grant general access to the Mac or its filesystem.
If the connection is read-only or the task is not qualified for writes, stop
and have Codex complete the operator setup. Do not route around that boundary.

## Option 1: ChatGPT authors the implementation

Use Author Mode when the primary ChatGPT model should write the actual source.
The writable Author tools let ChatGPT read pinned files, make versioned draft
edits, seal a candidate and request policy-approved checks. No local ZIP or
manual source copying is needed. The authoring model does not delegate
implementation or failure recovery to Codex.

Copy and adapt this task prompt:

> Use PatchBay Author Mode for task `TASK_ID` on the pre-registered repository. First call `author_recover_task` and inspect the exact base revision, writable paths, required checks and pending operations. Claim the task lease. Export one pinned context with `author_export_context`, limited to the files needed for this change; read bounded windows from that context. Implement the requested behavior and meaningful tests through versioned Author draft edits, seal the draft, run the required checks, and inspect canonical feedback and bounded logs. If a check fails, correct the draft, seal a new candidate revision, and run its approved checks. Use a new idempotency key for a corrected request; after an uncertain response, inspect recent operations before retrying. Do not edit the operator's source checkout, widen policy, delegate, commit, publish, create a PR or deploy. Return the candidate revision and exact check evidence, including anything still unverified.

An edit is a draft, not an accepted candidate. Sealing validates and accepts a
candidate; checks then qualify that exact candidate. A pass does not approve
publication. Approval and publication are separate operator actions, and
Author does not create a PR, merge or deploy.

## Option 2: ChatGPT manages local Codex workers

Use Manager/worker mode when local Codex colleagues should investigate, write
or review the repository. The primary ChatGPT model remains the manager. The
PatchBay server enforces `gpt-6-luna` for every delegated worker, with `high`
reasoning by default (`xhigh` or `max` are supported); if Luna is unavailable,
the worker cannot be substituted with another model.

Copy and adapt this task prompt:

> Use PatchBay Manager/worker mode for the registered repository and this exact task: `TASK`. Start with `codex_self_test`, open the workspace, and check `codex_worker_options`. Assign one named writing worker an isolated write worktree to implement the change and run the relevant tests. After its report is ready, ask a second named read-only worker to review the actual candidate and test evidence. Keep both worker selections at the server-required `gpt-6-luna` / `high` policy. Continue the same workers for corrections, wait at the returned cadence, and inspect reports plus the focused integration preview. You may accept and apply a clean reviewed preview within this task's authorized scope, then run the affected checks on the integrated source. Do not commit, push, publish, create a PR or deploy. Report exact tests run, remaining concerns and the resulting integration state.

Workers use isolated worktrees by default. A clean `codex_worker_integrate`
preview is the gate before explicit integration; applying it updates the base
checkout but does not commit or publish. Review the accepted changes before
any separate Git or release action.

## Pin source and join dependent work

Before parallel implementation, declare the input generation, independent and dependent lanes, interface owner, join condition and required combined checks. Keep these as a short controller brief; Hub groups can persist the same declaration in `source_contract`. Start only independent lanes concurrently. For a dependent lane, retain the upstream preview target and pass `source_candidate` with its worker and `candidate_id`; default starts use committed base source and do not inherit dirty no-commit integration.

Use `context_detail=review` and the retained candidate ID to review actual source. Pass `candidate_id` to inspect changes/file/diff; a later worker edit cannot replace that target. Keep read-only review distinct from executable verification in a separate authorized isolated writable check lane. Supply a candidate-bound `review_decision` on integration when required. After joining the accepted changes, run the declared checks against the actual combined source. At complete group close, `combined_verification` retains the matching tree/check evidence as controller attestation. Independent passes alone do not establish a combined pass. Do not commit or publish to manufacture a source generation. See the [full source/delivery contract](../repairs/2026-10-22-s06-candidate-delivery.md).

## Keep long tasks and context manageable

PatchBay stores task, draft, operation and worker state, but it does not wake
ChatGPT when work finishes. A completed check or worker wait is not a new
model response. Keep the returned task, operation and worker names; ask the
same conversation to continue, or start a new one and recover the exact
durable state first. For Author, use `author_recover_task`,
`author_operation_status`, `author_recent_operations` and
`author_get_feedback`. For Manager mode, use the named worker status, wait and
inspect tools. Never blind-rerun an operation whose outcome is uncertain.

To reduce context overhead, give one precise goal, base revision, path scope
and acceptance criteria. In Author Mode, read only relevant pinned paths and
use bounded file windows and feedback pages; export context or feedback only
when needed for a handoff. `author_recover_task` defaults to 20 recent
operations (`author_get_task` still embeds the domain's 50-row default).
Active and uncertain operations have a separate prioritized projection,
bounded to 200 with total counts and a partial flag; classification includes
the entire active set even when the projection is partial. Follow
`author_recent_operations` cursor pages when more history is required. Keep
the full compatibility output profile unless the client has separately proved
that it can read complete source, logs and errors from `structuredContent`;
the concise profile is a separate qualified choice.

In Manager mode, send workers a concise brief and
let them read the repository; use their reports for routine updates instead of
repeating whole files or raw logs in ChatGPT. Continue existing tasks and
workers rather than recreating them after a lost final response. Prefer status
for monitoring; request history and detailed inspection when they answer a
specific question. Do not paste the complete setup guide or server instructions
into each follow-up. Server instructions are supplied during initialization,
not appended by PatchBay to every tool result. A reconnect may initialize again;
that alone does not prove the client repeatedly injects them into model context.

## What the recorded trials establish

The 21 October 2026 report records a Mac complete suite of 1,220 passed and 39
platform skips, a Linux complete suite of 1,253 passed and 7 skips, and
Author-specific suites of 172 passed/35 skipped on Mac and 202 passed/5
skipped on Linux. A disposable SQLite queue task passed 25 tests and
compilation. A disposable ledger task passed 21 unit tests and compilation
after repair; separate trials exercised cancellation before command start and
while a command was running. The test and cancellation counts describe those
fixtures, not a general success rate.

The ordinary-Chat coding trials used GPT-6 Extra High without Pro. They do not
verify GPT-5.6 Pro, GPT-6 Pro, other accounts, arbitrary repositories or all
future deployments. GitHub Actions was disabled, so no CI pass is claimed. The
52-case research evaluator had no per-case receipts and reported `NOT_RUN`;
it is not evidence of 52 passing or failing trials. Mac
unattended worker cleanup remains unqualified, and ordinary ChatGPT
occasionally failed to render a final response. Durable task state supports
recovery, but it does not solve that client behavior or automatically wake the
model. The trial Docker service and routes are qualification infrastructure,
not production-ingress, reboot or power-loss qualification.

For startup details, see [QUICKSTART.md](../../QUICKSTART.md),
[ChatGPT client instructions](chatgpt-instructions.md), and
[workflow modes](../reference/workflow-modes.md).
OpenAI's [connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt)
describes adding a custom MCP server, selecting its plugin in a new
conversation and refreshing metadata after server changes. A refreshed entry
still needs the mode's runtime/client qualification.
