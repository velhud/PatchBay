# PatchBay

Author trial follow-up: recovery distinguishes source provenance from the exact
candidate context, and nonpublishing candidates await review. See the
[repair and deployment handoff](docs/repairs/2026-10-22-author-trial-follow-up.md).

PatchBay is a local control plane for two complementary ChatGPT Pro workflows:

1. **Manager/worker mode** — ChatGPT Pro plans, delegates, reviews, and coordinates while local Codex workers investigate, implement, execute, and verify inside approved repositories.
2. **Author Mode** — ChatGPT Pro writes the implementation itself. PatchBay transports a deterministic source package, validates it, materializes an isolated candidate, runs authorized checks, and returns bounded evidence for correction. Author Mode does not initialize, call, or recover through Codex workers.

The workflows share repository, Git, process, storage, validation, and evidence infrastructure, but they do not share implementation authority. Choose one workflow deliberately for each task.

## Workflow boundaries

| Boundary | Manager/worker mode | Author Mode |
| --- | --- | --- |
| Who writes implementation code | Local Codex workers | ChatGPT Pro |
| PatchBay role | Worker orchestration and review transport | Deterministic package transport, candidate construction, checks, and evidence |
| Codex worker initialization | Required for delegated implementation | Forbidden |
| Automatic model fallback | Forbidden; choices remain explicit and visible | Not applicable |
| Primary entrypoints | `patchbay start`, `patchbay stdio`, Hub/Edge commands | `patchbay author ...` or `patchbay-author ...` |
| State | Worker/job/worktree and optional Hub/Edge state | Independent Author home, leases, tasks, revisions, operations, approvals, and publications |

The detailed contract is in [Workflow modes and authority boundaries](docs/reference/workflow-modes.md).
For a practical first task, use the [ChatGPT heavy-task guide](docs/user/chatgpt-heavy-tasks.md),
with separate prompts for direct Author coding and worker delegation and explicit
limits on what the recorded trials establish.

## Manager/worker mode

PatchBay exposes a local MCP server over Streamable HTTP or stdio. ChatGPT can open approved workspaces, appoint durable named workers, continue them by name, inspect reports and bounded evidence, and explicitly integrate accepted isolated work.

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

Use the options tool to check observed availability. An unavailable selector is a blocker, never permission to substitute.

### Manager quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e '.[test]'

patchbay doctor
patchbay start --root /absolute/path/to/disposable/repo --tool-mode worker
```

For ChatGPT web, use an authenticated HTTPS tunnel and the private Server URL printed by PatchBay. Start with a disposable repository and keep `--tool-mode worker` for the first real connector run.

Optional Hub/Edge mode provides the durable multi-machine `patchbay_*` manager surface. Hub V2 is the default control plane; V1 is an explicit compatibility selection.

## Author Mode

Author Mode is an independent, deterministic authoring subsystem. It accepts a manifest-bound package from ChatGPT Pro, validates every path and object, constructs an isolated candidate from an exact base, runs policy-authorized checks, and publishes evidence. It does not delegate implementation or recovery to a model.

Author Mode qualification and execution are intentionally guarded for Linux or WSL2. Do not weaken that guard to make macOS qualification appear green.

```bash
AUTHOR_HOME="$PWD/.patchbay-author"

