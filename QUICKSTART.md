# PatchBay Quick Start

PatchBay has two complementary workflows. Do not mix their implementation authority inside one task.

- **Manager/worker mode:** ChatGPT Pro coordinates; local Codex workers implement and execute.
- **Author Mode:** ChatGPT Pro writes source; PatchBay deterministically transports, validates, materializes, checks, and returns evidence. It never initializes a Codex worker.

For a task brief you can use directly in ChatGPT, see the
[heavy-task guide](docs/user/chatgpt-heavy-tasks.md). The operator prepares the
repository scope once; the guide distinguishes direct coding from delegation
and records what still needs client or deployment qualification.

## Install the candidate

```bash
git fetch origin
git checkout main
git pull --ff-only

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e '.[test]'
```

Confirm installed entrypoints:

```bash
patchbay --help
patchbay-author --help
patchbay author --help
python -m patchbay.authoring.interfaces.cli --help
```

## A. Manager/worker mode

Use a disposable repository for the first run.

```bash
patchbay doctor
patchbay doctor --json
patchbay start \
  --root /absolute/path/to/disposable/repo \
  --tool-mode worker
```

For a local MCP host, stdio is also available:

```bash
patchbay stdio --config config.yaml
```

For ChatGPT web, start an authenticated HTTPS tunnel and use the private Server URL printed by PatchBay. One copied Server URL represents one shared local state surface across connected clients. Reads may be shared; cross-owner mutation requires explicit takeover, and repository mutation contention must remain visible as `repo_busy` rather than being bypassed.

### Manager startup sequence

1. Call `codex_self_test`.
2. Open or discover the repository with `codex_open_workspace` or `codex_list_workspaces`.
3. Call `codex_worker_options` once for the relevant installed catalog state.

Check `worker_model_available` before dispatch. An installed CLI and a valid
login alone do not establish support for the selected model. Codex CLI 0.163.0
was verified with the exact `gpt-6-luna` selector; an older CLI can reject it
even with the same signed-in account. Use the installed account-specific catalog
and a disposable real-worker trial to verify the deployed runtime. Correct an
unsupported CLI/account configuration before retrying the same named worker;
do not substitute a different model or treat a missing final report as success.
Install the complete official CLI package, including its companion execution
hosts and resources. Copying only the standalone executable can pass version
and model checks while leaving shell execution unavailable. Verify an actual
read-only command in the disposable worker trial before starting coding work.

4. For an ordinary worker, explicitly pass:

```json
{
  "model": "gpt-6-luna",
  "reasoning_effort": "high"
}
```

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

5. Start durable named workers with colleague-quality briefs: purpose, current context, outcome, scope, constraints, non-goals, lane relationships, deliverables, and verification evidence.
6. Continue the same worker with `codex_worker_message` when evidence is thin or correction is required.
7. Wait at the returned cadence. Quiet work and a wait timeout are not proof of failure.
8. Inspect reports first. Use direct file/diff tools for orientation, focused verification, contradictions, risky changes, or explicit user requests—not as the default broad implementation loop.
9. Preview isolated integration before applying it. Integration does not commit and does not delete the worker worktree.

Retain the preview’s `review_target.candidate_id` for pinned file/diff inspection. Record an explicit `review_decision` when source review is required. Default isolated starts use committed source; dependent lanes use `source_candidate`. Safe cleanup leaves `discard_unintegrated_changes` false. See the [candidate and recovery contract](docs/repairs/2026-10-22-s06-candidate-delivery.md).

For local `codex_worker_start` and `codex_worker_message`, supply a stable
`idempotency_key` for each deliberate request (1–200 UTF-8 characters). After a
lost response, recover existing work and retry with the exact key and arguments.
Omitting the key preserves the legacy behavior; `auto_suffix` or a new key
requests independent work. A message to an active turn is refused, not queued.
Keyed integration also requires the signed `preview_token` from preview.
See the [S02 recovery contract](docs/repairs/2026-10-22-s02-controller-recovery.md)
for ownership scope and retention limits.

Local `codex_worker_wait` reads first and returns existing terminal/actionable
work immediately, or returns after a meaningful status change. Its default idle
budget cap is 30 seconds (`workers.status_max_wait_seconds`, bounded to 1–120).
An operator changing this value must reserve time for admission, final readback,
serialization and delivery within the measured route deadline. The idle cap is
not a total HTTP deadline. Cancelling a wait never stops execution. A Hub
`resync_required` response contains a full snapshot: use it as the new baseline.
See the [S03 observation contract](docs/repairs/2026-10-22-s03-observation.md).

