# PatchBay Quick Start

PatchBay has two complementary workflows. Do not mix their implementation authority inside one task.

- **Manager/worker mode:** ChatGPT Pro coordinates; local Codex workers implement and execute.
- **Author Mode:** ChatGPT Pro writes source; PatchBay deterministically transports, validates, materializes, checks, and returns evidence. It never initializes a Codex worker.

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

## Verify the source and deployment

Run the exact matrix in [TESTING.md](TESTING.md). Mac checks verify portable mechanics; Linux/WSL2 execution and the deployed ordinary-Chat route require their own qualification. No PR is created by the Author workflow. See [Author operations](docs/authoring/operations.md) for explicit inbox watch, recovery, pause, revocation and policy changes.
