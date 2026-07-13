# Hub V2 And Edge Backup Restore

Hub V2 state and every Edge journal are private WAL-mode SQLite databases. A
main-file copy is not a valid online snapshot. Use the operator commands below;
they use SQLite's `Connection.backup()` API, run `PRAGMA integrity_check`, and
write a `0600` database plus `0600` manifest into a `0700` destination
directory. These commands are not MCP tools and are not part of the ChatGPT
manager surface.

This is distinct from the V1 JSON migration snapshot in the V1-to-V2 migration
runbook. `patchbay hub backup` protects an already-running V2 Hub database.

## Create And Validate

On the Hub, use the configured `hub.state_db` by omitting `--database`, or pass
the exact live SQLite path explicitly:

```bash
patchbay hub backup create \
  --database /private/runtime/hub/hub-state-v2.sqlite3 \
  --backup /private/backups/hub/hub-2026-07-12.sqlite3 \
  --drain-timeout-seconds 30 \
  --deployed-revision "<deployed-commit>" \
  --json

patchbay hub backup validate \
  --backup /private/backups/hub/hub-2026-07-12.sqlite3 \
  --expected-generation "<hub-id>" \
  --expected-deployed-revision "<deployed-commit>" \
  --json
```

On each Edge, name its local journal explicitly. The Edge generation is an
immutable fence, so pass it during backup and validation when it is known:

```bash
patchbay edge backup create \
  --database /private/runtime/hub/edge-v2-journal-<edge-generation>.sqlite3 \
  --backup /private/backups/edge/edge-2026-07-12.sqlite3 \
  --expected-generation "<edge-generation>" \
  --deployed-revision "<deployed-commit>" \
  --json

patchbay edge backup validate \
  --backup /private/backups/edge/edge-2026-07-12.sqlite3 \
  --expected-generation "<edge-generation>" \
  --json
```

Each bundle consists of the database path passed to `--backup` and its sidecar
`<backup-path>.manifest.json`. The manifest records:

- the explicit `hub_v2` or `edge_v2` database kind;
- the Hub id or immutable Edge generation;
- SQLite schema version and `integrity_check` result;
- source path, SHA-256, size, and mtime observation;
- backup SHA-256 and size;
- Hub V2 contract version and hashes plus the optional deployed revision; and
- a complete deterministic proof of every durable table, every entity type,
  schema/index metadata, Hub/Edge identity, operations, attempts, receipts,
  events, payload metadata, and current control-plane projections. Compatibility
  summaries for groups, operations, attempts, and receipts remain included.

The manifest has its own SHA-256 checksum. Validation fails on database,
manifest, permissions, kind, generation, integrity, or logical-state-proof
drift. SHA-256 detects accidental tampering; archive storage controls remain
the authority for protecting a bundle from a deliberate attacker who can
rewrite both files.

## Restore Safely

Normal restore targets a path that does not exist. It never overwrites a
different database, WAL, or shared-memory sidecar, so it cannot replace a
running Hub or Edge state file in place. There is one crash-recovery exception:
if a prior invocation published the restore database but died before returning,
an exact byte/state/manifest match at the same target is validated and reused.
Any non-matching existing target or sidecar is refused in place. This makes the
command retry-safe without turning it into an overwrite operation.

```bash
patchbay hub backup restore \
  --backup /private/backups/hub/hub-2026-07-12.sqlite3 \
  --restore-to /private/recovery/hub-state-v2.sqlite3 \
  --expected-generation "<hub-id>" \
  --json

patchbay edge backup restore \
  --backup /private/backups/edge/edge-2026-07-12.sqlite3 \
  --restore-to /private/recovery/edge-journal.sqlite3 \
  --expected-generation "<edge-generation>" \
  --json
```

Restore validates the source bundle first, copies through SQLite's backup API,
runs `integrity_check`, compares the deterministic state proof, then opens the
fresh or exactly reusable output through `HubStoreV2` or `EdgeJournal`. The
success report's `publication.database` field is `created` or `reused` and proves
Hub groups, operations, attempts, and receipts or Edge group correlations,
intents, attempts, and receipts survived. Inspect that fresh output before a
separate, deliberate cutover procedure points any process at it.

## Admission Freeze Contract

SQLite backup is transactionally consistent while the service is live. Hub V2
also coordinates backup admission across processes through a private lock
directory adjacent to the configured database. The server and
`patchbay hub backup create` derive the same coordination path. The backup
process publishes a private owner marker, blocks new manager mutation
admissions and new Edge claims, acquires the exclusive admission lock after
already-admitted short dispatch sections drain, then snapshots the database.
Read/status calls, Edge result upload, receipt acknowledgement, and
reconciliation remain available.

The same contract is available to embedded callers:

```python
from patchbay.hub.backup_v2 import (
    AdmissionFreezeController,
    admission_coordination_path,
    create_hub_v2_backup,
)

admission_gate = AdmissionFreezeController(
    admission_coordination_path(hub_database)
)

# The running Hub uses the same gate around new mutation admission.
with admission_gate.admit_mutation():
    dispatch_new_mutation()

# Backup coordinator boundary:
create_hub_v2_backup(
    hub_database,
    backup_path,
    admission_freeze=admission_gate,
    drain_timeout_seconds=30,
)
```

`freeze_admissions()` waits only for short admitted dispatch sections. It does
not cancel Codex work, declare active workers complete, or discard receipts.
Kernel locks are released automatically if either process dies; a PID/start
identity check removes a stale owner marker on the next request.

This online contract is available only when both the running Hub and backup CLI
come from a release that implements the shared gate. For the first upgrade from
an older Hub, stop the old Hub service, take and validate the backup while it is
offline, and add `--prepare-migration`:

```bash
patchbay hub backup create \
  --database /private/runtime/hub/hub-state-v2.sqlite3 \
  --backup /private/backups/hub/pre-upgrade.sqlite3 \
  --prepare-migration \
  --deployed-revision "<old-deployed-commit>" \
  --json
```

That command writes a private marker beside the source database by default. The
marker binds the validated backup manifest to the exact Hub id, old schema
version, target schema version, and complete source-state hash. Opening an older
Hub store without this valid marker fails before migration. A source mutation
after marker creation also invalidates startup, so the old service must remain
stopped until the new release starts. `hub.pre_migration_backup_marker` may name
an explicit marker path when the operator cannot use the default adjacent path.

The new process validates the marker once before opening the database and again
while holding SQLite's migration write reservation. It never treats a backup of
similar-looking state as approval for a changed source. Current-schema and
provably new databases do not require a marker. For subsequent V2 rollouts,
online backup is supported. If `--drain-timeout-seconds` expires, backup fails
without publishing or deleting another process's manifest.
