import asyncio
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from patchbay.repo_locks import (
    ALLOW_CONCURRENT_SHARED_WRITE_OPTION,
    RepoMutationBusy,
    RepoMutationLockManager,
    job_requires_repo_mutation_lock,
)


def lock_config(tmp_path):
    return {
        "logging": {"job_logs_dir": str(tmp_path / "logs"), "job_state_dir": str(tmp_path / "state")},
        "locks": {"root": str(tmp_path / "locks")},
    }


def _attempt_repo_lock_in_process(config, repo_path, result_connection):
    async def attempt() -> str:
        manager = RepoMutationLockManager(config)
        try:
            lease = await manager.acquire(repo_path, operation="third-process")
        except RepoMutationBusy:
            return "busy"
        lease.release()
        return "acquired"

    try:
        result_connection.send(asyncio.run(attempt()))
    finally:
        result_connection.close()


class _PausedCleanupLockManager(RepoMutationLockManager):
    """Expose the historical post-owner-release scheduling gap deterministically."""

    def __init__(self, config):
        super().__init__(config)
        self.waiter_paused = threading.Event()
        self.allow_main_lock = threading.Event()

    def _hold_cleanup_lock(
        self,
        handle,
        turnstile_handle,
        stop_event,
        started_event,
        acquired_event,
        already_acquired,
    ):
        started_event.set()
        self.waiter_paused.set()
        if not self.allow_main_lock.wait(timeout=5):
            stop_event.set()
        super()._hold_cleanup_lock(
            handle,
            turnstile_handle,
            stop_event,
            started_event,
            acquired_event,
            already_acquired,
        )


@pytest.mark.asyncio
async def test_repo_mutation_lock_is_keyed_by_normalized_repo_path(tmp_path):
    config = lock_config(tmp_path)
    manager = RepoMutationLockManager(config)
    repo = tmp_path / "repo"
    repo.mkdir()

    lease = await manager.acquire(str(repo), operation="first")
    try:
        with pytest.raises(RepoMutationBusy) as error:
            await manager.acquire(str(repo / "."), operation="second")
        payload = error.value.public_payload()
        assert payload["repo_busy"] is True
        assert payload["workspace_name"] == "repo"
        assert str(tmp_path) not in str(payload)
    finally:
        lease.release()

    second = await manager.acquire(str(Path(repo)), operation="second")
    second.release()


@pytest.mark.asyncio
async def test_cleanup_waiter_closes_cross_manager_lock_gap(tmp_path):
    config = lock_config(tmp_path)
    original_manager = RepoMutationLockManager(config)
    cleanup_manager = RepoMutationLockManager(config)
    third_manager = RepoMutationLockManager(config)
    repo = tmp_path / "repo"
    repo.mkdir()
    normalized = str(repo.resolve())

    original_lease = await original_manager.acquire(normalized, operation="original")
    cleanup_manager.block_job_cleanup("cleanup-job", normalized, operation="recovery")
    acquired_event = cleanup_manager._cleanup_acquired_events[normalized]
    assert not acquired_event.is_set()

    probe_started = asyncio.Event()
    cleanup_resolved = asyncio.Event()
    acquired_before_resolution = False

    async def probe_third_manager() -> None:
        nonlocal acquired_before_resolution
        while not cleanup_resolved.is_set():
            try:
                lease = await third_manager.acquire(normalized, operation="third")
            except RepoMutationBusy:
                probe_started.set()
                await asyncio.sleep(0.001)
                continue
            lease.release()
            acquired_before_resolution = True
            return

    probe = asyncio.create_task(probe_third_manager())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    original_lease.release()
    assert await asyncio.to_thread(acquired_event.wait, 2)
    await asyncio.sleep(0.02)
    assert acquired_before_resolution is False

    cleanup_resolved.set()
    cleanup_manager.release_job("cleanup-job")
    await asyncio.wait_for(probe, timeout=1)

    for _ in range(100):
        try:
            final_lease = await third_manager.acquire(normalized, operation="after_cleanup")
        except RepoMutationBusy:
            await asyncio.sleep(0.001)
        else:
            final_lease.release()
            break
    else:
        pytest.fail("cleanup waiter did not release the repository lock")

    assert cleanup_manager.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_cleanup_turnstile_prevents_deterministic_cross_process_handoff_gap(tmp_path):
    config = lock_config(tmp_path)
    original_manager = RepoMutationLockManager(config)
    cleanup_manager = _PausedCleanupLockManager(config)
    repo = tmp_path / "repo"
    repo.mkdir()
    normalized = str(repo.resolve())

    original_lease = await original_manager.acquire(normalized, operation="original")
    cleanup_manager.block_job_cleanup("cleanup-job", normalized, operation="recovery")
    assert cleanup_manager.waiter_paused.wait(timeout=1)

    # Force the exact historical window: the original owner is gone, but the
    # cleanup waiter has not yet attempted the main flock. A separate process
    # must still fail fast because cleanup established its turnstile first.
    original_lease.release()
    context = multiprocessing.get_context("spawn")
    result_reader, result_writer = context.Pipe(duplex=False)
    contender = context.Process(
        target=_attempt_repo_lock_in_process,
        args=(config, normalized, result_writer),
    )
    contender.start()
    result_writer.close()
    try:
        assert result_reader.poll(5), "third process did not report its lock result"
        assert result_reader.recv() == "busy"
    finally:
        result_reader.close()
        contender.join(timeout=5)
        if contender.is_alive():
            contender.terminate()
            contender.join(timeout=1)
        cleanup_manager.allow_main_lock.set()

    assert contender.exitcode == 0
    acquired_event = cleanup_manager._cleanup_acquired_events[normalized]
    assert await asyncio.to_thread(acquired_event.wait, 2)
    cleanup_manager.release_job("cleanup-job")
    assert cleanup_manager.shutdown(timeout=1)

    final_lease = await original_manager.acquire(normalized, operation="after-cleanup")
    final_lease.release()


