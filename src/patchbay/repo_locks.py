"""Per-repository mutation locks for shared local MCP server state."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
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
ALLOW_CONCURRENT_SHARED_WRITE_OPTION = "_allow_concurrent_shared_write"


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
        self._cleanup_job_repos: dict[str, str] = {}
        self._cleanup_block_jobs: dict[str, set[str]] = {}
        self._cleanup_block_handles: dict[str, BinaryIO] = {}
        self._cleanup_turnstile_handles: dict[str, BinaryIO] = {}
        self._cleanup_stop_events: dict[str, threading.Event] = {}
        self._cleanup_started_events: dict[str, threading.Event] = {}
        self._cleanup_acquired_events: dict[str, threading.Event] = {}
        self._cleanup_waiter_threads: dict[str, threading.Thread] = {}
        self._cleanup_threads: set[threading.Thread] = set()
        self._cleanup_guard = threading.RLock()
        configured = (config.get("locks") or {}).get("root") if isinstance(config.get("locks"), dict) else None
        self.lock_dir = resolve_runtime_path(configured, "locks")
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def acquire(self, repo_path: str, *, operation: str) -> RepoMutationLease:
        normalized = str(Path(repo_path).expanduser().resolve())
        with self._cleanup_guard:
            if self._cleanup_block_jobs.get(normalized):
                raise RepoMutationBusy(normalized, operation)
        lock = self._locks.setdefault(normalized, asyncio.Lock())
        if lock.locked():
            raise RepoMutationBusy(normalized, operation)
        await lock.acquire()

        file_handle: BinaryIO | None = None
        turnstile_handle: BinaryIO | None = None
        try:
            if fcntl is not None:
                turnstile_handle = self._open_turnstile_file(normalized)
                try:
                    # Normal writers take the turnstile exclusively only for
                    # the transition into the main repository lock. Cleanup
                    # waiters hold it shared while waiting, so no writer can
                    # pass through a cleanup handoff gap.
                    fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RepoMutationBusy(normalized, operation) from error
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
        finally:
            if turnstile_handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    turnstile_handle.close()

    @contextlib.asynccontextmanager
    async def hold(self, repo_path: str, *, operation: str) -> AsyncIterator[RepoMutationLease]:
        lease = await self.acquire(repo_path, operation=operation)
        try:
            yield lease
        finally:
            lease.release()

    def bind_to_job(self, job_id: str, lease: RepoMutationLease) -> None:
        with self._cleanup_guard:
            self._job_leases[str(job_id)] = lease

    def bound_job_ids(self) -> set[str]:
        """Return jobs that still own an in-process lease or cleanup barrier."""

        with self._cleanup_guard:
            return set(self._job_leases).union(self._cleanup_job_repos)

    def duplicate_job_lock_fd(self, job_id: str) -> int | None:
        """Duplicate a bound job's OS lock for its process supervisor.

        The duplicate refers to the same open-file description as the normal
        lease.  If PatchBay crashes, closing the parent descriptor does not
        release the flock while the supervisor is still alive.  The caller
        owns the returned descriptor and must close its local copy after the
        supervisor has inherited it.
        """

        with self._cleanup_guard:
            lease = self._job_leases.get(str(job_id))
            if (
                lease is None
                or lease.released
                or lease.file_handle is None
                or lease.file_handle.closed
            ):
                return None
            duplicate = os.dup(lease.file_handle.fileno())
            os.set_inheritable(duplicate, True)
            return duplicate

    def block_job_cleanup(
        self,
        job_id: str,
        repo_path: str,
        *,
        operation: str,
    ) -> None:
        """Recreate a durable-job mutation barrier after PatchBay restart.

        The original in-process lease already provides this barrier while the
        executor is alive. After restart its file descriptor is gone, so a
        terminal job with unresolved process cleanup must reacquire the lock
        file and block new local acquisitions until process death is proven.
        """

        job = str(job_id)
        if job in self._job_leases:
            return
        normalized = str(Path(repo_path).expanduser().resolve())
        with self._cleanup_guard:
            if self._cleanup_job_repos.get(job) == normalized:
                self._cleanup_started_events[normalized].wait()
                return
            if job in self._cleanup_job_repos:
                self._release_cleanup_block_locked(job)

            jobs = self._cleanup_block_jobs.setdefault(normalized, set())
            jobs.add(job)
            self._cleanup_job_repos[job] = normalized
            if normalized in self._cleanup_waiter_threads:
                self._cleanup_started_events[normalized].wait()
                return

            turnstile_handle = self._open_turnstile_file(normalized)
            if fcntl is not None:
                while True:
                    try:
                        # Cleanup waiters take the turnstile shared. Multiple
                        # recoveries can therefore queue safely, while normal
                        # writers (which require it exclusively) remain blocked
                        # until every cleanup waiter has acquired the main lock.
                        fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_SH)
                        break
                    except InterruptedError:
                        continue

            handle = self._open_lock_file(normalized)
            already_acquired = False
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    already_acquired = True
                except BlockingIOError:
                    pass

            stop_event = threading.Event()
            started_event = threading.Event()
            acquired_event = threading.Event()
            waiter = threading.Thread(
                target=self._hold_cleanup_lock,
                args=(
                    handle,
                    turnstile_handle,
                    stop_event,
                    started_event,
                    acquired_event,
                    already_acquired,
                ),
                name=f"patchbay-repo-cleanup-{hashlib.sha256(normalized.encode()).hexdigest()[:8]}",
                daemon=True,
            )
            self._cleanup_block_handles[normalized] = handle
            self._cleanup_turnstile_handles[normalized] = turnstile_handle
            self._cleanup_stop_events[normalized] = stop_event
            self._cleanup_started_events[normalized] = started_event
            self._cleanup_acquired_events[normalized] = acquired_event
            self._cleanup_waiter_threads[normalized] = waiter
            self._cleanup_threads.add(waiter)
            try:
                waiter.start()
            except Exception:
                self._cleanup_threads.discard(waiter)
                self._cleanup_waiter_threads.pop(normalized, None)
                self._cleanup_acquired_events.pop(normalized, None)
                self._cleanup_started_events.pop(normalized, None)
                self._cleanup_stop_events.pop(normalized, None)
                self._cleanup_block_handles.pop(normalized, None)
                self._cleanup_turnstile_handles.pop(normalized, None)
                jobs.discard(job)
                if not jobs:
                    self._cleanup_block_jobs.pop(normalized, None)
                self._cleanup_job_repos.pop(job, None)
                if already_acquired and fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                try:
                    if fcntl is not None:
                        fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    turnstile_handle.close()
                raise

        # The inter-process turnstile is already held before the daemon starts.
        # Waiting here only confirms that the daemon owns the handles and has
        # entered the main-lock acquisition path.
        started_event.wait()

    def release_job(self, job_id: str) -> None:
        job = str(job_id)
        with self._cleanup_guard:
            lease = self._job_leases.pop(job, None)
        if lease is not None:
            lease.release()
        self._release_cleanup_block(job)

    def _release_cleanup_block(self, job_id: str) -> None:
        with self._cleanup_guard:
            waiter, acquired_event = self._release_cleanup_block_locked(job_id)
        if waiter is None or waiter is threading.current_thread():
            return
        if acquired_event is not None and not acquired_event.is_set():
            acquired_event.wait(timeout=0.1)
        if acquired_event is not None and acquired_event.is_set():
            waiter.join(timeout=1.0)

    def _release_cleanup_block_locked(
        self, job_id: str
    ) -> tuple[threading.Thread | None, threading.Event | None]:
        normalized = self._cleanup_job_repos.pop(job_id, None)
        if normalized is None:
            return None, None
        jobs = self._cleanup_block_jobs.get(normalized)
        if jobs is None:
            return None, None
        jobs.discard(job_id)
        if jobs:
            return None, None
        self._cleanup_block_jobs.pop(normalized, None)
        self._cleanup_block_handles.pop(normalized, None)
        self._cleanup_turnstile_handles.pop(normalized, None)
        stop_event = self._cleanup_stop_events.pop(normalized, None)
        self._cleanup_started_events.pop(normalized, None)
        acquired_event = self._cleanup_acquired_events.pop(normalized, None)
        waiter = self._cleanup_waiter_threads.pop(normalized, None)
        if stop_event is not None:
            stop_event.set()
        return waiter, acquired_event

    def _hold_cleanup_lock(
        self,
        handle: BinaryIO,
        turnstile_handle: BinaryIO,
        stop_event: threading.Event,
        started_event: threading.Event,
        acquired_event: threading.Event,
        already_acquired: bool,
    ) -> None:
        turnstile_released = False
        try:
            started_event.set()
            if not already_acquired and fcntl is not None:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                        break
                    except InterruptedError:
                        continue
            acquired_event.set()
            try:
                if fcntl is not None:
                    fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_UN)
            finally:
                turnstile_handle.close()
                turnstile_released = True
            if not stop_event.is_set():
                stop_event.wait()
        finally:
            if not turnstile_released:
                try:
                    if fcntl is not None:
                        fcntl.flock(turnstile_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    turnstile_handle.close()
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                with self._cleanup_guard:
                    self._cleanup_threads.discard(threading.current_thread())

    def shutdown(self, timeout: float = 1.0) -> bool:
        """Stop cleanup waiters without waiting indefinitely on an OS lock owner."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._cleanup_guard:
            for event in self._cleanup_stop_events.values():
                event.set()
            self._cleanup_job_repos.clear()
            self._cleanup_block_jobs.clear()
            self._cleanup_block_handles.clear()
            self._cleanup_turnstile_handles.clear()
            self._cleanup_stop_events.clear()
            self._cleanup_started_events.clear()
            self._cleanup_acquired_events.clear()
            self._cleanup_waiter_threads.clear()
            threads = tuple(self._cleanup_threads)

        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def _open_lock_file(self, repo_path: str) -> BinaryIO:
        digest = hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:32]
        path = self.lock_dir / f"repo_{digest}.lock"
        return path.open("a+b")

    def _open_turnstile_file(self, repo_path: str) -> BinaryIO:
        digest = hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:32]
        path = self.lock_dir / f"repo_{digest}.turnstile.lock"
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
        return worker_workspace_mode == "shared_write" and not bool(
            options.get(ALLOW_CONCURRENT_SHARED_WRITE_OPTION)
        )
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
