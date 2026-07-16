import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from patchbay.jobs import manager as manager_module
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


def test_job_manager_persists_redacted_completed_job(tmp_path, monkeypatch):
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
    assert persisted["persistence_version"] == 2
    assert "prompt" not in persisted
    assert "prompt_preview" not in persisted
    assert persisted["result"]["summary"] == "done with token=[REDACTED_POSSIBLE_SECRET]"
    assert "_raw_stdout" not in persisted["result"]
    assert "inspect with password" not in serialized
    assert "fixture-value" not in serialized
    assert "raw output should not persist" not in serialized

    reload_persists = []
    persist_job = JobManager._persist_job

    def count_reload_persist(self, job):
        reload_persists.append(job.job_id)
        return persist_job(self, job)

    monkeypatch.setattr(JobManager, "_persist_job", count_reload_persist)
    reloaded = JobManager(config)
    assert reload_persists == []
    job = reloaded.get_job(job_id)
    assert job is not None
    assert job.state == JobState.COMPLETED
    assert job.prompt == ""
    assert job.result == {
        "summary": "done with token=[REDACTED_POSSIBLE_SECRET]",
        "files_changed": [],
    }

    persisted.pop("persistence_version")
    record_path.write_text(json.dumps(persisted), encoding="utf-8")
    reload_persists.clear()
    JobManager(config)
    assert reload_persists == [job_id]
    assert json.loads(record_path.read_text(encoding="utf-8"))[
        "persistence_version"
    ] == 2


def test_identical_job_persistence_skips_atomic_rewrite(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "inspect", config["repositories"]["default"], {}
    )
    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    original_payload = record_path.read_bytes()
    original_mtime = record_path.stat().st_mtime_ns
    original_revision = manager.state_revision
    replace_calls = []
    atomic_replace = manager_module.os.replace

    def record_replace(source, destination):
        replace_calls.append((source, destination))
        return atomic_replace(source, destination)

    monkeypatch.setattr(manager_module.os, "replace", record_replace)

    manager._persist_job(manager.jobs[job_id])

    assert replace_calls == []
    assert manager.state_revision == original_revision
    assert record_path.read_bytes() == original_payload
    assert record_path.stat().st_mtime_ns == original_mtime


def test_completion_evidence_is_redacted_and_survives_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "inspect", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)

    assert manager.record_completion_evidence(
        job_id,
        source="stdout_turn_completed",
        observed_at=123.5,
        fallback_result={
            "summary": "done with token=fixture-value",
            "completion_evidence_recovered": True,
        },
        session_id="session-evidence",
        result_status="structured",
    )

    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "running"
    assert persisted["terminal_source"] is None
    assert persisted["completion_evidence_source"] == "stdout_turn_completed"
    assert persisted["completion_evidence_observed_at"] == 123.5
    assert persisted["completion_evidence_session_id"] == "session-evidence"
    assert persisted["completion_evidence_result_status"] == "structured"
    assert persisted["completion_evidence_version"] == 1
    assert persisted["completion_evidence_result"]["summary"] == (
        "done with token=[REDACTED_POSSIBLE_SECRET]"
    )
    assert "fixture-value" not in record_path.read_text(encoding="utf-8")

    reloaded = JobManager(config).get_job(job_id)
    assert reloaded.state == JobState.RUNNING
    assert reloaded.completion_evidence_source == "stdout_turn_completed"
    assert reloaded.completion_evidence_result["summary"] == (
        "done with token=[REDACTED_POSSIBLE_SECRET]"
    )


def test_completion_evidence_is_structurally_capped_without_invalid_json(tmp_path):
    config = make_config(tmp_path)
    config["logging"]["job_log_max_bytes"] = 128
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "large completion", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)

    manager.record_completion_evidence(
        job_id,
        source="stdout_turn_completed",
        observed_at=123.5,
        fallback_result={
            "summary": "S" * 10_000,
            "detailed_report": "D" * 1_000_000,
            "_private": "must not persist",
        },
        result_status="structured",
    )

    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    evidence = persisted["completion_evidence_result"]
    compact = json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(compact) <= 128
    assert persisted["completion_evidence_result_status"] == "truncated"
    assert "_private" not in evidence
    assert "must not persist" not in record_path.read_text(encoding="utf-8")


def test_running_job_does_not_adopt_secondary_result_artifact_on_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "running result artifact", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    result_path = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"summary": "secondary artifact", "files_changed": []}),
        encoding="utf-8",
    )

    reloaded = JobManager(config).get_job(job_id)

    assert reloaded.state == JobState.RUNNING
    assert reloaded.result is None


