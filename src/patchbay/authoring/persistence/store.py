"""Independent SQLite journal. Opening state never starts jobs or creates a new deployment."""
from __future__ import annotations

import json
import hashlib
import re
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
from ..util import atomic_write, canonical, fingerprint as digest_fingerprint, fsync_dir, safe_id, strict_json, utc_now
from ..authority import assert_task_scope, current_identity, current_request_id
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
        identity = current_identity()
        assert_task_scope(task_id or None, identity)
        stored_key = self.scoped_key(key, identity=identity) if isinstance(key, str) else key
        is_scoped_key = (not identity.legacy and isinstance(stored_key, str)
                         and re.fullmatch(r"principal-[0-9a-f]{24}:[0-9a-f]{64}", stored_key) is not None
                         and stored_key == key)
        require(isinstance(key, str) and 0 < len(key) <= 200 or is_scoped_key,
                "INVALID_REQUEST", "A bounded idempotency key is required")
        require(isinstance(key, str) and "\x00" not in key, "INVALID_REQUEST", "Invalid idempotency key")
        journal_fingerprint = self.scoped_fingerprint(fingerprint, identity=identity)
        payload = dict(payload)
        if not identity.legacy:
            payload.setdefault("_principal_id", identity.principal_id)
        request_id = current_request_id()
        if request_id is not None:
            payload.setdefault("_request_id", request_id)
        task_id = task_id or ""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE task_id=? AND kind=? AND idempotency_key=?", (task_id, kind, stored_key)).fetchone()
            if row:
                require(row["fingerprint"] == journal_fingerprint, "IDEMPOTENCY_CONFLICT", "Idempotency key was already used for different semantics")
                return dict(row), False
            now = utc_now()
            operation = {"id": "op_" + uuid.uuid4().hex, "task_id": task_id, "kind": kind, "idempotency_key": stored_key,
                         "fingerprint": journal_fingerprint, "revision": revision, "state": "queued", "phase": "accepted",
                         "payload": canonical(payload).decode(), "result": None, "error": None, "owner_identity": None,
                         "cancel_requested": 0, "created": now, "updated": now}
            columns = list(operation)
            conn.execute("INSERT INTO operations (" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", tuple(operation.values()))
            if initialize:
                initialize(conn, operation)
            self.event(conn, "operation.accepted", task_id, {"operation_id": operation["id"], "kind": kind})
            return operation, True

    @staticmethod
    def scoped_key(key: str, *, identity=None) -> str:
        """Map a public logical key to a principal-local durable journal key."""
        identity = identity or current_identity()
        if identity.legacy:
            return key
        scope = hashlib.sha256(identity.principal_id.encode("utf-8")).hexdigest()[:24]
        prefix = "principal-" + scope + ":"
        if isinstance(key, str) and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", key):
            return key
        return prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def scoped_fingerprint(value: str, *, identity=None) -> str:
        """Bind an operation's caller fingerprint to its authenticated principal."""
        identity = identity or current_identity()
        if identity.legacy:
            return value
        return digest_fingerprint({"principal_id": identity.principal_id, "request": value})

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
        row = self.one("SELECT * FROM operations WHERE id=?", (operation_id,))
        assert_task_scope(row["task_id"] or None)
        return self.public_operation(row)

    @staticmethod
    def operation_summary_columns() -> str:
        """Keep large history values inside SQLite instead of loading each page."""
        fields = "id,task_id,kind,revision,state,phase,created,updated,cancel_requested"
        payload = "json_object('_request_id',json_extract(payload,'$._request_id'),'_admission',json_extract(payload,'$._admission')) AS payload"
        parts = [fields, payload]
        identities = ("id", "task_id", "revision", "operation_id", "request_operation_id", "commit", "tree",
                      "classification", "receipt_sha", "bundle_sha", "blob_sha", "context_id", "code", "state")
        for field in ("result", "error"):
            size = "length(CAST(" + field + " AS BLOB))"
            values = []
            for key in identities:
                kind = "json_type(" + field + ",'$." + key + "')"
                value = "json_extract(" + field + ",'$." + key + "')"
                values.append("'" + key + "',CASE WHEN " + kind + "='text' THEN substr(" + value + ",1,1000) WHEN " + kind + " IN ('integer','real','true','false') THEN " + value + " ELSE NULL END")
            projected = "json_object(" + ",".join(values) + ")"
            parts.extend(["CASE WHEN " + size + ">8192 THEN " + projected + " ELSE " + field + " END AS " + field,
                          size + " AS _" + field + "_bytes", size + ">8192 AS _" + field + "_partial"])
        return ",".join(parts)

    @staticmethod
    def public_operation(row: dict, *, summary: bool = False) -> dict:
        keys = ("id", "task_id", "kind", "revision", "state", "phase", "created", "updated", "cancel_requested")
        result = {key: row.get(key) for key in keys}
        try:
            payload = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row.get("payload")
            request_id = payload.get("_request_id") if isinstance(payload, dict) else None
            if isinstance(request_id, str):
                result["request_id"] = request_id
            if isinstance(payload, dict) and isinstance(payload.get("_admission"), dict) and row.get("state") == "queued":
                result["admission"] = {key: value for key, value in payload["_admission"].items() if key != "identity"}
        except (ValueError, TypeError):
            pass
        for key in ("result", "error"):
            result[key] = json.loads(row[key]) if isinstance(row.get(key), str) else row.get(key)
        def public(value):
            if isinstance(value, dict):
                return {k: public(v) for k, v in value.items()
                        if k not in {"token", "lease_token", "instance_id", "authorization"} and not k.startswith("_private")}
            if isinstance(value, list):
                return [public(item) for item in value]
            return value
        result = public(result)
        if summary:
            for field in ("result", "error"):
                value = result.get(field)
                if row.get("_" + field + "_partial"):
                    result[field + "_partial"] = True
                    result[field + "_bytes"] = row["_" + field + "_bytes"]
                    result["detail_read"] = {"tool": "author_operation_status", "arguments": {"operation_id": row["id"]}}
                    continue
                encoded = canonical(value)
                if len(encoded) > 8192:
                    result[field] = {key: item for key, item in value.items()
                        if key in {"id", "task_id", "revision", "operation_id", "request_operation_id", "commit", "tree",
                                   "classification", "receipt_sha", "bundle_sha", "blob_sha", "context_id", "code", "state"}
                        and isinstance(item, (str, int, float, bool, type(None)))
                        and len(canonical(item)) <= 1000} if isinstance(value, dict) else {}
                    result[field + "_partial"] = True
                    result[field + "_bytes"] = len(encoded)
                    result["detail_read"] = {"tool": "author_operation_status", "arguments": {"operation_id": row["id"]}}
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

    def source_integrity(self) -> dict:
        """Explicit maintenance-only SQL/Git join, including historical custody.

        Batch unique commit metadata once per mirror. Private refs and their
        ancestors prove retention, separately from mere object existence.
        No checkout, candidate code, network fetch or ordinary polling scan.
        """
        import re
        from ..repositories import Repositories
        references = []
        tasks = {r["id"]: r for r in self.rows("SELECT * FROM tasks LIMIT 100001")}
        require(len(tasks) <= 100000, "RESOURCE_LIMIT", "Source relation inventory exceeds its bound")
        def add(repository, owner, commit, tree=None):
            require(len(references) < 100000, "RESOURCE_LIMIT", "Source relation inventory exceeds its bound")
            references.append({"repository_id": repository, "record": owner, "commit": commit, "tree": tree})
        for task in tasks.values():
            current = self.one("SELECT commit_oid,tree_oid FROM revisions WHERE task_id=? AND number=?",
                               (task["id"], task["current_revision"]))
            require(current["commit_oid"] == task["current_commit"], "SOURCE_STATE_INCONSISTENT",
                    "Current task and revision disagree", task_id=task["id"])
            add(task["repository_id"], "task:" + task["id"], task["current_commit"], current["tree_oid"])
            add(task["repository_id"], "origin:" + task["id"], task["origin_commit"])
        for table, owner in (("revisions", "task_id"), ("contexts", "id"), ("drafts", "id"),
                             ("runs", "id"), ("approvals", "id"), ("publications", "id")):
            for row in self.rows("SELECT * FROM " + table + " LIMIT 100001"):
                task = tasks[row["task_id"]]
                record = table + ":" + str(row[owner]) + (":" + str(row["number"]) if table == "revisions" else "")
                add(task["repository_id"], record, row["commit_oid"], row.get("tree_oid"))
                if table == "revisions":
                    add(task["repository_id"], "base:" + row["task_id"] + ":" + str(row["number"]), row["base_commit"])
        for row in self.rows("SELECT v.*,d.task_id FROM draft_versions v JOIN drafts d ON v.draft_id=d.id LIMIT 100001"):
            add(tasks[row["task_id"]]["repository_id"], "draft_version:" + row["draft_id"] + ":" + str(row["version"]), row["commit_oid"], row["tree_oid"])
        pending = self.rows("SELECT id,task_id,payload FROM operations WHERE state NOT IN ('completed','failed','blocked','cancelled','timed_out') LIMIT 100001")
        require(len(pending) <= 100000, "RESOURCE_LIMIT", "Pending source intent inventory exceeds its bound")
        for row in pending:
            payload = json.loads(row["payload"])
            for field in ("commit", "base_commit", "resolved_base", "origin_commit"):
                if not payload.get(field):
                    continue
                task_id = row["task_id"] if row["task_id"] in tasks else payload.get("task_id")
                repository = tasks[task_id]["repository_id"] if task_id in tasks else payload.get("repository_id")
                require(repository, "SOURCE_STATE_INCONSISTENT", "Pending source intent lacks repository identity", operation_id=row["id"])
                add(repository, "pending:" + row["id"] + ":" + field, payload[field], payload.get("tree") if field == "commit" else None)
        unique = 0
        for repository in sorted({r["repository_id"] for r in references}):
            rows = [r for r in references if r["repository_id"] == repository]
            git = Repositories(self).git(repository)
            for row in rows:
                require(re.fullmatch("[0-9a-f]{" + str(git.oid_length) + "}", str(row["commit"]))
                        and (row["tree"] is None or re.fullmatch("[0-9a-f]{" + str(git.oid_length) + "}", str(row["tree"]))),
                        "SOURCE_STATE_INCONSISTENT", "Malformed SQL source identity", record=row["record"])
            commits = sorted({r["commit"] for r in rows}); unique += len(commits)
            try:
                metadata = git.run(["log", "--no-walk=unsorted", "--format=%H %T", "--stdin"],
                                   data=("\n".join(commits) + "\n").encode(), timeout=300)
                trees = dict(line.split() for line in metadata.decode("ascii").splitlines())
                anchors = git.run(["for-each-ref", "--count=100001", "--format=%(objectname)", "refs/author/"])
                require(anchors, "SOURCE_STATE_INCONSISTENT", "Source has no private retention anchor", repository_id=repository)
                require(len(anchors.splitlines()) <= 100000, "RESOURCE_LIMIT", "Private source anchors exceed the verification bound")
                reachable = git.run(["rev-list", "--max-count=500001", "--stdin"], data=anchors, timeout=300).decode("ascii").splitlines()
                require(len(reachable) <= 500000, "RESOURCE_LIMIT", "Private source ancestry exceeds the verification bound")
                retained = set(reachable)
            except AuthorError as exc:
                raise AuthorError("SOURCE_STATE_INCONSISTENT", "Managed source relation scan failed", repository_id=repository, cause=exc.code) from exc
            for row in rows:
                require(row["commit"] in trees and row["commit"] in retained
                        and (row["tree"] is None or trees[row["commit"]] == row["tree"]),
                        "SOURCE_STATE_INCONSISTENT", "SQL source is missing, mismatched, or not retained",
                        repository_id=repository, record=row["record"], commit=row["commit"])
        references.sort(key=lambda r: (r["repository_id"], r["record"], r["commit"], r["tree"] or ""))
        return {"version": 1, "references": len(references), "unique_commits": unique,
                "inventory_sha256": digest_fingerprint(references), "retention_basis": "private_refs_and_commit_ancestors"}
