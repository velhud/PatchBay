# Hub V1 To V2 Migration And Atomic Cutover Runbook

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Status: implemented internal cutover procedure. The production CLI selects Hub
V2 server and Edge behavior when `hub.control_plane: v2`; the explicit
migration/backup/doctor functions remain Python administrative APIs so an
operator cannot accidentally migrate state through an ordinary runtime command.

Normative authority:

- `resolved-contract-addendum.md`, sections 15, 16, and 18;
- the exact registry in `patchbay.hub.tool_surface`;
- `HubStoreV2.schema_info()` and its monotonic `v2_mutation_count`.

## Non-Negotiable Gates

Cutover must stop when any of these is true:

- V1 Hub or any V1 Edge polling loop is still running;
- a V1 command is `queued` or `running`;
- the V1 JSON version or exact top-level shape is not the supported V1 schema;
- source, backup, or backup-manifest checksums differ;
- V2 SQLite integrity, schema version, table set, migration lock, identity, or
  legacy classification fails doctor;
- the expected manifest, schema, or complete contract hash differs;
- any Edge reports a different contract hash, protocol version, action
  capability version, or generation at preflight/claim time;
- fewer or more than the exact 31 V2 tools are ready with implemented handlers,
  strict schemas, truthful annotations, semantic results, and live evidence.

Migration never edits, renames, quarantines, rewrites, or deletes the V1 JSON.
Apply imports the checksum-validated backup snapshot, not a live mutable file.

## Administrative Function Contract

The administrative functions are:

| Function | Required result |
|---|---|
| `exact_contract_manifest()` | Exact ordered 31-tool descriptor contract and all hashes |
| `migration_dry_run(...)` | `status=ready`, `can_apply=true`, and no filesystem side effects |
| `create_v1_backup(...)` | Immutable byte-identical backup plus `.manifest.json` evidence |
| `validate_backup_checksum(...)` | `valid=true` for source, backup, evidence, and contract |
| `migration_apply(...)` | Typed import, unchanged source, zero active legacy commands, zero V2 mutations |
| `migration_status(...)` | `status=applied_ready_for_cutover` before public switch |
| `v2_store_doctor(...)` | `ready=true`, exact schema, WAL, integrity, and classification |
| `rollback_eligibility(...)` | `eligible=true` only before the first V2 domain mutation |

Every call should pass the approved hashes explicitly during a real rollout,
even though the current constants are defaults:

```python
from patchbay.hub.cli_v2 import exact_contract_manifest

contract = exact_contract_manifest()
EXPECTED_CONTRACT_HASH = contract["contract_hash"]
EXPECTED_MANIFEST_HASH = contract["manifest_hash"]
EXPECTED_SCHEMA_HASH = contract["schema_hash"]
```

## Stage 0: Freeze Authority And Evidence

1. Record the deployed V1 Hub/Edge revisions and runtime state paths.
2. Dump `exact_contract_manifest()` to private rollout evidence.
3. Verify `tool_count == 31` and exact ordered `tool_names`.
4. Verify the five V1-only manager tools are absent from that list.
5. Record the approved contract, manifest, and schema hashes.
6. Confirm all 31 handlers and live acceptance evidence exist. A valid manifest
   alone does not authorize public V2 exposure.
7. Keep the public endpoint and catalog on V1.

Stop here on any contract or evidence mismatch.

## Stage 1: Quiesce V1

1. Stop the V1 Hub process.
2. Stop every V1 Edge polling process.
3. Confirm no process can write the V1 JSON after this point.
4. Inspect every V1 command. Resolve, cancel, or manually recover every
   `queued` or `running` command under V1.
5. Do not edit command states directly to pass this gate.

The dry-run independently rejects active legacy commands. A rejected dry-run
means V1 must remain the authoritative endpoint.

## Stage 2: Non-Persistent Dry Run

Run from the checked-out release candidate with explicit paths and hashes:

```python
from patchbay.hub.cli_v2 import migration_dry_run

report = migration_dry_run(
    V1_JSON_PATH,
    database_path=V2_DATABASE_PATH,
    expected_source_checksum=APPROVED_V1_CHECKSUM,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)
assert report["status"] in {"ready", "already_applied"}
assert report["can_apply"] is True
assert report["side_effects"] == {
    "source_mutated": False,
    "backup_created": False,
    "database_created": False,
}
```

For the first rehearsal, require `status=ready`. `already_applied` is acceptable
only when intentionally validating an idempotent repeat against the same source
checksum and same V2 database.

## Stage 3: Backup And Offline Import

Call apply once with an explicit operator-controlled backup path:

```python
from patchbay.hub.cli_v2 import migration_apply

applied = migration_apply(
    V1_JSON_PATH,
    database_path=V2_DATABASE_PATH,
    backup_path=V1_BACKUP_PATH,
    expected_source_checksum=APPROVED_V1_CHECKSUM,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)
assert applied["status"] == "applied"
assert applied["source_unchanged"] is True
assert applied["store"]["v2_mutation_count"] == 0
```

Apply performs these ordered actions:

1. rerun all dry-run gates;
2. create a checksum-named or explicit byte-identical V1 backup without
   overwriting an existing backup;
3. create and validate the backup evidence manifest;
4. import the validated backup into SQLite as typed legacy records;
5. never replay V1 commands as V2 operations or attempts;
6. verify import counts and foreign keys transactionally;
7. rerun source checksum, size, and mtime checks;
8. require `v2_mutation_count == 0`.

Apply is repeatable for the same checksum. A different snapshot, an existing
different import, or any active legacy command fails closed.

## Stage 4: Independent Pre-Cutover Validation

Run all three checks from a fresh process:

