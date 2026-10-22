# S04: native execution evidence and bounded recovery

This source follow-up starts from S03 `070d5a2ccfeb67c993386b5c69f372e4718744fc`.
The audit examined `362bd039`, with an S02 drift appendix; its 38 observations
and 48 proposed acceptance cases were review inputs, not 48 product passes.
No audit module was overlaid. S03 shared off-loop observation, fresh cleanup
barriers, admission and read-first waits remain in place. Author Mode remains
independent and does not import this execution profile or start native workers.

## Findings and owners

| Finding / change request | Disposition | Effective owner and boundary |
| --- | --- | --- |
| F01 / CR01 | Implemented locally | `jobs/session_terminal.py` checkpoints exact source generation and byte/record cursors; `JobExecutor._recover_completed_session` retains bounded observers and persists progress through `JobManager`. |
| F02 / CR02 | Implemented locally | `_observe_stdout_event` durably records typed negative evidence before telemetry/tail trimming. Recovery, semantic completion and process finalization consult the same receipt. |
| F03 / CR03 | Implemented with explicit attribution limit | Only `thread.started.thread_id` (including the existing typed data envelope) establishes native identity. Expected and observed identity stay separate. Worker message/integration preview refuse conflicts; launch and restart recovery check home/workspace bindings. External use of the same native thread is unsupported and unqualified. |
| F04 / CR04 | Implemented; native Linux qualification remains | Supervisor discovery returns immutable PID/start-identity references through the final signal boundary. Linux uses pidfds when available; other paths retain an explicit check-to-signal race. Unknown live ownership stays fenced. |
| F05 / CR05 | Implemented locally | Executor owns the exact subprocess handle across OS spawn, a finite nonblocking readiness handshake, cancellation and target-gate release. Optional startup serialization remains optional. |
| F06 / CR06 | Implemented locally | Explicit clipped preview metadata, retained full bounded report, and paged read-only `worker_inspect(view="report_source")` through local and Hub adapters. |
| F07 / CR07 | Implemented passive boundaries; native/client unrun | `native_profile.py`, the explicit static manifest checker, connector status and launch/trial provenance distinguish readiness, installation evidence and execution qualification. No automatic canary, model invocation or upgrade. |
| F08 / CR08 | Existing headless adapter retained and documented | No demonstrated need requires an App Server migration. Interactive approval/input and native/client qualification remain version-bound operator work. |

## Recovery bounds and source identity

Each poll reads at most 1 MiB of new transcript bytes. A JSONL record is bounded
at 4 MiB; oversized records are discarded through their delimiter and counted.
Restart reconstructs at most one pending record and one prior agent-message
record, each bounded at 4 MiB. Header/generation validation reads at most one
4 MiB record, and cursor validation reads at most 256 bytes. These are parser
work limits, not a constant total memory/RSS promise: decoded strings, temporary
JSON objects, complete report retrieval and response encoding also occupy memory.

The process-local recovery cache holds at most `max_concurrent_jobs`, clamped
to 1–32, and evicts terminal/missing owners or readers idle for 60 seconds when
recovery runs. Persisted checkpoints permit progress after eviction/restart.
The checkpoint binds the exact session, pre-turn baseline, file generation,
complete-record boundary and cursor content anchor. It stores ranges rather
than partial decoded text. Replacement, truncation, changed cursor content or
same-size modification stops that reader with an explicit source status.
An archived source can be rediscovered by a new observer only when exactly one
source declares that session and its persisted generation still matches.
A currently selected reader whose source disappears does not silently reset.

A missing pre-resume source does not establish an offset-zero baseline. A
separately valid durable stdout terminal receipt may still establish completion
when transcript evidence is unavailable. Stale recovery diagnostics expose
`session_source_status`; incomplete scanning remains pending until its bounded
reads finish. Historical snapshots without new fields remain readable.

Source-generation/cursor checks assume append-only managed transcripts; they
are not proof against arbitrary concurrent rewriting. Session identity and
timestamps cannot establish exclusive turn attribution when an external caller
uses the same native thread. Session results expose
`session_turn_attribution="managed_single_owner_assumption"` and
`external_session_use_qualified=false`. Keep each managed thread exclusive;
qualifying shared external use requires an adapter-native turn/owner binding.

