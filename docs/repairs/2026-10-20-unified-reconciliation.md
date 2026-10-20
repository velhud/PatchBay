# Main reconciliation and recent branch retirement

## Current state — 21 October 2026

Main contains the worker-runtime repairs, independent Author Mode, package
requirements, reliability work and Linux qualification repairs. The tested Linux
source is `f611a02a89c8154d93d424a715dba9716aed23fd`; subsequent documentation
and ignore-rule maintenance does not change that tested Python source.
Publication uses direct main commits. No PR creation or UI work is part of this
reconciliation. No older branch or dependency branch is included in retirement.

## Four recent branches reviewed

| Branch | Pinned tip | Disposition |
| --- | --- | --- |
| `chatgpt/patchbay-unified-reconciliation-20261020` | `33a7c588d6b5edc5474a7ea8fc5a5628d6c3fef7` | Entire history is already an ancestor of main. No missing source changes. |
| `chatgpt/patchbay-critical-fixes-20261019` | `58bb843baa3fd810c11071a1eed02f6b44cfd82f` | Entire history is already an ancestor of main. No missing source changes. |
| `chatgpt/source-export-20261019` | `d9d01bc3c1244a25a1c3b44390541cb6be7088f6` | Four unique delivery commits change only CI source-export configuration and a trigger. No application fix to import. |
| `chatgpt/patchbay-fixes-20261019` | `d6ce04c2612f328f59e3b7041894e7e353f43021` | Four unique delivery commits add a temporary source-export workflow, trigger and public archive link. No application fix to import. |

The two export branches were compared from their merge bases and their unique
commit histories were reviewed. Their export-only configuration is deliberately
excluded from main. In particular, broad hidden-file artifact uploads are not
needed for the application. A private verified Git bundle preserves all four
branch histories before deletion; main retains all incorporated implementation
history. The unified and critical-repair remote refs were already absent at this audit;
local cached refs still named their tips. The two remaining export refs can be
removed without adding obsolete export workflows or creating artificial source
merges. The four selected histories are retained in the verified bundle.

All existing Dependabot and older remote branches are preserved unchanged. The
recent `dependabot/pip/gitpython-3.1.62` ref was also already absent remotely; its
local cached name is not evidence of a live branch. Main already pins GitPython
3.1.62 consistently. No dependency ref is merged, recreated, deleted or updated
by this reconciliation.

## Incorporated implementation

The critical repair history remains the manager/worker foundation:

- complete Git candidates, delivered-snapshot integration baselines and exact
  content acceptance;
- preservation of undelivered changes and read-only enforcement;
- lossless source pagination, process ownership and signal delivery;
- complete Pro response dispatch and cross-process request serialization;
- atomic Hub/Edge projections, compatibility layers and durable recovery.

Author Mode remains independent, with deterministic packages, isolated
candidates, approved checks, leases, receipts, feedback and maintenance. It does
not initialize or delegate implementation/recovery to Codex workers. Shared
primitives do not collapse workflow authority or private state.

Later main changes add scoped principal credentials, writer fencing, policy
invalidation, replayable operations, joined recovery, recognized-package import
and opted-in watch, bounded source/results, typed output contracts, measured
resource admission and explicit cross-Edge retrieval. Current delegated worker
policy permits only explicit `gpt-6-luna`, high by default, with xhigh/max allowed;
no family fallback or escalation is authorized.

Linux repairs cover inherited cgroup limits, safe cleanup/retry after backup
publication failure, and sanitized custom Codex-home hints. The older
`ReliableWorkerRuntime` monkey-patch remains inactive.

## Verification and remaining qualification

The published Linux source completed **1,203 passed, four skipped, zero failures**
with long checks and an immutable Docker image enabled. The separately bounded
storage lab passed six cases. See the [Linux evidence record](../testing/linux-qualification-2026-10-20.md)
for exact scope and remaining limits, and [TESTING](../../TESTING.md) for commands.

Formal research scenario evidence remains five PASS, zero FAIL, 45 NOT_RUN and
two BLOCKED_ENVIRONMENT. No actual ordinary-Chat trial, production ingress,
real worker/account trial, host reboot, hard power loss or production capacity
is qualified by the local results. The [prepared client trial](../authoring/ordinary-chat-trial.md)
is setup for observing those client behaviors, not an already connected client.

## Public exposure review

The review covers main changes since
`678aa3ff93dda5cbfd2c517234504170d4cd3db7` and all unique objects on the four
retired branches. Credential-signature and operator-identifier checks covered
297 changed/historical blobs; no matching credential or personal-machine
identifier was found. Generic quoted-token and tokenized-URL scans also produced no candidates.
Authentication, credential preparation, public-path hints and artifact export
configuration were checked alongside these scans.
This is a bounded public-exposure review, not a proof that every vulnerability
is absent or a clearance of older retained branches.

The documented `.patchbay-author/` home and repository-root `results/` output
are now ignored because they can contain credentials, leases, raw evidence and
private task data. Source exports should use `git archive`; broad filesystem
uploads can include ignored private files. Existing private material is not
removed or altered by these ignore rules.
