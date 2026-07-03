import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from patchbay.jobs.manager import JobManager, JobState


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {"require_git_repo": False},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


def test_job_manager_persists_redacted_completed_job(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan",
        "inspect with password = fixture-value",
        config["repositories"]["default"],
        {"structured_output": True},
    )

    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        result={
            "summary": "done with token=fixture-value",
            "_raw_stdout": "raw output should not persist",
            "files_changed": [],
        },
        session_id="session-123",
        exit_code=0,
    )

    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    serialized = json.dumps(persisted)
    assert persisted["state"] == "completed"
    assert "prompt" not in persisted
    assert "prompt_preview" not in persisted
    assert persisted["result"]["summary"] == "done with token=[REDACTED_POSSIBLE_SECRET]"
    assert "_raw_stdout" not in persisted["result"]
    assert "inspect with password" not in serialized
    assert "fixture-value" not in serialized
    assert "raw output should not persist" not in serialized

    reloaded = JobManager(config)
    job = reloaded.get_job(job_id)
    assert job is not None
    assert job.state == JobState.COMPLETED
    assert job.prompt == ""
    assert job.result == {
        "summary": "done with token=[REDACTED_POSSIBLE_SECRET]",
        "files_changed": [],
    }


def test_completed_job_clears_prior_running_error(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.update_job_state(job_id, JobState.FAILED, error="temporary stale marker")

    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        result={"summary": "done", "files_changed": []},
        exit_code=0,
    )

    job = manager.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.error is None
    persisted = json.loads((tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert persisted["state"] == "completed"
    assert persisted["error"] is None


def test_job_manager_marks_interrupted_running_jobs_failed_on_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)

    reloaded = JobManager(config)
    job = reloaded.get_job(job_id)
    assert job is not None
    assert job.state == JobState.FAILED
    assert job.completed_at is not None
    assert "server stopped" in job.error


def test_pending_jobs_count_against_concurrency_limit(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    first = manager.create_job("plan", "inspect", config["repositories"]["default"], {})

    assert manager.active_job_count() == 1
    with pytest.raises(RuntimeError, match="active includes pending and running jobs"):
        manager.create_job("plan", "inspect again", config["repositories"]["default"], {})

    manager.update_job_state(first, JobState.COMPLETED, result={"summary": "done"})
    assert manager.active_job_count() == 0
    second = manager.create_job("plan", "inspect after completion", config["repositories"]["default"], {})
    assert second


def test_job_creation_admission_is_thread_safe(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)

    def create(index: int) -> tuple[str, str]:
        try:
            return ("ok", manager.create_job("plan", f"inspect {index}", config["repositories"]["default"], {}))
        except RuntimeError as error:
            return ("error", str(error))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [1, 2]))

    successes = [value for status, value in results if status == "ok"]
    errors = [value for status, value in results if status == "error"]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "active includes pending and running jobs" in errors[0]
    assert len(manager.jobs) == 1


def test_job_creation_can_queue_when_enabled(tmp_path):
    config = make_config(tmp_path)
    config["server"]["queue_enabled"] = True
    manager = JobManager(config)

    first = manager.create_job("plan", "inspect 1", config["repositories"]["default"], {})
    second = manager.create_job("plan", "inspect 2", config["repositories"]["default"], {})

    assert first != second
    assert len(manager.jobs) == 2
    assert manager.active_job_count() == 2


def test_cleanup_old_jobs_removes_persisted_record(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.COMPLETED, result={"summary": "done"})
    manager.jobs[job_id].completed_at = time.time() - (25 * 3600)
    manager._persist_job(manager.jobs[job_id])

    manager.cleanup_old_jobs()

    assert manager.get_job(job_id) is None
    assert not (tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json").exists()
