"""Durable, completion-aware intake for recognized Author Mode packages.

Folder scanning is an explicit caller action. This module never starts a watcher,
unpacks into a user directory, or executes package content. All package semantics
continue through PackageReader and Materializer.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import tempfile
import itertools
from types import SimpleNamespace
from pathlib import Path

from ..errors import AuthorError, require
from ..authority import AuthIdentity, current_identity, identity_context
from ..persistence.storage import StorageBudget
from ..persistence.locks import file_lock
from ..persistence.blobs import Blobs
from ..revisions.leases import Leases
from ..revisions.materialize import Materializer
from ..tasks import Tasks
from ..util import atomic_write, canonical, fingerprint, safe_id, utc_now
from .archive import PackageReader

_IGNORED_SUFFIXES = (".part", ".crdownload", ".download", ".tmp")
_PACKAGE_SUFFIXES = (".zip", ".pbpkg")
_MAX_SCAN_FILES = 25
_MAX_RECORDED = 1000
_MAX_PENDING = 256
_MAX_FOLDER_ENTRIES = 10000
_MAX_TRANSIENT_ATTEMPTS = 3


class PackageInbox:
    """Recognize completed packages and route them through the shared core.

    The optional grant file is a private, narrowly scoped task capability. Its
    contents are never returned from this API or placed in public operation
    results/events. An expired or superseded lease is left pending; it is never
    renewed or taken over automatically.
    """

    def __init__(self, store, materializer: Materializer | None = None):
        self.store = store
        self.materializer = materializer or Materializer(store)
        self.tasks = Tasks(store)
        self.leases = Leases(store)
        operator_leases = store.home / "operator-leases"
        require(not operator_leases.is_symlink(), "CORRUPT_STATE", "Inbox grant parent cannot be a symlink")
        self.grants = operator_leases / "package-inbox"
        require(not self.grants.is_symlink(), "CORRUPT_STATE", "Inbox grant storage cannot be a symlink")
        require(not self.grants.exists() or stat.S_IMODE(self.grants.stat().st_mode) & 0o077 == 0, "CORRUPT_STATE",
                "Inbox grant storage must be private to its owner")

    def grant(self, task_id: str, token: str, epoch: int, *, auto_validate: bool, key: str) -> dict:
        """Grant bounded auto-validation for one task under an existing lease."""
        safe_id(task_id)
        require(type(auto_validate) is bool, "INVALID_REQUEST", "auto_validate must be boolean")
        self.leases.assert_owned(task_id, token, epoch)
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        request = {"task_id": task_id, "epoch": epoch, "token_sha256": token_digest,
                   "auto_validate": auto_validate}
        operation, created = self.store.begin_operation(
            kind="package.inbox.grant", task_id=task_id, key=key,
            fingerprint=fingerprint(request), payload={"auto_validate": auto_validate, "epoch": epoch})
        if not created and operation["state"] == "completed":
            return operation["result"] and json.loads(operation["result"]) or {
                "task_id": task_id, "auto_validate": auto_validate, "granted": True}
        self.leases.assert_owned(task_id, token, epoch)
        payload = {"task_id": task_id, "token": token, "epoch": epoch,
                   "auto_validate": auto_validate, "grant_key": key, "token_sha256": token_digest,
                   "principal_id": current_identity().principal_id, "legacy": current_identity().legacy}
        self.grants.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._grant_path(task_id)
        atomic_write(path, canonical(payload), mode=0o600)
        result = {"task_id": task_id, "auto_validate": auto_validate, "granted": True}
        self.store.finish(operation["id"], "completed", result=result)
        return result

    def revoke(self, task_id: str, *, key: str) -> dict:
        """Revoke the task's stored auto-validation capability."""
        safe_id(task_id)
        request = {"task_id": task_id, "revoke": True}
        operation, created = self.store.begin_operation(
            kind="package.inbox.revoke", task_id=task_id, key=key,
            fingerprint=fingerprint(request), payload=request)
        if not created and operation["state"] == "completed":
            return json.loads(operation["result"])
        self._grant_path(task_id).unlink(missing_ok=True)
        result = {"task_id": task_id, "granted": False}
        self.store.finish(operation["id"], "completed", result=result)
        return result

    def ingest(self, path: Path, *, key: str | None = None, confirm: bool = False,
               token: str | None = None, epoch: int | None = None) -> dict:
        with file_lock(self.store.home / "locks/maintenance", shared=True), file_lock(self.store.home / "locks/package-inbox"):
            count = self.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox'")["n"]
            pending = self.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox' AND state='awaiting_approval'")["n"]
            if count >= _MAX_RECORDED or pending >= _MAX_PENDING:
                return self._replay_at_capacity(Path(path), key)
            return self._ingest(path, key=key, confirm=confirm, token=token, epoch=epoch)

    def _replay_at_capacity(self, source: Path, key: str | None) -> dict:
        """Classify a replay in disposable storage; full inboxes pin no new bytes."""
        source = Path(source).expanduser().absolute()
        require(not source.is_symlink(), "RESOURCE_LIMIT", "Inbox capacity is full")
        with tempfile.TemporaryDirectory(prefix="inbox-replay-", dir=self.store.home / "staging") as temporary:
            temporary_store = SimpleNamespace(limits=self.store.limits, blobs=Blobs(Path(temporary) / "blobs"))
            parsed = None
            try:
                parsed = PackageReader(temporary_store).read(source)
                semantic, task_id, archive_sha = parsed.fingerprint, parsed.manifest.task_id, parsed.archive_sha
            except (AuthorError, OSError):
                require(False, "RESOURCE_LIMIT", "Inbox capacity is full; inspect existing entries before new intake")
            rows = self.store.rows("SELECT * FROM operations WHERE kind='package.inbox' ORDER BY created LIMIT ?", (_MAX_RECORDED,))
            self.tasks.get(task_id)
            expected = self.store.scoped_fingerprint(fingerprint({"semantic": semantic, "task_id": task_id}))
            for row in rows:
                if key is not None and row["idempotency_key"] == self.store.scoped_key(key):
                    require(row["fingerprint"] == expected, "IDEMPOTENCY_CONFLICT", "Inbox key has different package semantics")
                    return self.store.public_operation(row)
            for row in rows:
                if row["fingerprint"] == expected:
                    return self.store.public_operation(row)
        require(False, "RESOURCE_LIMIT", "Inbox capacity is full; no new record or content was retained")

    def _ingest(self, path: Path, *, key: str | None = None, confirm: bool = False,
                token: str | None = None, epoch: int | None = None) -> dict:
        """Accept one completed regular package file into owned content storage.

        ``confirm`` is an explicit user action and must carry a current task
        lease. Without it, only a matching unexpired stored grant can route the
        recognized package to Materializer; otherwise it remains pending.
        """
        source = Path(path).expanduser().absolute()
        try:
            require(not source.is_symlink(), "INVALID_PACKAGE", "Package input cannot be a symlink")
            require(source.suffix.lower() in _PACKAGE_SUFFIXES, "INVALID_PACKAGE", "Expected a .zip or .pbpkg package")
            archive_sha = self._copy_completed(source)
        except AuthorError as exc:
            return self._record_rejection(source, key, exc)
        parsed = None
        validation_error = None
        try:
            parsed = PackageReader(self.store).read(self.store.blobs.path(archive_sha))
        except AuthorError as exc:
            validation_error = exc

        task_id = parsed.manifest.task_id if parsed else ""
        semantic = parsed.fingerprint if parsed else archive_sha
        operation_key = key or "inbox-" + semantic
        require(len(operation_key) <= 200, "INVALID_REQUEST", "Inbox idempotency key is too long")
        # Semantic duplicates share the same intake record even when ZIP
        # timestamps/compression or non-semantic notes make the bytes differ.
        request_fp = fingerprint({"semantic": semantic, "task_id": task_id})
        operation, created = self.store.begin_operation(
            kind="package.inbox", task_id=task_id, key=operation_key, fingerprint=request_fp,
            payload={"archive_sha": archive_sha, "semantic_fingerprint": semantic,
                     "recognized_task_id": task_id, "source_name": source.name,
                     "phase": "validating"})
        if not created:
            return self.store.operation(operation["id"])
        with self.store.transaction() as conn:
            self.store.reference(conn, "package-inbox:" + operation["id"], archive_sha)

        try:
            if validation_error:
                self.store.finish(operation["id"], "failed", error=validation_error.as_dict())
                return self.store.operation(operation["id"])
            self.tasks.get(task_id)
            # Semantic recognition here checks task identity; Materializer owns
            # authoritative repository/context/revision/policy validation.
            auto = self._authorized_auto(task_id)
            if confirm:
                require(token is not None and epoch is not None, "LEASE_REQUIRED",
                        "Explicit package confirmation requires the task writer lease")
                self.leases.assert_owned(task_id, token, epoch)
                return self._process(operation, parsed, token, epoch)
            if auto:
                grant = self._read_grant(task_id)
                try:
                    # This is the previously granted task capability, not the
                    # scanner's ambient identity or a new controller decision.
                    owner = AuthIdentity(grant.get("principal_id", "local-operator"), "author",
                                         (task_id,), legacy=grant.get("legacy", True))
                    with identity_context(owner):
                        self.leases.assert_owned(task_id, grant["token"], grant["epoch"])
                        return self._process(operation, parsed, grant["token"], grant["epoch"])
                except AuthorError as exc:
                    if exc.code not in {"LEASE_LOST", "LEASE_REQUIRED"}:
                        raise
            with self.store.transaction() as conn:
                conn.execute("UPDATE operations SET state='awaiting_approval',phase='awaiting_approval',updated=? WHERE id=?",
                             (utc_now(), operation["id"]))
                self.store.event(conn, "package.inbox.awaiting_approval", task_id,
                                 {"operation_id": operation["id"], "task_id": task_id,
                                  "phase": "awaiting_approval"})
            return self.store.operation(operation["id"])
        except AuthorError as exc:
            if exc.code in {"LEASE_REQUIRED", "LEASE_LOST", "LEASE_CONFLICT"}:
                return self._await_approval(operation["id"], error=exc)
            self.store.finish(operation["id"], "failed", error=exc.as_dict())
            return self.store.operation(operation["id"])

    def approve(self, operation_id: str, *, token: str, epoch: int, key: str) -> dict:
        """Approve one pending package and submit it under the current lease."""
        operation = self.store.operation(operation_id)
        require(operation["kind"] == "package.inbox", "INVALID_REQUEST", "Not a package inbox operation")
        payload = self._payload(operation_id)
        task_id = payload["recognized_task_id"]
        require(task_id, "INVALID_REQUEST", "Package has no recognized task")
        self.leases.assert_owned(task_id, token, epoch)
        action = {"operation_id": operation_id, "task_id": task_id, "epoch": epoch}
        approval_fingerprint = fingerprint(action)
        previous = self.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.inbox.approve' AND idempotency_key=?",
                                  (task_id, self.store.scoped_key(key)), required=False)
        if previous:
            require(previous["fingerprint"] == self.store.scoped_fingerprint(approval_fingerprint), "IDEMPOTENCY_CONFLICT",
                    "Approval key was already used for different semantics")
            if previous["state"] == "completed":
                return json.loads(previous["result"])
        else:
            require(operation["state"] == "awaiting_approval", "INVALID_REQUEST", "Package is not awaiting approval")
        approval, created = self.store.begin_operation(
            kind="package.inbox.approve", task_id=task_id, key=key,
            fingerprint=approval_fingerprint, payload=action)
        if not created and approval["state"] == "completed":
            return json.loads(approval["result"])
        try:
            package = PackageReader(self.store).read(self.store.blobs.path(payload["archive_sha"]))
            result = self._process(operation, package, token, epoch)
            self.store.finish(approval["id"], "completed", result=result)
            return result
        except AuthorError as exc:
            if exc.code in {"LEASE_REQUIRED", "LEASE_LOST", "LEASE_CONFLICT"}:
                with self.store.transaction() as conn:
                    conn.execute("UPDATE operations SET state='awaiting_approval',phase='awaiting_approval',error=?,updated=? WHERE id=?",
                                 (canonical(exc.as_dict()).decode(), utc_now(), approval["id"]))
                    self.store.event(conn, "package.inbox.approval_waiting", task_id,
                                     {"operation_id": approval["id"], "inbox_operation_id": operation_id,
                                      "phase": "awaiting_approval"})
                return self._await_approval(operation_id, error=exc)
            self.store.finish(approval["id"], "failed", error=exc.as_dict())
            self.store.finish(operation_id, "failed", error=exc.as_dict())
            return self.store.operation(operation_id)

    def scan(self, folder: Path, *, stability_seconds: float = 1.0, max_files: int = _MAX_SCAN_FILES,
             offset: int = 0) -> dict:
        """One explicit, bounded scan of a user-selected folder; no daemon starts."""
        require(type(stability_seconds) in (int, float) and 0 <= stability_seconds <= 30,
                "INVALID_REQUEST", "stability_seconds must be between 0 and 30")
        require(type(max_files) is int and 1 <= max_files <= _MAX_SCAN_FILES,
                "INVALID_REQUEST", "max_files must be between 1 and 25")
        require(type(offset) is int and 0 <= offset <= _MAX_FOLDER_ENTRIES,
                "INVALID_REQUEST", "Invalid inbox scan offset")
        root = Path(folder).expanduser().absolute()
        require(not root.is_symlink() and root.is_dir(), "INVALID_FOLDER", "Selected inbox must be a real directory")
        entries = list(itertools.islice(root.iterdir(), _MAX_FOLDER_ENTRIES + 1))
        require(len(entries) <= _MAX_FOLDER_ENTRIES, "RESOURCE_LIMIT", "Selected inbox has too many entries")
        package_entries = [entry for entry in sorted(entries, key=lambda item: item.name)
                           if entry.suffix.lower() in _PACKAGE_SUFFIXES
                           and not entry.name.lower().endswith(_IGNORED_SUFFIXES)]
        eligible = []
        for entry in package_entries[offset:offset + max_files]:
            try:
                eligible.append((entry, self._regular_stat(entry)))
            except AuthorError as exc:
                eligible.append((entry, exc))
        if stability_seconds and any(not isinstance(first, AuthorError) for _, first in eligible):
            time.sleep(stability_seconds)
        candidates = []
        for entry, before in eligible:
            if isinstance(before, AuthorError):
                candidates.append({"name": entry.name, "error": before.as_dict()})
                continue
            try:
                after = self._regular_stat(entry)
                if self._stat_identity(before) != self._stat_identity(after):
                    candidates.append({"name": entry.name, "state": "in_progress"})
                else:
                    candidates.append({"name": entry.name, "operation": self.ingest(entry)})
            except AuthorError as exc:
                candidates.append({"name": entry.name, "error": exc.as_dict()})
        scanned = len(eligible)
        return {"folder": str(root), "items": candidates, "scanned": scanned,
                "more_available": len(package_entries) > offset + scanned,
                "next_offset": offset + scanned if len(package_entries) > offset + scanned else None,
                "directory_snapshot": False}

    def pending(self, *, limit: int = 50, offset: int = 0) -> dict:
        require(type(limit) is int and 1 <= limit <= 200 and type(offset) is int and offset >= 0,
                "INVALID_REQUEST", "Invalid inbox page")
        from ..authority import current_identity
        scope = current_identity().task_ids
        clause = " AND task_id IN (" + ",".join("?" for _ in scope) + ")" if scope is not None else ""
        rows = self.store.rows("SELECT id FROM operations WHERE kind='package.inbox'" + clause + " ORDER BY created DESC,id LIMIT ? OFFSET ?",
                               (*scope, limit + 1, offset) if scope is not None else (limit + 1, offset))
        ids = [row["id"] for row in rows[:limit]]
        return {"operations": [self.store.operation(operation_id) for operation_id in ids],
                "next_offset": offset + limit if len(rows) > limit else None}

    def _copy_completed(self, source: Path) -> str:
        descriptor = None
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            require(stat.S_ISREG(before.st_mode), "INVALID_PACKAGE", "Package input must be a regular file")
            require(before.st_size > 0 and before.st_size <= self.store.limits.archive.compressed_bytes,
                    "RESOURCE_LIMIT", "Package input is empty or exceeds its byte budget")
            StorageBudget(self.store).check(min(before.st_size * 4, self.store.limits.archive.expanded_bytes))
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                sha = self.store.blobs.put_stream(stream, limit=self.store.limits.archive.compressed_bytes)
            after = os.fstat(descriptor)
            require(self._stat_identity(before) == self._stat_identity(after), "PACKAGE_IN_PROGRESS",
                    "Package changed while it was being ingested")
            require(self.store.blobs.path(sha).stat().st_size == before.st_size,
                    "PACKAGE_IN_PROGRESS", "Package byte count changed while it was being ingested")
            return sha
        except OSError as exc:
            raise AuthorError("INVALID_PACKAGE", "Package input could not be opened safely", detail=str(exc)[:500]) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _record_rejection(self, source: Path, key: str | None, error: AuthorError) -> dict:
        try:
            info = source.lstat()
            identity = {"name": source.name[:255], "device": info.st_dev, "inode": info.st_ino,
                        "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                        "code": error.code}
        except OSError:
            identity = {"name": source.name[:255], "code": error.code}
        operation_key = key or "reject-" + fingerprint(identity)
        payload = {"source_name": source.name[:255], "recognized_task_id": "",
                   "phase": "rejected_before_capture"}
        operation, created = self.store.begin_operation(
            kind="package.inbox", task_id="", key=operation_key,
            fingerprint=fingerprint({"rejection": identity}), payload=payload)
        if error.code == "PACKAGE_IN_PROGRESS":
            prior_payload = json.loads(operation["payload"])
            if not created and prior_payload.get("transient_attempts", 0) >= _MAX_TRANSIENT_ATTEMPTS:
                return self.store.operation(operation["id"])
            attempts = prior_payload.get("transient_attempts", 0) + 1
            state = "retryable" if attempts < _MAX_TRANSIENT_ATTEMPTS else "failed"
            prior_payload.update(transient_attempts=attempts,
                                 phase="retryable" if state == "retryable" else "retry_exhausted")
            with self.store.transaction() as conn:
                conn.execute("UPDATE operations SET state=?,phase=?,payload=?,error=?,updated=? WHERE id=?",
                             (state, prior_payload["phase"], canonical(prior_payload).decode(),
                              canonical(error.as_dict()).decode(), utc_now(), operation["id"]))
                self.store.event(conn, "package.inbox.retry_state", operation["task_id"],
                                 {"operation_id": operation["id"], "state": state,
                                  "phase": prior_payload["phase"], "attempt": attempts})
            return self.store.operation(operation["id"])
        if created:
            self.store.finish(operation["id"], "failed", error=error.as_dict())
        return self.store.operation(operation["id"])

    @staticmethod
    def _regular_stat(path: Path):
        require(not path.is_symlink(), "INVALID_PACKAGE", "Inbox entries cannot be symlinks")
        info = path.stat(follow_symlinks=False)
        require(stat.S_ISREG(info.st_mode), "INVALID_PACKAGE", "Inbox entry must be a regular file")
        return info

    @staticmethod
    def _stat_identity(info) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _process(self, operation: dict, package, token: str, epoch: int) -> dict:
        result = self.materializer.submit_imported(package.manifest.task_id, package,
                                                   key="inbox-" + package.fingerprint,
                                                   token=token, epoch=epoch)
        self.store.finish(operation["id"], "completed", result=result)
        return self.store.operation(operation["id"])

    def _await_approval(self, operation_id: str, *, error: AuthorError | None = None) -> dict:
        with self.store.transaction() as conn:
            conn.execute("UPDATE operations SET state='awaiting_approval',phase='awaiting_approval',error=?,updated=? WHERE id=?",
                         (canonical(error.as_dict()).decode() if error else None, utc_now(), operation_id))
            row = conn.execute("SELECT task_id FROM operations WHERE id=?", (operation_id,)).fetchone()
            self.store.event(conn, "package.inbox.awaiting_approval", row["task_id"], {"operation_id": operation_id})
        return self.store.operation(operation_id)

    def _authorized_auto(self, task_id: str) -> bool:
        grant = self._read_grant(task_id, required=False)
        return bool(grant and grant.get("auto_validate"))

    def _read_grant(self, task_id: str, *, required: bool = True) -> dict | None:
        path = self._grant_path(task_id)
        require(not path.is_symlink(), "CORRUPT_STATE", "Inbox grant cannot be a symlink")
        if not path.exists():
            require(not required, "LEASE_REQUIRED", "Task has no stored inbox grant")
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o077 == 0
                        and info.st_size <= 4096, "CORRUPT_STATE",
                        "Stored inbox grant is not a bounded regular file")
                value = json.loads(stream.read(4097).decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise AuthorError("CORRUPT_STATE", "Stored inbox grant cannot be read") from exc
        require(value.get("task_id") == task_id, "CORRUPT_STATE", "Stored inbox grant identity mismatch")
        return value

    def _grant_path(self, task_id: str) -> Path:
        safe_id(task_id)
        return self.grants / (task_id + ".json")

    def _payload(self, operation_id: str) -> dict:
        row = self.store.one("SELECT payload FROM operations WHERE id=?", (operation_id,))
        return json.loads(row["payload"])

    def _enforce_capacity(self) -> None:
        count = self.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox'")["n"]
        pending = self.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox' AND state='awaiting_approval'")["n"]
        require(count <= _MAX_RECORDED, "RESOURCE_LIMIT", "Inbox record capacity is full; inspect or export existing records")
        require(pending < _MAX_PENDING, "RESOURCE_LIMIT", "Too many packages are awaiting approval")
