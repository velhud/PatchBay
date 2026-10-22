# S08 deployment, release and recovery contract

This implements the public S08 audit against published S07, rather than applying
its older `c3b2ef8b` proposals unchanged. It preserves independent Author and
Manager/Luna endpoints, state and authority. Local fixtures below do not qualify
an installed service, ordinary Chat, a native model, or macOS execution.

## Finding dispositions

| S08 finding / request | Source decision and local evidence | Remaining boundary |
|---|---|---|
| F01 / CR01 | Select drain-before-owning-unit maintenance. Existing endpoint-process continuity and conservative recovery remain. | No promise of running work surviving a systemd unit stop, execution-owner restart, Docker restart or reboot. |
| F02 / CR02 | Author startup captures source, wheel RECORD when available, interpreter and dependency versions. Current disk observation stays separate. Drift invalidates qualification and new interface writes; dispatch and child compare loaded release identities before execution. | Immutable release and venv are deployment obligations. Mutable dependency/native replacement is unsupported; record and requalify the actual route. |
| F03 / CR03 | Hub V2 authoritative connections use WAL/FULL, matching Author and Edge's FULL policy. Author Git writes explicitly select committed-object/ref fsync with fsync method and require Git >=2.36. | Storage/controller honesty and abrupt power-loss behavior require a disposable storage/VM trial. |
| F04 / CR04 | Explicit backup and restore validate SQL source identities against each correct private mirror, expected trees and retained Author-ref ancestry, including historical and pending references. Unique commit metadata is batched. | This bounded maintenance scan is not a new poll, scheduler or continuously cached assurance. Over-budget inventories refuse maintenance. |
| F05 / CR05 | Restore sets mode before file sync; validates content and relations; syncs final files and directories bottom-up, renames, then syncs the parent. Post-publication failure retains the destination with `RESTORE_PUBLICATION_UNCERTAIN`. | Ordered fault fixtures do not simulate hardware power failure. |
| F06 / CR06 | Keep explicit manual offline restore/cutover. Tests preserve historical logical identity and source records. No startup replay, authority refresh or activation automation was added. | Old credentials, revocations, approvals, leases and effects must be reconciled before manually reopening. No global anti-replay epoch or clone activation guarantee. |
| F07 / CR07 | Private config publication uses a 0600 sibling temp, file sync, atomic publication and directory sync. Every launcher invocation gets a unique no-clobber generation; the child verifies its expected hash before initialization. | Config parent ancestry is operator-controlled; generation retention is an operator policy. No S09 cleanup feature. |
| F08 / CR08 | Coupled launcher stop paths share the existing outer 45-second server budget instead of a shorter inner three-second kill. Recording tests cover tunnel exit, quit and interruption. | No arbitrary-fleet graceful-stop guarantee; retain exact per-worker cancellation/cleanup evidence. |
| F09 / CR09 | Supply a deployment component custody template and offline restore/cutover runbook; distinguish database snapshots from Manager source/native custody. Existing SQLite backup engines remain. | Complete private Manager corpus/native continuation and installed restore rehearsal were not run. |
| F10 / CR10 | Manager rejects a supplied untrusted/malformed Origin before auth, session creation or domain work. Absent Origin supports nonbrowser MCP clients; bearer checks remain independent. | Real browser/proxy/client behavior requires exact-route qualification. |

## Supported service lifetime

Author supervisors use new sessions to survive an endpoint **process** exit;
this does not move them outside an inherited cgroup. A service manager can kill
all members of its unit. Do not use `KillMode=none` as a continuity remedy.
For the supported initial profile, stop admissions, drain active checks and
finish or explicitly resolve unknown execution/cleanup before restarting the
owning unit, changing code/venv/configuration or replacing storage. Preserve
read-only recovery. A local Manager graceful stop requests worker cancellation;
Hub and Edge have different operation/attempt owners and must be assessed
separately. Hub backup admission freeze drains short dispatch sections, not all
running native workers.

## Loaded identity and qualification

