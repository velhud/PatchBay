import json
import time

from job_manager import JobManager, JobState


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
    assert persisted["prompt_preview"] == "inspect with password = [REDACTED_POSSIBLE_SECRET]"
    assert persisted["result"]["summary"] == "done with token=[REDACTED_POSSIBLE_SECRET]"
    assert "_raw_stdout" not in persisted["result"]
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
