from __future__ import annotations

import json
import contextlib
from pathlib import Path

import pytest

from patchbay.jobs.cleanup_recovery import CONFIRMATION, _server_is_listening, recover_cleanup
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobInfo, JobManager, JobState
from patchbay.repo_locks import RepoMutationBusy, RepoMutationLockManager


def _fixture(tmp_path: Path) -> tuple[dict, JobInfo, Path]:
    logs = tmp_path / "logs"
    states = tmp_path / "state"
    worktrees = tmp_path / "worktrees"
    repo = tmp_path / "fixture-repo"
    for path in (logs, states, worktrees, repo):
        path.mkdir()
    config = {
        "server": {
            "host": "127.0.0.1",
            "port": 59871,
            "max_concurrent_jobs": 3,
            "queue_enabled": False,
            "job_timeout_seconds": 0,
            "job_cleanup_after_hours": 24,
        },
        "logging": {
            "job_logs_dir": str(logs),
            "job_state_dir": str(states),
            "worktrees_dir": str(worktrees),
        },
        "locks": {"root": str(tmp_path / "locks")},
        "security": {},
    }
    job_id = "11111111-2222-4333-8444-555555555555"
    pid = 65531
    sentinel = 65532
    proof = logs / f"{job_id}.supervisor-proof"
    proof.write_text(
        f"patchbay-supervisor-cleanup-unproven-v2:{pid}:{sentinel}\n",
        encoding="ascii",
    )
    proof.chmod(0o600)
    job = JobInfo(
        job_id=job_id,
        state=JobState.FAILED,
        mode="apply",
        prompt="synthetic test",
        repo_path=str(repo),
        options={
            "_job_process_supervisor_version": 3,
            "_job_process_supervisor_spawned": True,
            "_job_process_supervisor_cleanup_proof": str(proof),
            "_job_process_marker_version": 1,
            "_job_descendant_process_identities_v1": [],
        },
        process_pid=pid,
        process_pgid=pid,
        exit_code=1,
        wrapper_cleanup_outcome="cleanup_blocked_untrusted_process_identity",
    )
    manager = JobManager(config)
    manager.jobs[job_id] = job
    manager._persist_job(job)
    return config, job, proof


@pytest.fixture
def quiescent_census(monkeypatch):
    monkeypatch.setattr("patchbay.jobs.cleanup_recovery._server_is_listening", lambda _config: False)
    monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda _pid: False)
    monkeypatch.setattr(JobExecutor, "_process_group_liveness", lambda _self, _pgid: False)
    monkeypatch.setattr(JobExecutor, "_job_marked_process_pids", lambda _self, _job_id, **_kw: set())


def _state_bytes(config: dict, job_id: str) -> bytes:
    return (Path(config["logging"]["job_state_dir"]) / f"{job_id}.json").read_bytes()


def test_server_probe_checks_nondefault_configured_loopback_first(monkeypatch):
    attempted = []

    def connect(address, timeout):
        attempted.append(address[0])
        if address[0] == "127.0.0.2":
            return contextlib.nullcontext()
        raise OSError("not listening")

    monkeypatch.setattr("patchbay.jobs.cleanup_recovery.socket.create_connection", connect)
    assert _server_is_listening({"server": {"host": "127.0.0.2", "port": 8895}})
    assert attempted[0] == "127.0.0.2"


def test_recovery_retries_transient_marker_scan_uncertainty_but_requires_empty_census(
    tmp_path, monkeypatch
):
    config, job, _proof = _fixture(tmp_path)
    monkeypatch.setattr("patchbay.jobs.cleanup_recovery._server_is_listening", lambda _config: False)
    monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda _pid: False)
    monkeypatch.setattr(JobExecutor, "_process_group_liveness", lambda _self, _pgid: False)
    scans = iter([None, set(), set(), set(), set()])
    monkeypatch.setattr(
        JobExecutor,
        "_job_marked_process_pids",
        lambda _self, _job_id, **_kw: next(scans),
    )
    result = recover_cleanup(
        config,
        job.job_id,
        operator="Authorized local operator",
    )
    assert result["ready"] is True
    assert result["applied"] is False


def test_cleanup_recovery_dry_run_is_read_only_and_apply_records_operator_attestation(
    tmp_path, monkeypatch, quiescent_census
):
    config, job, proof = _fixture(tmp_path)
    before = _state_bytes(config, job.job_id)
    dry_run = recover_cleanup(config, job.job_id, operator="fixture-operator")
    assert dry_run["ready"] is True
    assert dry_run["applied"] is False
    assert _state_bytes(config, job.job_id) == before
    assert list(Path(config["logging"]["job_logs_dir"]).glob("*.operator-cleanup-recovery.*")) == []

    with pytest.raises(ValueError, match="exact --confirm-quiescence"):
        recover_cleanup(config, job.job_id, operator="fixture-operator", apply=True)
    assert _state_bytes(config, job.job_id) == before
    assert list(Path(config["logging"]["job_logs_dir"]).glob("*.operator-cleanup-recovery.*")) == []

    result = recover_cleanup(
        config,
        job.job_id,
        operator="fixture-operator",
        apply=True,
        confirmation=CONFIRMATION,
    )
    assert result["applied"] is True
    assert proof.read_text(encoding="ascii").startswith(
        f"patchbay-supervisor-cleanup-unproven-v2:{job.process_pid}:"
    )
    reloaded = JobManager(config).get_job(job.job_id)
    assert reloaded is not None
    assert reloaded.wrapper_cleanup_outcome == "operator_resolved_quiescence_attested"
    executor = JobExecutor(config, JobManager(config))
    assert executor._supervisor_cleanup_proven(job.job_id)