## Terminal evidence and process ownership

Top-level `turn.failed` and fatal `error` are durable negative receipts. An
`item` error or report prose discussing a failed command does not by itself
fail the native turn. The first negative diagnostic is bounded to 2,048
characters and includes stream location, event hash, expected/observed session,
source/version and time. A later identity conflict upgrades the fence while
retaining the prior negative receipt. Contradictory stdout completion is kept
as evidence and cannot erase the negative outcome after restart.

Existing governing cancellation/failure, late evidence and cleanup outcomes
remain separate. A wrapper terminated after authoritative completion is not
indistinguishable from an unexpected native failure. Exit-only completion is
explicitly marked `native_finality="process_exit_only"` with
`native_terminal_evidence=false`; structured JSON validity alone does not prove
native finality or verification. Unknown events do not invent session identity.

Discovery retains `(pid, start_identity, ownership_basis)` through signalling.
On Linux, available pidfds receive the signal only after the numeric PID's
identity is rechecked after opening the handle; unsupported kernels use the
identity-checked fallback. Linux fallback and Darwin numeric signalling retain
a residual race between final identity check and the kernel call. They do not
provide the same atomic guarantee as pidfd signalling. Incomplete discovery,
unknown live identity and failed signals retain cleanup uncertainty and locks.

Darwin `proc_pidinfo` can withhold information for zombies while `kill(pid,0)`
still succeeds. A bounded `ps` status fallback recognizes an exited zombie or
an absent process; failed fallback discovery remains live/unknown. Existing
subreaper, marker, quiescent-scan and uncertainty-sentinel boundaries remain.
Recording-only PID-reuse tests never signal a fabricated PID. They and actual
macOS fixture processes do not qualify Linux pidfd/subreaper behavior.

`server.codex_supervisor_ready_timeout_seconds` defaults to 10 seconds and
accepts 0.1–120. It bounds the pre-target readiness handshake, distinct from
session startup, model turn and cleanup budgets. EOF, invalid readiness,
timeout or cancellation closes the sole target-release gate, then terminates
and reaps the exact unreaped child handle. Missing ownership/reaping proof
keeps the cleanup fence. There is no abandoned blocking readiness-reader thread
and no target release after cancellation. Startup serialization does not bound
model turns; disabling it does not remove ownership or cleanup requirements.

## Complete reports through the existing inspect tools

A clipped preview includes `report_truncated`, `report_completeness`, original
character/UTF-8 byte counts and retained character ranges. The default preview
is 200,000 characters and retains its beginning and end with an omission marker.
Very small custom limits retain a flagged prefix. Clipped JSON is not labelled
a native schema failure: the preview has no valid complete structured result.
The exact source record is retained independently where available; otherwise
`report_source_unavailable=true` stays explicit. Records beyond the 4 MiB
parser bound cannot promise a retained complete report.

Use the same worker with `codex_worker_inspect` or `patchbay_worker_inspect`:

```json
{
  "worker": "<returned worker identity>",
  "view": "report_source",
  "report_source_sha256": "<returned report_source.sha256>",
  "report_offset": 0,
  "max_bytes": 8192
}
```

Follow `next_offset` with the same source hash until `has_more=false`.
Pages default to 8,192 bytes, capped at 65,536, and never split a UTF-8 character.
An invalid offset or a budget too small for the next character is refused.
Selection is scoped to that worker's exact retained artifacts; callers cannot
supply a path. Symlink/nonregular files, changed hashes and oversized artifacts
are refused. Existing privacy filtering applies before paging. `source_sha256`
identifies the private retained original; `returned_source_sha256`, `redacted`
and `offset_basis="returned_utf8"` identify the returned representation.
This extends an existing tool; the canonical Hub catalog remains 31 tools.

## Passive installation and execution provenance

Connector readiness proves the read/control surface only. Catalog availability,
a PATH executable or a successful version command does not prove companion
execution resources. An operator can explicitly check a supplied exact-distribution
manifest offline, without executing the native program:

