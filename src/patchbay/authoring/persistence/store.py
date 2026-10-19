"""Independent SQLite journal. Opening state never starts jobs or creates a new deployment."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..errors import AuthorError, require
from ..models import RuntimeLimits
from ..util import atomic_write, canonical, fsync_dir, safe_id, strict_json, utc_now
from .blobs import Blobs
from .schema import SCHEMA, SCHEMA_VERSION

TERMINAL = frozenset({"completed", "failed", "blocked", "timed_out", "cancelled", "unknown_execution", "publication_unknown"})


class Store:
    def __init__(self, home: Path, *, expected_instance: str | None = None):
        candidate = Path(home).expanduser().absolute()
        require(not candidate.is_symlink(), "CORRUPT_STATE", "Runtime root cannot be a symlink")
        self.home = candidate.resolve()
        identity_path, database = self.home / "identity.json", self.home / "author.sqlite3"
        require(identity_path.is_file() and database.is_file() and not database.is_symlink(),
                "STATE_UNAVAILABLE", "Author state is missing; initialize explicitly or restore the existing deployment")
        self.identity = strict_json(identity_path.read_bytes())
        require(self.identity.get("schema_version") == SCHEMA_VERSION, "INCOMPATIBLE_STATE", "Unsupported author-state schema")
        require(expected_instance is None or self.identity.get("instance_id") == expected_instance,
                "INSTANCE_MISMATCH", "This is not the expected author deployment")
        self.limits = RuntimeLimits.model_validate(self.identity["limits"])
        self.database = database
        self.blobs = Blobs(self.home / "blobs")
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='instance_id'").fetchone()
            version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            require(row and row[0] == self.identity["instance_id"] and version and int(version[0]) == SCHEMA_VERSION,
                    "CORRUPT_STATE", "Runtime identity and database do not match")

    @classmethod
    def initialize(cls, home: Path, limits: RuntimeLimits | None = None):
        home = Path(home).expanduser().absolute()
        require(not home.exists() and not home.is_symlink(), "ALREADY_INITIALIZED", "Initialization needs a new runtime directory")
        home.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".author-init-", dir=home.parent))
        try:
            temporary.chmod(0o700)
            for directory in ("blobs", "repositories", "locks", "uploads", "staging", "runs", "operator-leases"):
                (temporary / directory).mkdir(mode=0o700)
            identity = {"instance_id": "instance_" + uuid.uuid4().hex, "schema_version": SCHEMA_VERSION,
                        "limits": (limits or RuntimeLimits()).wire(), "created": utc_now()}
            database = temporary / "author.sqlite3"
            conn = sqlite3.connect(database)
            try:
                conn.executescript(SCHEMA)
                conn.executemany("INSERT INTO metadata VALUES(?,?)", [("instance_id", identity["instance_id"]), ("schema_version", str(SCHEMA_VERSION))])
                conn.commit()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            database.chmod(0o600)
            atomic_write(temporary / "identity.json", canonical(identity))
            atomic_write(temporary / "tokens.json", canonical({role: secrets.token_urlsafe(48) for role in ("operator", "author", "reader")}))
            os.rename(temporary, home)
            fsync_dir(home.parent)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return cls(home)

    @contextmanager
    def connect(self):
        try:
            conn = sqlite3.connect(self.database.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            raise AuthorError("STATE_UNAVAILABLE", "Author database cannot be opened") from exc
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=(), *, required: bool = True) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
        require(row is not None or not required, "NOT_FOUND", "Requested author record does not exist", status=404)
        return dict(row) if row is not None else None

    def begin_operation(self, *, kind: str, key: str, fingerprint: str, payload: dict,
                        task_id: str | None = None, revision: int | None = None, initialize=None) -> tuple[dict, bool]:
        require(isinstance(key, str) and 0 < len(key) <= 200 and "\x00" not in key,
                "INVALID_REQUEST", "A bounded idempotency key is required")
        task_id = task_id or ""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE task_id=? AND kind=? AND idempotency_key=?", (task_id, kind, key)).fetchone()
            if row:
                require(row["fingerprint"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Idempotency key was already used for different semantics")
                return dict(row), False
            now = utc_now()
            operation = {"id": "op_" + uuid.uuid4().hex, "task_id": task_id, "kind": kind, "idempotency_key": key,
                         "fingerprint": fingerprint, "revision": revision, "state": "queued", "phase": "accepted",
                         "payload": canonical(payload).decode(), "result": None, "error": None, "owner_identity": None,
                         "cancel_requested": 0, "created": now, "updated": now}
            columns = list(operation)
            conn.execute("INSERT INTO operations (" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(operation.values()))
            if initialize:
                initialize(conn, operation)
            self.event(conn, "operation.accepted", task_id, {"operation_id": operation["id"], "kind": kind})
            return operation, True

    def finish(self, operation_id: str, state: str, *, result=None, error=None) -> dict:
        require(state in TERMINAL, "INVALID_REQUEST", "Expected a terminal operation state")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            require(row is not None, "NOT_FOUND", "Operation is unavailable")
            if row["state"] in TERMINAL and row["state"] not in {"unknown_execution", "publication_unknown"}:
                return self.public_operation(dict(row))
            conn.execute("UPDATE operations SET state=?,phase='terminal',result=?,error=?,updated=? WHERE id=?",
                         (state, canonical(result).decode() if result is not None else None,
                          canonical(error).decode() if error is not None else None, utc_now(), operation_id))
            self.event(conn, "operation." + state, row["task_id"], {"operation_id": operation_id})
        return self.operation(operation_id)

    def operation(self, operation_id: str) -> dict:
        safe_id(operation_id)
        return self.public_operation(self.one("SELECT * FROM operations WHERE id=?", (operation_id,)))

    @staticmethod
    def public_operation(row: dict) -> dict:
        keys = ("id", "task_id", "kind", "revision", "state", "phase", "created", "updated", "cancel_requested")
        result = {key: row.get(key) for key in keys}
        for key in ("result", "error"):
            result[key] = json.loads(row[key]) if isinstance(row.get(key), str) else row.get(key)
        return result

    def reference(self, conn, owner: str, *hashes: str) -> None:
        for sha in hashes:
            require(self.blobs.path(sha).is_file(), "CORRUPT_STATE", "Cannot reference missing content", sha256=sha)
            conn.execute("INSERT OR IGNORE INTO blob_refs VALUES(?,?)", (owner, sha))

    def event(self, conn, event: str, task_id: str, payload: dict) -> None:
        encoded = canonical(payload)
        require(len(encoded) <= 262144, "RESOURCE_LIMIT", "Event payload must remain bounded")
        conn.execute("INSERT INTO events(event,task_id,payload,created) VALUES(?,?,?,?)", (event, task_id or "", encoded.decode(), utc_now()))
        conn.execute("DELETE FROM events WHERE seq <= (SELECT MAX(seq) - ? FROM events)", (self.limits.max_events,))

    def integrity(self) -> dict:
        with self.connect() as conn:
            require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "CORRUPT_STATE", "SQLite integrity check failed")
            require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "CORRUPT_STATE", "Database references are inconsistent")
            hashes = [row[0] for row in conn.execute("SELECT DISTINCT sha FROM blob_refs")]
        for sha in hashes:
            require(self.blobs.verify(sha), "CORRUPT_STATE", "Referenced bytes failed verification", sha256=sha)
        return {"database": "ok", "referenced_blobs": len(hashes), "instance_id": self.identity["instance_id"]}
