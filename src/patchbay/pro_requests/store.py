"""Compatibility-preserving Pro Request store with process-safe mutations.

The original implementation is retained in :mod:`store_legacy`. This subclass
serializes manifest read/check/write sequences across store instances and
processes, maintains a durable projection revision, and exposes the complete
stored response for dispatch while display reads remain bounded.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .store_legacy import *  # noqa: F401,F403 - preserve public module API
from . import store_legacy as _legacy

_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve(strict=False))
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


class ProRequestStore(_legacy.ProRequestStore):
    """Durable storage with cross-process compare/write exclusion."""

    def __init__(self, config: dict[str, Any]):
        from patchbay.repo_locks import RepoMutationLockManager
        self._mirror_locks = RepoMutationLockManager(config)
        super().__init__(config)
        self._process_lock_path = self.root / ".store.lock"
        self._revision_path = self.root / ".projection-revision"
        self._thread_lock = _thread_lock_for(self.root)
        self._guard_state = threading.local()
        with self._mutation_guard():
            if not self._revision_path.exists():
                self._write_revision(max(1, int(__import__("time").time_ns())))

    @property
    def projection_state_revision(self) -> int:
        try:
            return int(
                self._revision_path.read_text(encoding="ascii").strip() or "0"
            )
        except (FileNotFoundError, OSError, ValueError):
            return int(getattr(self, "_projection_revision", 0) or 0)

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        depth = int(getattr(self._guard_state, "depth", 0) or 0)
        if depth:
            self._guard_state.depth = depth + 1
            try:
                yield
            finally:
                self._guard_state.depth = depth
            return

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._thread_lock:
            with self._process_lock_path.open("a+b") as lock_handle:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                self._guard_state.depth = 1
                try:
                    yield
                finally:
                    self._guard_state.depth = 0
                    if fcntl is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _write_revision(self, revision: int) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".projection-revision.", suffix=".tmp", dir=self.root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{int(revision)}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._revision_path)
            try:
                directory_fd = os.open(
                    self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _bump_durable_revision(self) -> None:
        self._write_revision(self.projection_state_revision + 1)

    def _write_manifest(self, request_dir: Path, manifest: dict[str, Any]) -> None:
        if not int(getattr(self._guard_state, "depth", 0) or 0):
            with self._mutation_guard():
                return self._write_manifest(request_dir, manifest)
        super()._write_manifest(request_dir, manifest)
        self._bump_durable_revision()

    def dispatch_response_text(self, request_id: str) -> dict[str, Any]:
        """Read the complete already-bounded response for worker dispatch."""
        manifest, request_dir = self._load_request(request_id)
        path = request_dir / "response.md"
        if not path.exists():
            return {
                "request_id": manifest["id"],
                "exists": False,
                "response_markdown": "",
                "total_bytes": 0,
            }
        raw = path.read_bytes()
        if len(raw) > self._max_response_bytes():
            raise ValueError(
                "Stored Pro response exceeds the configured storage maximum; "
                "refusing partial dispatch"
            )
        return {
            "request_id": manifest["id"],
            "exists": True,
            "response_markdown": raw.decode("utf-8", errors="replace"),
            "total_bytes": len(raw),
        }

    def create_request(self, **kwargs):
        with self._mutation_guard():
            return super().create_request(**kwargs)

    def claim_request(self, **kwargs):
        with self._mutation_guard():
            return super().claim_request(**kwargs)

    def respond_request(self, **kwargs):
        with self._mutation_guard():
            self._last_mirror_effect = None
            result = super().respond_request(**kwargs)
            if result.get("accepted"):
                result["mirror_effect"] = self._last_mirror_effect or {"state": "disabled"}
                result["note"] = "Response stored without worker dispatch or implementation apply. The configured repository mirror effect is reported separately."
            return result

    def _write_mirror(self, repo_path, manifest, report_text, response_text):
        from patchbay.repo_locks import RepoMutationBusy
        from patchbay.security import redact_text
        if self.config.get("pro_requests", {}).get("mirror_enabled", True) is False:
            self._last_mirror_effect = {"state": "disabled"}
            return
        try:
            with self._mirror_locks.hold_sync(repo_path, operation="pro_request_mirror"):
                super()._write_mirror(repo_path, manifest, redact_text(report_text),
                                      redact_text(response_text) if response_text is not None else None)
            self._last_mirror_effect = {"state": "written", "path": manifest.get("mirror", {}).get("path")}
        except RepoMutationBusy:
            # Canonical private evidence is already durable; a busy optional
            # mirror cannot claim it edited the checkout or lose that evidence.
            self._last_mirror_effect = {"state": "deferred", "reason": "repo_busy"}

    def mark_dispatch_requested(self, **kwargs):
        with self._mutation_guard():
            return super().mark_dispatch_requested(**kwargs)

    def finish_dispatch(self, **kwargs):
        with self._mutation_guard():
            return super().finish_dispatch(**kwargs)

    def close_request(self, **kwargs):
        with self._mutation_guard():
            return super().close_request(**kwargs)