patchbay author --home "$AUTHOR_HOME" init
patchbay-author --home "$AUTHOR_HOME" doctor
patchbay-author --home "$AUTHOR_HOME" token --role reader
```

The full register → task → claim → context → apply → check → feedback → approve → publish flow is in [QUICKSTART.md](QUICKSTART.md).

Recovery is available through `author_recover_task` and `patchbay-author recovery TASK_ID` without starting a model or a check. Explicit inbox import or an opted-in folder watch routes recognized packages through the same validator. Durable retry keys, writer epochs, current-policy receipts, bounded source windows, and operator pause/revocation protect the loop across disconnected requests. See the [Author operations contract](docs/authoring/operations.md) and [package evidence map](docs/authoring/coverage-map.md).

Author MCP exposes eleven read tools; a qualified author profile adds nine writes, and a separately qualified operator profile adds approval and publication. Qualification binds the exact runtime, schemas, role, client route, and output profile. The compatibility output profile remains the default; concise structured output requires its own observed client qualification. No PR creation or automatic merge is part of this workflow.

S05 strengthens selected-context exports, exact unsealed-draft byte reads,
verified artifact delivery, transactional policy/cancellation fences and uncertain
publication readback. See the [Author custody contract and operator handoff](docs/repairs/2026-10-22-s05-author-custody.md).
These source changes do not update or qualify a running connector.

## Installation and entrypoints

Python 3.10+, Git, and the project dependencies are required. Manager/worker execution also requires a configured Codex CLI. Author Mode does not require Codex workers, but check execution is subject to its Linux/WSL2 qualification guard and task policy.

Installed entrypoints:

```text
patchbay
patchbay-stdio
patchbay-author
```

`patchbay author ...` and `patchbay-author ...` reach the same Author CLI. Global Author options such as `--home` must appear before the Author subcommand.

## Safety boundaries

- Localhost and read-only orientation remain the defaults.
- Public or tunneled access requires authentication.
- Repository paths, package paths, exports, and materialized files are validated against their declared roots.
- Isolated worker integration is preview-first and explicit; it does not commit or delete the worker worktree.
- Review targets retain exact candidate source; dependent lanes can explicitly start from that snapshot. Controller review, delivery and combined-check evidence remain distinct. See [S06 contracts](docs/repairs/2026-10-22-s06-candidate-delivery.md).
- Author packages are deterministic inputs, not instructions to launch another model.
- Unknown execution ownership, stale leases, stale acceptance, or uncertain process identity fail closed.
- Public status and evidence are bounded and sanitized. Private instance identity and unsafe path details are not projected unnecessarily.

PatchBay is independent open-source software and is not an official OpenAI product. See [SECURITY.md](SECURITY.md), [NOTICE](NOTICE), and the usage-boundary documentation under `docs/security/`.

## Verification

The unified verification matrix is in [TESTING.md](TESTING.md). It separates:

- compilation and deterministic tests;
- critical audit regressions;
- Author Mode tests and Linux/WSL2 qualification;
- local MCP and Hub/Edge evaluators;
- production restart/upgrade rehearsal;
- the optional real-Codex worker trial, which depends on a configured account and intentionally invokes workers.

The published Linux candidate `f611a02a89c8154d93d424a715dba9716aed23fd` completed 1,203 tests with four skips and zero failures, with long checks and an immutable Docker image enabled. The separate bounded storage lab passed six cases. See the [Linux evidence and remaining qualification boundaries](docs/testing/linux-qualification-2026-10-20.md) and [ordinary-Chat trial](docs/authoring/ordinary-chat-trial.md). Subsequent disposable ordinary-Chat coding trials exercised actual Author writes/checks and Luna worker execution; their [repairs and limits](docs/repairs/2026-10-21-live-trial-repairs.md) include a macOS cleanup fence and independent ChatGPT rendering failures. Production ingress and reboot/power-loss qualification remain separate, unrun boundaries.

Do not infer qualification from a clean merge or from historical branch test counts.

The [S01 MCP interface contract](docs/reference/mcp-interface-contract.md) covers
source selectors, supported-version negotiation, compact outcome evidence and
recovery after a result fault. These source changes require renewed route
qualification; they do not update or qualify existing deployed trial connections.
The [S01 implementation record](docs/repairs/2026-10-22-s01-interface-contracts.md)
separates repository verification from the remaining client/deployment gates.
The [S02 controller recovery contract](docs/repairs/2026-10-22-s02-controller-recovery.md)
describes optional local worker retry keys, notification admission, trusted
post-effect errors, takeover fencing and complete Author history traversal.
The [S03 observation contract](docs/repairs/2026-10-22-s03-observation.md)
describes silence-threshold publication, meaningful status deltas, shared
observation costs, bounded waits and authorized urgent Author admission.
Its local measurements leave native mixed-workload and client qualification explicit.

## Documentation

- [Quick start](QUICKSTART.md)
- [Workflow modes and authority boundaries](docs/reference/workflow-modes.md)
- [ChatGPT client instructions](docs/user/chatgpt-instructions.md)
- [Public tool surface](docs/reference/public-tool-surface.md)
- [Worker and transport details](docs/reference/tool-surface-and-worker-details.md)
- [Testing](TESTING.md)
- [Unified reconciliation record](docs/repairs/2026-10-20-unified-reconciliation.md)
- [Security](SECURITY.md)
- [CodexPro attribution](NOTICE)

The [S04 native execution contract](docs/repairs/2026-10-22-s04-native-execution.md)
adds durable typed failure evidence, restart-safe bounded session cursors,
explicit clipped reports with complete-source paging, bounded supervisor readiness
and passive launch provenance. Connector readiness, static installation matching,
native execution and client qualification remain separate. These source changes
do not update existing trial deployments.

The [S07 evidence contract](docs/repairs/2026-10-22-s07-evidence-authority.md) separates executor observations, program assertions and acceptance; documents strict JUnit, explicit executed-test policy, transformed source views and downstream publication consent. Publication does not qualify a deployed/client route.

S08 defines [immutable releases, service lifetime and durability](docs/repairs/2026-10-22-s08-deployment-durability.md), with a manual offline restore/cutover contract. Owning-unit maintenance drains work; source publication does not qualify a deployed service.

S09 bounds candidate Git capture, streams Author context/feedback export, rotates
queued check windows, and provides finite Manager inbox import defaults and
list pages. Existing Author state can explicitly install admission indexes.
See [repeated-use limits and retention decisions](docs/repairs/2026-10-22-s09-repeated-use.md);
these local mechanics do not qualify native/client throughput or cost savings.

The original audit programme ends at S10. Manager review targets now use durable
admitted-brief identity across reload without restoring raw prompt logging.
The [final capability disposition](docs/repairs/2026-10-22-s10-final-composition.md)
accounts for all inherited findings and separates checked local mechanics from
actual Pro/native/service/capacity gates. A structurally valid evidence record
is not an independent product acceptance certificate.

## Postimplementation reliability

Manager artifact imports run in bounded background writers; repeated waits on
unchanged mixed teams respect the wait budget. Author rejects hidden JUnit
outcomes and retains separate principal/role/profile qualifications. Existing
homes need explicit quiescent index maintenance, immutable release verification
and actual client requalification. See [postimplementation repairs and qualification limits](docs/repairs/2026-10-23-postimplementation.md).