If a terminal job reports `cleanup_blocked_untrusted_process_identity`, do not
blindly retry or remove the durable fence. Stop the local PatchBay server first,
then inspect one exact job with the offline operator command. It defaults to an
assessment that does not change job state or receipts; applying requires an explicit quiescence attestation
under the named operator identity and never signals processes:

```bash
patchbay cleanup-recover --config /path/to/config.yaml --job-id <exact-job-id> --operator <name>
patchbay cleanup-recover --config /path/to/config.yaml --job-id <exact-job-id> --operator <name> \
  --apply --confirm-quiescence 'I independently verified that every tracked process has exited and no detached same-job process remains active.'
```

Recovery refuses live or inconclusive PID, process-group, marker, descendant,
server, or repository-lock evidence. On Darwin the OS cannot retrospectively
identify a fully detached process that removed PatchBay's job marker; the
private receipt records this limit and the operator attestation separately from
the supervisor's original unproven receipt.

An unproven-cleanup sentinel intentionally remains alive and holds the
repository lock. It ignores TERM and INT, and `cleanup-recover` never signals
processes, so an active sentinel must be dispositioned by the operator before
the command can pass its sentinel and lock checks. Do this only after the
server is stopped and independent evidence establishes that no tracked or
detached same-job process remains. A persisted PID alone is not identity: when
the record has no process-birth identity, independently match the live process
to this job's sentinel using current OS process evidence, the exact job marker,
and its matching supervisor proof. Never signal a stale or recycled PID; if
that exact match cannot be established, leave the cleanup blocker in place.
After the verified sentinel exits and releases the lock, rerun the read-only
assessment before applying the operator attestation.

Hub/Edge deployments use the corresponding `patchbay_*` group, worker, workspace, Pro Request, and operation tools. One user task is one durable group; its workers remain pinned to one machine unless explicitly reassigned.

The complete canonical Hub export has 31 tools. A scoped trial or tools loaded
for one message is a different observation; diagnose missing actions using the
selected route, permissions and a complete contract capture. See the
[MCP interface contract](docs/reference/mcp-interface-contract.md) for version
negotiation and result-fault recovery. Compact text preserves decisions;
consume the exact structured source/log pages before claiming complete review.

## B. Author Mode

Run Author Mode qualification and check execution on Linux or WSL2. The platform guard is intentional. For the first disposable ChatGPT trial, use the [fixture preparation guide](docs/authoring/ordinary-chat-trial.md); it prepares source, state and evidence templates without calling ChatGPT or qualifying a live connection.

The default `.patchbay-author/` state directory and `results/` trial output are ignored by Git because they contain credentials and private evidence. Keep them outside public source exports and never force-add them.

```bash
AUTHOR_HOME="$PWD/.patchbay-author"
patchbay author --home "$AUTHOR_HOME" init
patchbay-author --home "$AUTHOR_HOME" doctor
```

Register a source repository with an explicit policy:

```bash
patchbay-author --home "$AUTHOR_HOME" register \
  demo \
  /absolute/path/to/source/repository \
  --policy /absolute/path/to/task-policy.json
```

Create and claim a task:

```bash
patchbay-author --home "$AUTHOR_HOME" task \
  demo \
  --goal "Implement the requested change and return qualification evidence" \
  --base HEAD \
  --key create-demo-task

patchbay-author --home "$AUTHOR_HOME" tasks
patchbay-author --home "$AUTHOR_HOME" claim TASK_ID --label operator
```

Export deterministic context outside the private Author home:

```bash
patchbay-author --home "$AUTHOR_HOME" context \
  TASK_ID \
  --output "$PWD/task-context.zip"
```

For a small context add `--path src/file.py`; unrelated payloads are omitted
while complete path presence remains recorded. Use `--identity-only` for a
new-file-only context with no baseline payloads; an empty path list still means
full export. Extract the bundle, use its included helper to prepare `edited/`,
and keep the completion marker beside that directory. Build outside `edited/`.
A directory containing only changed files is not a prepared complete tree.

ChatGPT Pro writes a package matching the Author package schema. Apply it with an idempotency key:

