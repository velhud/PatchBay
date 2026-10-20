# PatchBay Quick Start

PatchBay has two complementary workflows. Do not mix their implementation authority inside one task.

- **Manager/worker mode:** ChatGPT Pro coordinates; local Codex workers implement and execute.
- **Author Mode:** ChatGPT Pro writes source; PatchBay deterministically transports, validates, materializes, checks, and returns evidence. It never initializes a Codex worker.

## Install the candidate

```bash
git fetch origin
git checkout chatgpt/patchbay-unified-reconciliation-20261020
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

Hub/Edge deployments use the corresponding `patchbay_*` group, worker, workspace, Pro Request, and operation tools. One user task is one durable group; its workers remain pinned to one machine unless explicitly reassigned.

## B. Author Mode

Run Author Mode qualification and check execution on Linux or WSL2. The platform guard is intentional.

```bash
AUTHOR_HOME="$PWD/.patchbay-author"
rm -rf "$AUTHOR_HOME"

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

## Verify before merge

Run the exact matrix in [TESTING.md](TESTING.md). Do not merge the consolidation PR until the combined commit has passed the required local Linux/WSL2 and repository-specific checks.
