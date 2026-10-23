# Local Author persistence, manager cleanup and context efficiency

This change carries portable repairs from disposable local qualifications into
the public source. It does not deploy a service, refresh an installed plugin,
grant access to another repository, or qualify every supported platform/client.

## Durable source authoring

An ordinary ChatGPT Extra High client exercised direct Author MCP edits against
a narrowly scoped disposable task. The client created a draft file, changed it
in a second edit, read the exact version, recovered the task and read it again.
The operator independently reopened the stored bytes/Git objects and the local
export. The extended qualification covered eight files, seven draft versions,
roughly 48 KB of source and a 22-minute client task. Exact same-key replay did
not duplicate an update; a stale draft version/before-hash was rejected without
changing saved source. Restart and reconnect preserved readback.

The primary delivery path was versioned Author tools into host-owned storage,
not the client's Python sandbox or a final downloadable client ZIP. Native
file/package import is an additional channel and is not qualified by these
direct-edit results. A draft is not an accepted candidate; sealing, authorized
checks, approval and publication remain distinct. macOS storage/readback does
not qualify Linux/WSL2 Author execution, and the platform guard remains intact.

Use the supported registration, principal, qualification and lease workflow in
[QUICKSTART](../../QUICKSTART.md), [Author operations](../authoring/operations.md)
and [qualification](../authoring/qualification.md). Discover installed tool
schemas rather than copying private trial identifiers or hand-writing success
records. Keep runtime homes and evidence outside a public source export.

One early task-owned transport adapter altered the JSON representation of
source text during response handling. Exact MCP readback exposed the mismatch;
the adapter was corrected to preserve the native source payload. Treat a
gateway's response transformation as a separate verification boundary, compare
actual received bytes and do not infer correctness from HTTP200. Adapter code,
credentials and private lease/session handles are deployment evidence, not
portable package changes or a new public upload product. A recovered client
must still obey the native writer-session/lease fencing; an inaccessible chat
does not authorize bypassing those checks.

## Actual manager qualification and repairs

A separate ordinary ChatGPT Extra High client managed two writing workers and
one read-only reviewer, all explicitly using `gpt-6-luna` / `high`. The retained
qualification lasted about 38 minutes and included 13 native worker turns,
review/repair, six integrated fixture files and 63 focused offline tests.
The same retained worker/session executed another verification after a
task-owned native-service restart. No replacement workers or public commits
were needed to recover that fixture. These are historical results from the
pinned trial runtime; they do not automatically qualify a later source build.

The trial exposed these concrete failures:

| Symptom | Cause and repair | Boundary preserved |
| --- | --- | --- |
| Worker writes rejected before implementation | Brief identified only the base checkout. Continue the same worker, authorize its allocated isolated worktree and use its current directory/relative paths. | Keep the native filesystem sandbox; do not broaden roots to fix a brief. |
| Completed job remains cleanup-blocked after a short-lived child exits | Darwin can lose a child between identity lookup and kqueue registration. Consult the existing kernel/status-backed liveness check before marking a missing identity/registration uncertain. | Unknown/live children remain fenced; no unknown PID is signalled and no cleanup proof is fabricated. |
| Executor cannot bind process birth on Darwin | Use the supervisor's existing Darwin identity helper when Linux identity is unavailable. | Identity remains birth-bound; PID alone never authorizes signalling. |
| File hash is correct, but the last page remains perpetually truncated | Physical iteration omitted the final empty logical line counted by `split('\n')`. Supply that line, including empty-file line 1. | Preserve bytes, digest, limits, line coordinates and oversized-line refusal. |
| Wait returns immediately on every request | Completed/actionable workers remain selected. Inspect those reports first, then use `active_only=true` for remaining active work. | Quiet work and a timeout remain separate from failure. |

The public allocator also supports `workers.detached_worktrees: true` for
operators who need isolated durable worktrees without creating worker branches.
The default remains false. Both modes pin the exact source revision and retain
the same ownership, cleanup, review and integration checks. Detached workers
have no branch name; Git objects and worktrees retain their source. This is an
explicit configuration choice, not permission to delete existing branches.

Darwin's fallback census still does not establish lossless hostile descendant
tracking. A successful trusted fixture is not adversarial process containment.
Any genuinely unknown cleanup result stays blocked. The complete official
Codex installation, including execution hosts/resources, is required; copying
only its executable or passing a version/catalog check does not prove a usable
native execution path.