Use immutable versioned release and virtual-environment paths. Do not overwrite
files or upgrade dependencies inside a running installation. Capabilities report
`loaded_runtime_sha256`, `startup_source_sha256`, `observed_source_sha256`,
`source_drift`, and the output-profile/catalog/runtime binding. The loaded hash
never changes to claim that old Python objects loaded replacement source.
Dependency versions and interpreter/installation identities are captured once,
not by an expensive per-request dependency scan. An editable installation has
no wheel RECORD assurance; source equality alone is not installed-artifact proof.

`PATCHBAY_AUTHOR_DEPLOYMENT_SHA256` may bind an operator's private deployment
manifest by its opaque SHA-256 only. It must be 64 lower-case hex digits; the
manifest and credentials are not returned. Change it and restart/requalify when
auth/principal/ingress/unit/mount or other deployment assumptions change. The
value `not_recorded` explicitly provides no deployment-manifest binding. Do not
infer current-route qualification from a source catalog or a historical receipt.
Author checks capture the loaded release hash and selected service-stop contract
in environment evidence; a mismatching new supervisor refuses before candidate
execution. Recovery/cleanup remains available; no hidden Author worker is added.

## Durability and restore publication

FULL SQLite and explicit Git fsync strengthen acknowledgement boundaries; they
are not multi-store transactions or evidence that every filesystem honors sync.
Author source relation checking is invoked on explicit maintenance. It checks
current/origin/base and historical revision, context, draft/version, run,
approval, publication and nonterminal operation commit/tree relationships.
Object presence without retained private-ref ancestry is insufficient. The
manifest's relation digest is additive to format 1; old archives are still
checked against the same SQL/Git rules rather than grandfathered as valid.

A restore failure before rename leaves no published destination. After rename,
a directory-sync or final-open failure returns `RESTORE_PUBLICATION_UNCERTAIN`
and retains the target for inspection. Do not delete or blindly retry over it.
Preserve the archive and target, determine what was published, and use another
new target for any subsequent offline attempt. Successful restoration preserves
historical instance identity, tokens and writer epochs; it grants no new current
authority by itself. Follow the [cutover runbook](../maintainer/workflows/deployment-release-cutover.md).

## Manager ingress and launch configuration

`server.allowed_origins` is an authorization gate for supplied Origin headers,
independent of `server.enable_cors`. It is an explicit list of serialized HTTP(S)
origins with no path, credentials, query, fragment, wildcard or `null`. Host and
forwarded headers never populate it. If omitted, only configured-port loopback
origins are admitted. An empty list permits only absent-Origin clients. The
shipped list remains the two local UI origins. An admitted Origin does not
replace bearer authentication. CORS response handling alone is not this check.

Launcher generations are unique private files. Saved preferences and print-only
preparation retain their existing semantics. The child hashes the exact bytes
it loads, checks `PATCHBAY_CONFIG_SHA256`, and reports `configuration_sha256` on
its authenticated health response; compare this with `runtime_config_sha256`
from preparation. This hashes configuration bytes, not effective secrets from
the environment or an observed proxy topology. Do not treat it as full deployment
identity. Stop and inspect a post-publication sync error; it is not launch success.

## Checks and qualification limits

Run the full [testing matrix](../../TESTING.md), including old-schema restart,
a separate Author suite, offline wheel install and source parity. The S08 subset
adds file-publication failures/positive cases, unique config generations,
loaded/disk drift and qualification refusal, supervisor mismatch refusal,
SQL/tree/retention corruption, archive rehash corruption, restore sync ordering
and pre/post-rename faults, Origin/auth HTTP cases, teardown budgets and actual
Hub PRAGMAs. These are disposable deterministic fixtures. The retained external
implementation receipts record exact commands and results, including failures.

Unrun: actual service-unit/cgroup cutover, native Manager/Luna continuation,
complete deployment component rehearsal, Docker/OOM/live-restore, abrupt reboot
or power loss, native macOS, and ordinary Chat/Pro/browser/proxy qualification.
S09 retention/resource research remains separate.