```json
{
  "format": "patchbay-native-distribution-v1",
  "version": "<declared distribution version>",
  "provenance": "<declared manifest origin>",
  "files": [
    {"path": "bin/codex", "sha256": "<64 hex>", "role": "launcher", "executable": true},
    {"path": "<required companion>", "sha256": "<64 hex>", "role": "companion"}
  ]
}
```

```bash
python scripts/qualify_native_installation.py \
  --root /absolute/distribution --manifest /absolute/manifest.json \
  --executable /absolute/distribution/bin/codex --output /absolute/new-private-receipt.json
```

Use the complete manifest for that exact distribution, not the illustrative
file list above. The helper checks 1–256 declared files, safe paths, hashes,
executable requirements and a 512 MiB aggregate cap. It does not authenticate
manifest provenance, discover undeclared required companions, execute commands
or certify native/tool functionality. Keep the receipt private; it contains
local bindings. An existing output is never overwritten. Set the optional
`server.native_installation_receipt` only to that operator-selected receipt.

Launch records pin the resolved logical PATH launcher (preserving venv/shim
semantics), hash its physical stat binding, home/workspace paths, selected
environment inputs, argv, output schema, profile/config overrides, bounded
home/workspace config/AGENTS files and rules. Rules are limited to 256 files per
root and approximately 4 MiB combined, with per-file limits and explicit bound
markers. This is declared coverage, not a complete native effective-config dump.
No configuration contents or private paths are exposed. The static receipt is
rechecked by stat bindings at launch; changed inputs make it stale. These passive
observations do not freeze files against concurrent replacement or prove the
native process's effective model. Requested Luna/effort and observed model/effort
remain distinct; unknown native facts stay null. Root argv does not qualify
nested helper model identities.

## Headless profile and remaining operator qualification

The supported adapter is `exec-jsonl-v1`: `codex exec` or exact-session
`codex exec resume`, JSONL stdout, stdin brief, explicit sandbox/model/effort and
an output schema. It does not provide an interactive terminal approval/input
channel. `--last` and an implicit replacement conversation are not recovery.
Interactive behavior depends on the exact installed release/configuration;
there is no new App Server adapter or guessed approval callback here.

After a separate explicit native/client trial authorization, an operator should:

1. Pin the published source, installed package, complete native distribution,
   platform, private home/workspace and relevant configuration. Retain the
   supplied manifest evidence separately from observed executable/tool behavior.
2. Run a disposable Luna high read-plus-command canary through the exact profile;
   retain typed events, command result, actual exit and complete report. A READY
   sentence, catalog or login alone is insufficient. Record unknown effective
   model/helper facts honestly, without substituting another family.
3. Exercise exact-session continuation, failure beyond display-tail eviction,
   restart, long/split reports, malformed terminal evidence, cold concurrent
   starts, pre-ready cancellation and detached descendants. Qualify Linux
   pidfd/subreaper and Darwin limits separately, preserving uncertainty fences.
4. Test blocked filesystem/network operations, command input/EOF, auth/network/
   quota errors under that release. Do not invent an interactive approval lane.
   Adopt an optional App Server adapter only for a demonstrated needed capability,
   with equivalent authority, cancellation, terminal and recovery evidence.
5. Separately capture the actual ordinary-Pro tool catalog, rendered/structured
   report-source pages, errors, disconnect/restart and long continuation.
   Refresh live deployments only under their own authorization.

No new native/model/paid/client/live/deployment trial was run for this source
change. S03 optional counter/compact profile, Author wait, mixed native workload
and client qualification debt remains. No quota saving or multi-hour reliability
claim follows from the local fixtures. See [TESTING](../../TESTING.md) for the
required local matrix and [workflow modes](../reference/workflow-modes.md) for
Author independence.

## Primary references

The version-bound event distinction was checked against
[OpenAI exec events at 83086188](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/exec_events.rs).
The [official non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode)
describes JSONL exec and exact-session resume. Neither establishes the installed
host's current native qualification. Python's
[pidfd signal interface](https://docs.python.org/3.12/library/signal.html#signal.pidfd_send_signal)
and the [Linux pidfd signal contract](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html)
explain the handle guarantee; fallback paths retain the stated race.
