from pathlib import Path

import pytest

from patchbay.repo_locks import RepoMutationBusy, RepoMutationLockManager


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
