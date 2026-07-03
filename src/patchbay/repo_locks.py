"""Per-repository mutation locks for shared local MCP server state."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Mapping

try:  # pragma: no cover - fcntl is expected on supported Unix-like hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from patchbay.connector.profiles import resolve_runtime_path


REPO_LOCK_OPTION = "_repo_mutation_lock"
REPO_LOCK_OPERATION_OPTION = "_repo_mutation_lock_operation"


class RepoMutationBusy(RuntimeError):
    """Raised when a base-checkout mutation is already in progress."""

    def __init__(self, repo_path: str, operation: str):
        self.repo_path = str(Path(repo_path).expanduser().resolve())
        self.operation = operation
        super().__init__("Repository checkout is busy with another mutation")

    def public_payload(self) -> dict[str, Any]:
        return {
            "repo_busy": True,
            "operation": self.operation,
            "workspace_name": Path(self.repo_path).name or "workspace",
            "required_action": "retry after the current base-checkout mutation finishes",
            "note": (
                "Another tool call is currently mutating this repository checkout. "
                "PatchBay does not queue base-checkout writes; retry after inspecting current status."
            ),
        }


@dataclass
class RepoMutationLease:
    repo_path: str
    operation: str
    lock: asyncio.Lock
    file_handle: BinaryIO | None = None
    acquired_at: float = 0.0
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.file_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.file_handle.close()
        if self.lock.locked():
            self.lock.release()


class RepoMutationLockManager:
    """Fast-fail lock manager keyed by normalized repository path."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self._locks: dict[str, asyncio.Lock] = {}
        self._job_leases: dict[str, RepoMutationLease] = {}
        configured = (config.get("locks") or {}).get("root") if isinstance(config.get("locks"), dict) else None
        self.lock_dir = resolve_runtime_path(configured, "locks")
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def acquire(self, repo_path: str, *, operation: str) -> RepoMutationLease:
        normalized = str(Path(repo_path).expanduser().resolve())
        lock = self._locks.setdefault(normalized, asyncio.Lock())
        if lock.locked():
            raise RepoMutationBusy(normalized, operation)
        await lock.acquire()

        file_handle: BinaryIO | None = None
        try:
            file_handle = self._open_lock_file(normalized)
            if file_handle is not None and fcntl is not None:
                try:
                    fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RepoMutationBusy(normalized, operation) from error
            return RepoMutationLease(
                repo_path=normalized,
                operation=operation,
                lock=lock,
                file_handle=file_handle,
                acquired_at=time.time(),
            )
        except Exception:
            if file_handle is not None:
                file_handle.close()
            if lock.locked():
                lock.release()
            raise

    @contextlib.asynccontextmanager
    async def hold(self, repo_path: str, *, operation: str) -> AsyncIterator[RepoMutationLease]:
        lease = await self.acquire(repo_path, operation=operation)
        try:
            yield lease
        finally:
            lease.release()

    def bind_to_job(self, job_id: str, lease: RepoMutationLease) -> None:
        self._job_leases[str(job_id)] = lease

    def release_job(self, job_id: str) -> None:
        lease = self._job_leases.pop(str(job_id), None)
        if lease is not None:
            lease.release()

    def _open_lock_file(self, repo_path: str) -> BinaryIO:
        digest = hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:32]
        path = self.lock_dir / f"repo_{digest}.lock"
        return path.open("a+b")


def job_requires_repo_mutation_lock(
    mode: str,
    options: Mapping[str, Any] | None,
    *,
    default_sandbox: str = "read-only",
) -> bool:
    """Return whether a Codex job can mutate the base checkout directly."""
    options = options or {}
    worker_workspace_mode = str(options.get("_worker_workspace_mode") or "").strip().lower()
    if worker_workspace_mode:
        return worker_workspace_mode == "shared_write"
    if options.get("_worker_worktree_path"):
        return False
    if mode == "apply":
        return False
    if options.get("dangerously_bypass"):
        return True
    sandbox = str(options.get("sandbox") or default_sandbox or "read-only").strip().lower()
    return sandbox not in {"", "read-only", "readonly"}


def mark_repo_lock_options(options: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    marked = dict(options)
    marked[REPO_LOCK_OPTION] = True
    marked[REPO_LOCK_OPERATION_OPTION] = operation
    return marked