## Context and code organization

- Identical selected source paths are loaded once, preserving first occurrence
  order and the applicable instruction chain; changed source is read anew.
- Discovery prunes only workspace-relative subtrees proved excluded by an
  existing `directory/**` policy. Ordinary path checks still run; symlinks are
  not followed and name-only exclusions gain no new descendant semantics.
- Eligible small JSON text results use literal UTF-8. Decoded results are
  unchanged, and large results retain their bounded summary/full structured
  evidence. This is a byte reduction, not a token-price or client-context claim.
- Static descriptors move to `protocol/tool_catalog.py`; stateless event
  helpers move to `jobs/event_helpers.py`; discovery helpers move to
  `workspace/discovery.py`. Public wrappers, validation, signatures, override
  hooks, checkpoint clock and execution authority remain in their owners.

See [context loading](../reference/context-loading.md) for the precise contracts.
The disposable 20-tool gateway measured a catalog byte reduction of 18.67%
while preserving its allowlist/schema/guards. That measurement is not a claim
that every canonical tool catalog shrinks by the same percentage. Recorded
native event replay produced 2,464 identical parser outputs. Neither module
extraction nor replay replaces fresh native execution qualification.

## ChatGPT loading failures and recovery

Two distinct client failures were observed:

1. A conversation-history request returned HTTP 429 with `Retry-After: 11`.
   Honoring the backoff and using the page's normal retry returned HTTP 200 and
   restored the saved conversation. Repeated reloads are not a storage repair.
2. Individual completed answers displayed “This response couldn't load” after
   HTTP 200. The observed client marker was `data-dil-render-failed`, the DIL v2
   interactive-response fallback. Normal browser reload or Back/Forward remount
   restored five saved final answers without changing PatchBay. Two recovered
   history deliveries had identical final content, programs and completion
   statuses. The original failed body and exact historical renderer exception
   were not captured; a permanent upstream fix is not claimed.

A fresh three-call Extra High turn then passed self-test, retained-worker
recovery and exact file readback. Its final answer survived native reopening
and an automation reload. React hydration error 418 also appeared during a
successful render, so that diagnostic alone does not establish the failure
cause. Debugger detach alone did not restore the five earlier fallbacks;
automation attachment is not established as the sole cause either.

After a missing answer, recover the task/worker and inspect durable operation
and delivery evidence **before** retrying a mutation. A successful HTTP response
does not establish source correctness, execution success or a displayed final
answer. Conversely, a display failure does not undo a completed write. Reuse a
same-key request only with identical arguments and supported replay semantics;
corrected intent requires a new key. No rendering workaround should weaken
authentication, containment, ownership or qualification controls.

## Repository verification and known test failures

The optimization snapshot before reconciliation with newer public commits
passed 1,478 tests with 40 skips, the Author subset passed 259 with 36 skips,
the MCP evaluator passed 29 checks and Hub V2 passed 32. These counts describe
that exact earlier source, not the combined release. The current release's
verification is recorded separately in `TESTING.md` and its release result.

An early archive-based test attempt lacked Git metadata required by preparation
fixtures; restoring a disposable detached HEAD/index corrected the setup.
One early Hub evaluator failure lost its exact abbreviated phase when its
temporary database disappeared. The standalone and final suite reruns passed;
the assertion now retains the exact error and failed checks. Its historical
cause remains unresolved; no timeout, guard or assertion outcome was relaxed.

The publication regressions also initially found two mistakes in the newly
added test harness: synthetic kqueue flags collided with a native flag, and
the worktree test lacked its Git import. Correcting the harness yielded nine
passing focused cases; neither was an MCP or product runtime failure.

The combined Mac run also exposed five newer Author cases that called the
Linux-only execution-admission path without platform markers. They correctly
received `BLOCKED_ENVIRONMENT`. Their Linux/WSL2 requirement is now explicit,
matching the adjacent execution cases; the product guard was not changed and
the skipped cases do not establish Mac Author execution support.

## Public evidence boundary

Only generic source, tests, operating instructions and bounded qualification
summaries belong in this repository. Credentials, capability URLs, raw HARs,
transcripts, local machine paths/identities, private project names, browser
screenshots and complete worker prompts remain private. Product `AGENTS.md`
contracts remain public; personal/global agent instructions do not belong here.
See the [publication checklist](../maintainer/workflows/public-source-release.md).