@pytest.mark.parametrize(
    "blocker, expected",
    [
        ("live_pid", "Recorded supervisor or sentinel PID is live"),
        ("recycled_pid", "Recorded supervisor or sentinel PID is live"),
        ("live_sentinel", "Recorded supervisor or sentinel PID is live"),
        ("group_unknown", "Process-group census is live or inconclusive"),
        ("marker_unknown", "Same-job marker census remained inconclusive"),
        ("untrusted", "unresolved or untrusted observation"),
        ("tracked_unknown_live", "tracked descendant PID is still live"),
        ("bad_proof", "does not exactly match"),
        ("lock_busy", "Repository mutation lock is busy"),
    ],
)
def test_cleanup_recovery_fails_closed_without_state_or_receipt_changes(
    tmp_path, monkeypatch, quiescent_census, blocker, expected
):
    config, job, proof = _fixture(tmp_path)
    if blocker == "live_pid":
        monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda pid: pid == job.process_pid)
    elif blocker == "recycled_pid":
        monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda pid: pid == job.process_pid)
    elif blocker == "live_sentinel":
        monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda pid: pid == 65532)
    elif blocker == "group_unknown":
        monkeypatch.setattr(JobExecutor, "_process_group_liveness", lambda _self, _pgid: None)
    elif blocker == "marker_unknown":
        monkeypatch.setattr(JobExecutor, "_job_marked_process_pids", lambda _self, _job_id, **_kw: None)
    elif blocker == "untrusted":
        job.options["_job_descendant_scan_uncertain_v1"] = True
        JobManager(config)._persist_job(job)
    elif blocker == "tracked_unknown_live":
        job.options["_job_descendant_process_identities_v1"] = [
            {"pid": 65533, "identity": "darwin:recorded-start"}
        ]
        JobManager(config)._persist_job(job)
        monkeypatch.setattr("patchbay.jobs.cleanup_recovery._pid_live", lambda pid: pid == 65533)
    elif blocker == "bad_proof":
        proof.write_text("patchbay-supervisor-cleanup-unproven-v2:999:998\n", encoding="ascii")
    elif blocker == "lock_busy":
        def busy(_self, *_args, **_kwargs):
            raise RepoMutationBusy(job.repo_path, "operator_cleanup_recovery")
        monkeypatch.setattr(RepoMutationLockManager, "hold_sync", busy)
    before = _state_bytes(config, job.job_id)
    with pytest.raises(ValueError, match=expected):
        recover_cleanup(
            config,
            job.job_id,
            operator="fixture-operator",
            apply=True,
            confirmation=CONFIRMATION,
        )
    assert _state_bytes(config, job.job_id) == before
    assert list(Path(config["logging"]["job_logs_dir"]).glob("*.operator-cleanup-recovery.*")) == []


def test_recovery_receipt_rejects_bad_mode_or_changed_supervisor_proof(tmp_path, monkeypatch, quiescent_census):
    config, job, proof = _fixture(tmp_path)
    result = recover_cleanup(
        config,
        job.job_id,
        operator="fixture-operator",
        apply=True,
        confirmation=CONFIRMATION,
    )
    assert result["applied"] is True
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    assert executor._supervisor_cleanup_proven(job.job_id)

    receipt = Path(manager.get_job(job.job_id).options["_job_supervisor_operator_recovery_receipt"])
    receipt.chmod(0o644)
    assert not executor._supervisor_cleanup_proven(job.job_id)
    receipt.chmod(0o600)
    recovery_data = json.loads(receipt.read_text(encoding="utf-8"))
    recovery_data["operator_confirmation"] = "operator says ok"
    receipt.write_text(json.dumps(recovery_data), encoding="utf-8")
    receipt.chmod(0o600)
    assert not executor._supervisor_cleanup_proven(job.job_id)
    recovery_data["operator_confirmation"] = CONFIRMATION
    receipt.write_text(json.dumps(recovery_data), encoding="utf-8")
    receipt.chmod(0o600)
    proof.write_text("patchbay-supervisor-cleanup-unproven-v2:65531:65532\nchanged", encoding="ascii")
    assert not executor._supervisor_cleanup_proven(job.job_id)
