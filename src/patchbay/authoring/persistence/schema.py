"""Schema 1. Positional revision/run columns are a versioned internal contract."""
SCHEMA_VERSION = 1
STATE_CLOCK_MARKER = "state_clock:installed"
STATE_CLOCK_GLOBAL = "state_clock:global"
STATE_CLOCK_PREFIX = "state_clock:task:"
STATE_CLOCK_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS operations_state_clock_insert AFTER INSERT ON operations BEGIN
        INSERT INTO metadata(key,value) VALUES('state_clock:global','1')
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
        INSERT INTO metadata(key,value)
        SELECT 'state_clock:task:'||NEW.task_id,'1' WHERE NEW.task_id<>''
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
    END""",
    """CREATE TRIGGER IF NOT EXISTS operations_state_clock_update AFTER UPDATE ON operations BEGIN
        INSERT INTO metadata(key,value) VALUES('state_clock:global','1')
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
        INSERT INTO metadata(key,value)
        SELECT 'state_clock:task:'||OLD.task_id,'1' WHERE OLD.task_id<>''
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
        INSERT INTO metadata(key,value)
        SELECT 'state_clock:task:'||NEW.task_id,'1' WHERE NEW.task_id<>'' AND NEW.task_id<>OLD.task_id
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
    END""",
    """CREATE TRIGGER IF NOT EXISTS operations_state_clock_delete AFTER DELETE ON operations BEGIN
        INSERT INTO metadata(key,value) VALUES('state_clock:global','1')
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
        INSERT INTO metadata(key,value)
        SELECT 'state_clock:task:'||OLD.task_id,'1' WHERE OLD.task_id<>''
        ON CONFLICT(key) DO UPDATE SET value=CAST(metadata.value AS INTEGER)+1;
    END""",
)

ADMISSION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS runs_queue_window ON runs(state,created,id)",
    "CREATE INDEX IF NOT EXISTS runs_profile_receipts ON runs(profile_digest,updated DESC) WHERE receipt_sha IS NOT NULL",
)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO metadata(key,value) VALUES('state_clock:installed','1');
INSERT INTO metadata(key,value) VALUES('state_clock:global','0');
CREATE TABLE repositories(
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
 remote TEXT, default_branch TEXT NOT NULL, object_format TEXT NOT NULL,
 policy TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
 goal TEXT NOT NULL, origin_commit TEXT NOT NULL, current_commit TEXT NOT NULL,
 current_revision INTEGER NOT NULL, policy TEXT NOT NULL, policy_digest TEXT NOT NULL,
 status TEXT NOT NULL, dependencies TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE revisions(
 task_id TEXT NOT NULL REFERENCES tasks(id), number INTEGER NOT NULL,
 parent_number INTEGER, base_commit TEXT NOT NULL, commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL,
 fingerprint TEXT, archive_sha TEXT, context_id TEXT, created REAL NOT NULL,
 PRIMARY KEY(task_id,number), UNIQUE(task_id,fingerprint));
CREATE TABLE contexts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
 commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL, bundle_sha TEXT NOT NULL,
 inventory_sha TEXT NOT NULL, selection TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE packages(fingerprint TEXT PRIMARY KEY, archive_sha TEXT NOT NULL, manifest TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE operations(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL, revision INTEGER,
 state TEXT NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT,
 owner_identity TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(task_id,kind,idempotency_key));
CREATE TABLE runs(
 id TEXT PRIMARY KEY REFERENCES operations(id), task_id TEXT NOT NULL REFERENCES tasks(id),
 revision INTEGER NOT NULL, commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL,
 profile TEXT NOT NULL, profile_digest TEXT NOT NULL, receipt_sha TEXT,
 state TEXT NOT NULL, cleanup TEXT NOT NULL, environment_digest TEXT,
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE leases(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), token_hash TEXT NOT NULL,
 label TEXT NOT NULL, epoch INTEGER NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE approvals(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
 commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL, policy_digest TEXT NOT NULL,
 receipt_sha TEXT NOT NULL, actor TEXT NOT NULL, remote_head TEXT, target_head TEXT,
 revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE publications(
 id TEXT PRIMARY KEY REFERENCES operations(id), approval_id TEXT NOT NULL REFERENCES approvals(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), commit_oid TEXT NOT NULL,
 branch TEXT NOT NULL, remote TEXT NOT NULL, expected_head TEXT,
 state TEXT NOT NULL, pull_request TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE drafts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), base_revision INTEGER NOT NULL,
 version INTEGER NOT NULL, tree_oid TEXT NOT NULL, commit_oid TEXT NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE draft_versions(
 draft_id TEXT NOT NULL REFERENCES drafts(id), version INTEGER NOT NULL,
 commit_oid TEXT NOT NULL, tree_oid TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(draft_id,version));
CREATE TABLE blob_refs(owner TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY(owner,sha));
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, task_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX operations_state ON operations(state,created);
CREATE INDEX runs_task_revision ON runs(task_id,revision,created);
CREATE INDEX tasks_updated ON tasks(updated);
CREATE INDEX contexts_task ON contexts(task_id,revision);
""" + ";\n".join((*STATE_CLOCK_TRIGGERS, *ADMISSION_INDEXES))