@pytest.mark.asyncio
async def test_cleanup_release_while_waiting_is_idempotent(tmp_path):
    config = lock_config(tmp_path)
    original_manager = RepoMutationLockManager(config)
    cleanup_manager = RepoMutationLockManager(config)
    next_manager = RepoMutationLockManager(config)
    repo = tmp_path / "repo"
    repo.mkdir()
    normalized = str(repo.resolve())

    original_lease = await original_manager.acquire(normalized, operation="original")
    cleanup_manager.block_job_cleanup("cleanup-job", normalized, operation="recovery")
    cleanup_manager.block_job_cleanup("cleanup-job", normalized, operation="recovery")
    waiter = cleanup_manager._cleanup_waiter_threads[normalized]

    cleanup_manager.release_job("cleanup-job")
    cleanup_manager.release_job("cleanup-job")
    assert waiter.is_alive()

    original_lease.release()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    lease = await next_manager.acquire(normalized, operation="after_cleanup")
    lease.release()
    assert cleanup_manager.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_cleanup_waiter_shutdown_is_bounded_while_os_lock_is_owned(tmp_path):
    config = lock_config(tmp_path)
    original_manager = RepoMutationLockManager(config)
    cleanup_manager = RepoMutationLockManager(config)
    repo = tmp_path / "repo"
    repo.mkdir()
    normalized = str(repo.resolve())

    original_lease = await original_manager.acquire(normalized, operation="original")
    cleanup_manager.block_job_cleanup("cleanup-job", normalized, operation="recovery")
    waiter = cleanup_manager._cleanup_waiter_threads[normalized]

    started_at = time.monotonic()
    assert cleanup_manager.shutdown(timeout=0.02) is False
    assert time.monotonic() - started_at < 0.5

    original_lease.release()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert cleanup_manager.shutdown(timeout=0.02) is True


def test_worker_workspace_mode_controls_repo_mutation_lock():
    assert (
        job_requires_repo_mutation_lock(
            "interactive",
            {
                "_worker_workspace_mode": "read_only",
                "dangerously_bypass": True,
                "sandbox": "workspace-write",
            },
            default_sandbox="danger-full-access",
        )
        is False
    )
    assert (
        job_requires_repo_mutation_lock(
            "interactive",
            {
                "_worker_workspace_mode": "isolated_write",
                "_worker_worktree_path": "/tmp/patchbay-worker",
                "dangerously_bypass": True,
            },
            default_sandbox="danger-full-access",
        )
        is False
    )
    assert (
        job_requires_repo_mutation_lock(
            "interactive",
            {"_worker_workspace_mode": "shared_write", "sandbox": "read-only"},
            default_sandbox="read-only",
        )
        is True
    )
    assert (
        job_requires_repo_mutation_lock(
            "interactive",
            {
                "_worker_workspace_mode": "shared_write",
                ALLOW_CONCURRENT_SHARED_WRITE_OPTION: True,
            },
            default_sandbox="danger-full-access",
        )
        is False
    )


def test_generic_jobs_still_lock_when_they_can_mutate_base_checkout():
    assert job_requires_repo_mutation_lock("plan", {"sandbox": "read-only"}) is False
    assert job_requires_repo_mutation_lock("plan", {"sandbox": "workspace-write"}) is True
    assert job_requires_repo_mutation_lock("plan", {"dangerously_bypass": True}) is True
    assert job_requires_repo_mutation_lock("apply", {"sandbox": "workspace-write"}) is False