```python
from patchbay.hub.cli_v2 import (
    migration_status,
    rollback_eligibility,
    v2_store_doctor,
    validate_backup_checksum,
)

backup = validate_backup_checksum(
    V1_JSON_PATH,
    backup_path=V1_BACKUP_PATH,
    expected_checksum=APPROVED_V1_CHECKSUM,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)
doctor = v2_store_doctor(
    V2_DATABASE_PATH,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)
status = migration_status(
    V1_JSON_PATH,
    database_path=V2_DATABASE_PATH,
    backup_path=V1_BACKUP_PATH,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)
rollback = rollback_eligibility(
    V1_JSON_PATH,
    database_path=V2_DATABASE_PATH,
    backup_path=V1_BACKUP_PATH,
    expected_contract_hash=EXPECTED_CONTRACT_HASH,
    expected_manifest_hash=EXPECTED_MANIFEST_HASH,
    expected_schema_hash=EXPECTED_SCHEMA_HASH,
)

assert backup["valid"] is True
assert doctor["ready"] is True
assert doctor["v2_mutation_count"] == 0
assert status["status"] == "applied_ready_for_cutover"
assert rollback["eligible"] is True
```

Also run the repository-required compile and test suites, migration tests,
failure injection, two-Edge lifecycle, restart recovery, and real ChatGPT
acceptance defined by the implementation verification plan.

## Stage 5: Install Compatible V2 Edges While Public V1 Remains Frozen

1. Install the accepted V2 release on every intended Edge.
2. Keep Edge polling disabled.
3. Verify each Edge advertises the approved protocol version, immutable Edge
   generation, ordered manifest hash, schema hash, complete contract hash, and
   action capability versions.
4. Reject mixed versions. Do not allow an old Edge to claim V2 work.
5. Run strict local workspace preflight on every intended projection.
6. Re-run Stage 4 after the final artifacts are installed.

## Stage 6: Atomic Public Cutover

Perform one controlled endpoint/catalog switch:

1. start the accepted V2 Hub against the validated V2 database;
2. start only compatible V2 Edges;
3. initialize MCP and verify the exact ordered 31-tool catalog and hashes;
4. verify `patchbay_fleet_status` and `patchbay_workspace_list` as the Hub
   initialization entry points;
5. verify old/incompatible Edges cannot preflight or claim;
6. switch the public endpoint/catalog from complete V1 to complete V2 once;
7. do not expose a partial V2 catalog, temporary V2 labels, or mixed handlers;
8. keep the first manager mutation blocked until the rollback decision below.

At this checkpoint, reads and health checks may run, but no mutating V2 tool may
be accepted yet. Re-run `rollback_eligibility()` immediately before authorizing
the first V2 mutation.

## Stage 7: First-Mutation Commit Point

The first successful V2 domain transaction increments the monotonic
`v2_mutation_count`. This is the irreversible V1 rollback boundary.

1. Record a final `rollback_eligibility()` result with `eligible=true` and
   `v2_mutation_count=0`.
2. Create a recoverable V2 SQLite backup using the deployment's tested SQLite
   backup procedure. Copying only the main `.sqlite3` file while WAL is active
   is not a valid backup.
3. Authorize one bounded V2 mutation.
4. Verify the mutation's semantic result and durable operation/event records.
5. Verify `v2_mutation_count >= 1`.
6. Verify `rollback_eligibility()` now returns `eligible=false` with reason
   `first_v2_mutation_already_recorded`.
7. Verify `migration_status()` reports `status=cutover_committed`.

After step 3, never restart V1 against the unchanged JSON as the fleet control
plane. V1 no longer contains V2 mutations and cannot be authoritative.

## Rollback A: Before The First V2 Mutation

This procedure is allowed only when `rollback_eligibility()` returns exactly:

```text
eligible=true
v2_mutation_count=0
rollback_mode=restart_unchanged_v1
```

Procedure:

1. block all mutating traffic;
2. stop V2 Hub and every V2 Edge;
3. rerun backup validation and rollback eligibility from a fresh process;
4. preserve the V2 database, WAL/SHM files, logs, and validation reports for
   investigation; do not delete or rewrite them;
5. verify the original V1 JSON still matches the approved checksum;
6. restart the accepted V1 Hub against that unchanged original JSON;
7. restart only compatible V1 Edges;
8. verify V1 fleet status and command processing before reopening the endpoint;
9. keep the public catalog entirely V1.

The backup is evidence and emergency recovery material. Do not copy it over an
already matching source merely to perform rollback.

## Rollback B: At Or After The First V2 Mutation

Rollback to V1 is forbidden when `v2_mutation_count >= 1`, even if the first
mutation appears trivial or failed at a higher layer.

Procedure:

1. block mutating traffic and stop V2 Hub/Edges;
2. preserve current SQLite, WAL/SHM, logs, operation receipts, and Edge outboxes;
3. recover through the tested V2 SQLite restore/reconciliation procedure;
4. verify doctor, operation reconciliation, Edge generation fences, and exact
   contract hashes before reopening V2;
5. if V2 fleet recovery is not possible, use a separate explicitly isolated
   single-machine endpoint; do not present stale V1 fleet state as V2 truth.

A source JSON backup cannot reverse V2 mutations. A reverse exporter does not
exist unless separately implemented and accepted with its own migration tests.

## Failure Evidence

For every stopped stage, retain:

- exact function input paths and expected hashes without credentials;
- structured dry-run/status/doctor/backup/rollback reports;
- source and backup checksums;
- V2 schema version and mutation count;
- process stop/start evidence;
- Edge compatibility matrix;
- exact tool manifest dump;
- the failed gate and the operator decision.

Do not weaken a failed gate, edit stored state manually, or delete evidence to
make the next stage pass.
