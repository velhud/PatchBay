# Linux qualification repairs and evidence boundaries

The Linux work starts from public main `760f7db4331d7a4e6888c66e1852d06aec6b5c6c`.
Exact final commits, raw commands, environment observations, failures, exclusions,
and file hashes belong in the private execution record for the run. The original
T01–T52 source register and its `NOT_RUN_IN_THIS_RESEARCH` statuses stay unchanged.
Source maps and historical Mac results are not Linux execution receipts.

## Reproduced repairs

- Resource admission previously read only the current cgroup. A disposable
  half-core/512 MiB parent with an unlimited child reproduced host-wide CPU,
  memory and PID capacity being reported inside the constrained child. Admission
  now walks the visible controller ancestry and uses each ancestor's own usage
  when comparing remaining memory and PID capacity. Fixtures cover v1/v2 and
  different limiting ancestors. Hidden ancestors above a namespace's visible
  mount and unreadable kernel files are outside this proof.
- Real ENOSPC during backup publication left an owned `.part` file that blocked
  retry. Failure cleanup now removes only the regular inode exclusively created
  by that invocation. Pre-existing partial files and symlinks are preserved.
  The bounded tmpfs lab exercises actual block/inode exhaustion, receipt and
  package failure boundaries, backup/restore, replay and reference-aware GC.
- A configured Codex directory inside the user home exposed its relative path
  through a public hint. Only the conventional `.codex` location now receives a
  recognizable home hint; custom locations use a generic label.

## Execution and preparation

`tests/authoring/test_linux_continuity.py` uses disposable jobs and an authenticated
loopback Author endpoint. It restarts only its endpoint while verifying the same
owned supervisor, fills the fixture's configured job slots with finite bounded
jobs, measures status/recovery/cancel latency, verifies unchanged reads do not
append events, and rejects forged process start/boot identities without signalling
the live fixture. This is local process and HTTP evidence.

`scripts/author_storage_lab.py` is an explicit opt-in lab with a private mount
namespace and bounded tmpfs. Its synthetic receipt-persistence boundary is not
a real check pass or a complete T37 run. Its exception/process-level faults do
not qualify power loss or physical storage durability.

`scripts/prepare_author_chat_trial.py` prepares a small broken application,
independent Author state, task-scoped credentials, pinned context and observation
templates. Server/runtime/profile facts are separate from unobserved client
fields. The configured check is deliberately unrun in the prepared real-client
fixture so its later actual Chat failure/correction/pass journey can be recorded.

Run the complete [TESTING matrix](../../TESTING.md), including the long-test flag,
prepared immutable Docker image, local evaluators and production restart/old-schema
rehearsals. Preserve actual platform skips and failures. For formal scenario
evidence, review the exact original setup, steps and pass condition before adding
a result; a related unit-test name alone does not satisfy a compound scenario.

## Remaining external qualification

Actual ordinary Chat account/model/client, metadata refresh, ingress deadlines,
tool selection, fresh-chat recovery and paired output handling require client
observations. Physical reboot and hard power-off require a disposable VM that
can be destroyed safely. A no-network container test does not prove the host was
offline. A configured job-slot/control reserve fixture does not qualify production
capacity or sustained host-wide saturation. Optional transports, profile changes,
publication and deployments retain their separate authority and evidence gates.
