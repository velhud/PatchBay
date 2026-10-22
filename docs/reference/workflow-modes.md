# Workflow modes and authority boundaries

PatchBay exposes two complementary workflows. They may coexist in one installation, but a task must not cross their implementation-authority boundary implicitly.

## Manager/worker mode

### Authority

ChatGPT Pro acts as manager, architect, reviewer, and coordinator. Local Codex workers perform delegated repository investigation, implementation, execution, testing, and detailed review.

PatchBay owns transport, durable worker identity, worktree isolation, status, evidence, integration preview/apply, process lifecycle, and optional Hub/Edge coordination. The manager remains responsible for worker selection, briefs, follow-up, acceptance, and user-facing synthesis.

Retained candidate targets bind review to exact source, separately from delivery. Explicit dependent source and combined-check handoffs use existing worker/group owners. Group check attestations are controller evidence, not canonical execution receipts. [S06](../repairs/2026-10-22-s06-candidate-delivery.md) defines source and recovery boundaries.

### Entry points and state

- `patchbay start`
- `patchbay stdio` / `patchbay-stdio`
- `patchbay hub ...`
- `patchbay edge ...`

State includes jobs, named workers, sessions, worktrees, artifacts, Pro Requests, locks, and optional Hub/Edge projections and operations.

### Model policy

The installed Codex catalog is authoritative. Ordinary worker starts explicitly select Luna/High when supported:

```text
model = gpt-6-luna
reasoning_effort = high
```

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

### Recovery

PatchBay may recover transport, process, attempt, projection, or receipt state deterministically. It may continue or reconcile the same worker execution according to durable evidence. It must not invent a model result or silently ask another model to repair a failed worker.

## Author Mode

### Authority

ChatGPT Pro writes the implementation package. PatchBay is not the implementer. It validates and transports content, constructs an exact isolated candidate, executes declared checks, and returns bounded evidence to the same authoring loop.

Author Mode must not initialize a Codex worker, import the worker runtime, dispatch implementation to another model, or use model output as hidden recovery.

### Entry points and state

- `patchbay author ...`
- `patchbay-author ...`
- `python -m patchbay.authoring.interfaces.cli ...`

Author state is independent: repositories, policies, tasks, leases, contexts, revisions, operations, check logs, approvals, publications, blobs, tokens, qualification records, and maintenance metadata.

### Package and candidate boundary

An Author package is data, not authority to execute arbitrary recovery logic. It must be manifest-bound and path-safe. PatchBay verifies declared files, hashes, object formats, base revision, policy, and package structure before constructing a candidate.

Candidates are isolated from the source checkout. Checks execute only when authorized by task policy and platform qualification. Results are classified and returned as evidence. Failure returns to ChatGPT Pro for a corrected package.

### Platform boundary

Execution qualification is intentionally Linux/WSL2-only. A macOS result stopped by that guard is not proof of multiple independent implementation failures. The guard must remain until the execution containment contract is deliberately extended and qualified.

## Shared infrastructure, separate authority

The modes may share low-level Git helpers, storage formats, process supervision patterns, hashing, export safety, and bounded evidence conventions. Shared implementation must not create these forbidden crossings:

- Author startup importing or initializing worker runtime state;
- manager mode accepting an Author package as implicit worker instructions;
- Author failure silently spawning a worker or another model;
- worker recovery mutating Author approvals/publications;
- shared CLI dispatch obscuring which state home and authority model is active.

`patchbay author` is dispatched before the legacy manager CLI and reaches the same implementation as `patchbay-author`, but that packaging convenience does not merge the workflows.

## Choosing a mode

Choose manager/worker mode when the desired system is a managed local engineering team and Codex workers are intended to implement or execute.

Choose Author Mode when ChatGPT Pro must write the exact source and PatchBay should act only as deterministic transport, candidate builder, check runner, and evidence return path.

Do not begin in one mode and recover by silently delegating into the other. Switching modes is a visible user/manager decision with a new task boundary.

## Evidence origin

Manager reports are worker-authored assertions; native completion is lifecycle evidence. Author receipts record observed processes and identities, while report assertions originate in the candidate program. Passing checks permit review, not implicit acceptance or publication. Source boundary equality is not continuous immutability. See [S07](../repairs/2026-10-22-s07-evidence-authority.md).

For both workflows, the supported initial maintenance contract drains work before restarting its owning service unit. Author process detachment is not cgroup independence. Manager/Luna native continuation has separate custody and qualification. See [S08](../repairs/2026-10-22-s08-deployment-durability.md).