```bash
patchbay-author --home "$AUTHOR_HOME" apply \
  TASK_ID \
  /absolute/path/to/package.zip \
  --key apply-revision-1
```

Run policy-authorized checks. `--wait` without a value waits up to 3,600 seconds; an explicit value is bounded to 1–86,400 seconds.

```bash
patchbay-author --home "$AUTHOR_HOME" check \
  TASK_ID \
  --key check-revision-1 \
  --wait

patchbay-author --home "$AUTHOR_HOME" status OPERATION_ID --wait 600
```

Read bounded correction evidence:

```bash
patchbay-author --home "$AUTHOR_HOME" feedback TASK_ID --markdown
patchbay-author --home "$AUTHOR_HOME" diff TASK_ID
patchbay-author --home "$AUTHOR_HOME" logs OPERATION_ID stdout.log --offset 0 --limit 65536
```

Approval and publication are explicit and role-bearing:

```bash
patchbay-author --home "$AUTHOR_HOME" approve \
  TASK_ID \
  --actor HUMAN_OR_AUTOMATION_ID \
  --key approve-revision-1

patchbay-author --home "$AUTHOR_HOME" publish \
  APPROVAL_ID \
  --key publish-revision-1

patchbay-author --home "$AUTHOR_HOME" publications TASK_ID
```

Author Mode does not call manager/worker tools, import the worker runtime, or ask another model to repair failures. A failed check returns evidence to the same authoring loop.

A reader can recover tasks and read existing context, draft, feedback and logs.
Creating a new export requires a qualified writer or operator. For MCP source
reads, select `context_id` alone, or `draft_id` with its exact positive
`draft_version`. Recovery exposes existing IDs and draft versions. After
`INVALID_OUTPUT` or a response-size error, follow the returned read-only recovery
actions and original retry key before attempting another mutation.

For a huge line or binary source use `author_read_file` with the same selector,
`byte_offset=0` and `max_bytes=65536`. Concatenate decoded `data_base64` bytes
while following `next_byte_offset`; verify the full source hash and never mix
draft versions. This read-only route also works before sealing. See the
[S05 custody contract](docs/repairs/2026-10-22-s05-author-custody.md) for
inventory/helper compatibility, verified exports and publication recovery.

For history during ongoing activity, call `author_recent_operations` with
`traversal="snapshot"`, then carry its unchanged cursor and `next_offset` until
`partial=false`. This pins the initial operation identities; each page reads
their current states. Recover the task again before any successor mutation.

Select `candidate.candidate_context_id` for current candidate source; compare
its `candidate_context` revision/commit with the candidate. If null, an authorized
writer must export that revision first. `source_context_id` and the deprecated
`candidate.context_id` retain authoring provenance and may identify older source.
Passing candidates with publication disabled return `awaiting_review`; review
is recorded externally and does not authorize publication or persist acceptance.

## Verify the source and deployment

Run the exact matrix in [TESTING.md](TESTING.md). Mac checks verify portable mechanics; Linux/WSL2 execution and the deployed ordinary-Chat route require their own qualification. No PR is created by the Author workflow. See [Author operations](docs/authoring/operations.md) for explicit inbox watch, recovery, pause, revocation and policy changes.

### Native execution evidence

The supported native adapter is headless `codex exec` / exact-session resume.
Its finite pre-target supervisor handshake defaults to 10 seconds; it is separate
from the model turn budget. Typed native failures and identity conflicts cannot
be erased by generic completion. Preserve cleanup fences and resolve identity
conflicts before continuation or acceptance.

When a report returns `report_truncated=true`, use the same worker inspect tool
with `view="report_source"`, its `report_source.sha256`, `report_offset=0` and
`max_bytes`; follow `next_offset` until `has_more=false`. The returned UTF-8
representation can be privacy-filtered. A missing complete source stays explicit.
See the [S04 contract](docs/repairs/2026-10-22-s04-native-execution.md) for the
offline installation-manifest helper, source bounds and separate native/client
qualification recipe. No canary runs automatically.

Before enabling publication, explicitly select downstream-effect consent or an operator-controlled no-automation destination policy. Checks provide evidence for review; exit success and candidate-authored reports are not independent acceptance. See [S07 evidence and migration](docs/repairs/2026-10-22-s07-evidence-authority.md).
