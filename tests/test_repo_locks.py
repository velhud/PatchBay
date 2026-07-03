from pathlib import Path

import pytest

from patchbay.repo_locks import RepoMutationBusy, RepoMutationLockManager, job_requires_repo_mutation_lock


@pytest.mark.asyncio
async def test_repo_mutation_lock_is_keyed_by_normalized_repo_path(tmp_path):
    config = {
        "logging": {"job_logs_dir": str(tmp_path / "logs"), "job_state_dir": str(tmp_path / "state")},
        "locks": {"root": str(tmp_path / "locks")},
    }
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


def test_generic_jobs_still_lock_when_they_can_mutate_base_checkout():
    assert job_requires_repo_mutation_lock("plan", {"sandbox": "read-only"}) is False
    assert job_requires_repo_mutation_lock("plan", {"sandbox": "workspace-write"}) is True
    assert job_requires_repo_mutation_lock("plan", {"dangerously_bypass": True}) is True
    assert job_requires_repo_mutation_lock("apply", {"sandbox": "workspace-write"}) is False
