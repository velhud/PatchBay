from __future__ import annotations

from patchbay.jobs.executor import JobExecutor


def _executor(monkeypatch, snapshots, *, pid_exists=True):
    executor = object.__new__(JobExecutor)
    calls = []

    def rows(*, include_environment, pid=None):
        calls.append((include_environment, pid))
        return snapshots.pop(0)

    monkeypatch.setattr(executor, "_portable_process_rows", rows)
    monkeypatch.setattr(executor, "_portable_pid_exists", lambda _pid: pid_exists)
    return executor, calls


def test_marker_scan_rechecks_only_transient_argv_mismatch_without_losing_marker(
    monkeypatch,
):
    job_id = "job-transient-argv"
    marker = "PATCHBAY_JOB_MARKER=" + JobExecutor._job_process_marker(job_id)
    snapshots = [
        {41: ("S", "/bin/old-command")},
        {41: ("S", "/bin/new-command")},
        {41: ("S", "/bin/new-command")},
        {41: ("S", f"/bin/new-command {marker}")},
    ]
    executor, calls = _executor(monkeypatch, snapshots)

    assert executor._job_marked_process_pids_from_ps(job_id) == {41}
    assert calls == [
        (False, None),
        (True, None),
        (False, 41),
        (True, 41),
    ]


def test_marker_scan_stays_unknown_when_pid_specific_rechecks_remain_inconsistent(
    monkeypatch,
):
    job_id = "job-unknown-argv"
    snapshots = [
        {42: ("S", "/bin/first")},
        {42: ("S", "/bin/second")},
        {42: ("S", "/bin/third")},
        {42: ("S", "/bin/fourth")},
        {42: ("S", "/bin/fifth")},
        {42: ("S", "/bin/sixth")},
    ]
    executor, _calls = _executor(monkeypatch, snapshots)

    assert executor._job_marked_process_pids_from_ps(job_id) is None


def test_marker_scan_resolves_disappeared_pid_without_claiming_marker(
    monkeypatch,
):
    job_id = "job-disappeared-process"
    snapshots = [
        {43: ("S", "/bin/process")},
        {},
        {},
        {},
        {},
        {},
    ]
    executor, _calls = _executor(monkeypatch, snapshots, pid_exists=False)

    assert executor._job_marked_process_pids_from_ps(job_id) == set()