def test_job_manager_can_store_complete_private_prompt_evidence(tmp_path):
    config = make_config(tmp_path)
    config["logging"]["private_evidence_log"] = True
    config["logging"]["private_evidence_dir"] = str(tmp_path / "logs" / "private-evidence")
    manager = JobManager(config)
    prompt = "Implement this exact worker brief with private context password = fixture-value"

    job_id = manager.create_job(
        "plan",
        prompt,
        config["repositories"]["default"],
        {"structured_output": True, "_worker_name": "Evidence Worker"},
    )

    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    serialized = json.dumps(persisted)
    assert "prompt" not in persisted
    assert prompt not in serialized
    assert persisted["prompt_artifact"]
    assert persisted["prompt_sha256"]
    assert persisted["prompt_bytes"] == len(prompt.encode("utf-8"))

    evidence_path = tmp_path / "logs" / "private-evidence" / "jobs" / job_id / "brief.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "job_brief"
    assert payload["job_id"] == job_id
    assert payload["prompt"] == prompt
    assert payload["options"]["_worker_name"] == "Evidence Worker"
    assert payload["prompt_sha256"] == persisted["prompt_sha256"]

    reloaded = JobManager(config)
    job = reloaded.get_job(job_id)
    assert job is not None
    assert job.prompt == ""
    assert job.prompt_artifact == str(evidence_path)


def test_completed_job_clears_prior_running_error(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.update_job_state(job_id, JobState.RUNNING, error="temporary stale marker")

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


def test_completed_job_error_is_cleared_on_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        result={"summary": "done", "files_changed": []},
        exit_code=0,
    )
    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    stale_record = json.loads(record_path.read_text(encoding="utf-8"))
    stale_record["error"] = "stale failure from an old lifecycle race"
    record_path.write_text(json.dumps(stale_record), encoding="utf-8")

    reloaded = JobManager(config)

    job = reloaded.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.error is None
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    assert persisted["error"] is None


def test_terminal_job_clears_live_current_command_state(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        current_phase="command_running",
        current_item_type="command_execution",
        current_item_status="started",
        current_command_preview="rg SampleRepo",
        current_command_started_at=time.time() - 30,
    )

    manager.update_job_state(job_id, JobState.COMPLETED, result={"summary": "done", "files_changed": []})

    job = manager.get_job(job_id)
    assert job.current_phase is None
    assert job.current_item_type is None
    assert job.current_item_status is None
    assert job.current_command_preview is None
    assert job.current_command_started_at is None
    assert job.last_command_preview == "rg SampleRepo"

    reloaded = JobManager(config)
    reloaded_job = reloaded.get_job(job_id)
    assert reloaded_job.current_command_preview is None
    assert reloaded_job.last_command_preview == "rg SampleRepo"


def test_job_manager_preserves_running_jobs_for_executor_reconciliation_on_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)

    reloaded = JobManager(config)
    job = reloaded.get_job(job_id)
    assert job is not None
    assert job.state == JobState.RUNNING
    assert job.completed_at is None
    assert job.error is None
    assert "executor reconciliation" in job.progress


def test_job_manager_marks_interrupted_pending_jobs_failed_on_reload(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})

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


def test_job_manager_serializes_high_contention_state_and_option_persistence(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "persistence race", config["repositories"]["default"], {})
    operation_count = 1_024
    workers = 32
    start = threading.Barrier(workers)

    def mutate(index: int) -> None:
        start.wait()
        if index % 2:
            manager.update_job_options(job_id, {"kind": "options", "sequence": index})
        else:
            manager.update_job_state(
                job_id,
                JobState.RUNNING,
                progress=f"state-{index}",
                last_event=f"event-{index}",
                event_count=index,
            )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(mutate, range(operation_count)))

    job = manager.get_job(job_id)
    assert job is not None
    record_path = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    assert persisted == job.to_persisted_dict()
    assert persisted["state"] == JobState.RUNNING.value
    assert not list(record_path.parent.glob("*.tmp"))

    reloaded = JobManager(config)
    reloaded_job = reloaded.get_job(job_id)
    assert reloaded_job is not None
    assert reloaded_job.to_persisted_dict() == persisted


def test_job_option_mutations_merge_without_losing_concurrent_fields(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "atomic option patches", config["repositories"]["default"], {}
    )
    workers = 32
    start = threading.Barrier(workers)

    def mutate(index: int) -> None:
        start.wait()
        manager.mutate_job_options(
            job_id,
            lambda current: {**current, f"field_{index}": index},
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(mutate, range(workers)))

    options = manager.get_job(job_id).options
    assert options == {f"field_{index}": index for index in range(workers)}
    reloaded = JobManager(config)
    assert reloaded.get_job(job_id).options == options


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


@pytest.mark.parametrize(
    "cleanup_outcome",
    [
        "cleanup_pending",
        "cleanup_retry_pending_process_live",
        "cleanup_blocked_untrusted_process_identity",
    ],
)
def test_retention_and_explicit_cleanup_preserve_unresolved_process_ownership(
    tmp_path, cleanup_outcome
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "retain unresolved ownership", config["repositories"]["default"], {}
    )
    manager.transition_job_terminal(
        job_id,
        JobState.FAILED,
        result={"summary": "terminal"},
        wrapper_cleanup_outcome=cleanup_outcome,
    )
    manager.jobs[job_id].completed_at = time.time() - (25 * 3600)
    manager._persist_job(manager.jobs[job_id])
    record = tmp_path / "logs" / "jobs" / "state" / f"{job_id}.json"

    manager.cleanup_old_jobs()
    explicit = manager.cleanup_job(job_id)

    assert explicit is False
    assert manager.get_job(job_id) is not None
    assert record.exists()
