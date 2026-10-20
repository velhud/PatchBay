"""Portable resource-admission and resource-evidence contract tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

from patchbay.authoring.execution import admission
from patchbay.authoring.execution import environment
from patchbay.authoring.execution.process import _empty_usage, _record_usage
from patchbay.authoring.execution.receipts import classify
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.models import Check, Profile
from patchbay.authoring.util import fingerprint


def _profile(*, cpu=.25, memory_mb=256, pids=16, disk_mb=64):
    return Profile(id="resource-test", backend="trusted-local", cpu=cpu, memory_mb=memory_mb,
                   pids=pids, disk_mb=disk_mb,
                   checks=[Check(id="unit", argv=["python", "-m", "unittest"])])


class _Blobs:
    def __init__(self, values):
        self.values = values

    def read(self, sha, *, limit):
        value = self.values[sha]
        if len(value) > limit:
            raise ValueError("fixture receipt is over read limit")
        return value


class _Store:
    def __init__(self, profile, receipts=()):
        self.home = "/fixture"
        self.limits = SimpleNamespace(max_jobs=4, cpu_cores=4.0, memory_mb=8192,
                                      minimum_free_bytes=128 * 1024**2)
        self._receipts = dict(receipts)
        self.blobs = _Blobs(self._receipts)
        self.profile_digest = fingerprint(profile.wire())

    def rows(self, sql, args=()):
        return [{"receipt_sha": sha} for sha in self._receipts]


def _capacity(*, cpu=2.0, memory=8 * 1024**3, pids=10000):
    return {"cpu_cores": cpu, "available_memory_bytes": memory,
            "memory_capacity_bytes": memory, "memory_current_bytes": 0,
            "cgroup_memory_remaining_bytes": memory, "cpu_quota": None,
            "cpu_source": "fixture", "memory_source": "fixture", "cgroup_observed": True,
            "pids_current": 0, "pids_capacity": pids}


def test_observed_cpu_admission_subtracts_all_active_reservations(monkeypatch):
    profile = _profile()
    active = [{"profile": json.dumps(_profile(cpu=.5).wire())}]
    store = _Store(profile)
    monkeypatch.setattr(admission, "host_capacity", lambda: _capacity(cpu=1.0))
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))

    admitted, details = admission.can_admit(store, profile, active)

    assert not admitted
    assert details["gates"]["observed_cpu"] is False
    assert details["control_reserve"]["cpu_cores"] == .5


def test_peak_receipts_raise_future_reservation_with_margin(monkeypatch):
    profile = _profile()
    receipt = {"resource_usage": {"observation_state": "observed", "samples": 1, "cpu_cores_peak": .8,
                                  "rss_bytes_peak": 500 * 1024**2, "pids_peak": 20,
                                  "disk_bytes_peak": 80 * 1024**2}}
    store = _Store(profile, [("receipt-1", json.dumps(receipt).encode())])
    monkeypatch.setattr(admission, "host_capacity", lambda: _capacity())
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))

    admitted, details = admission.can_admit(store, profile, [])

    assert admitted
    assert details["historical_peak_estimate"]["observations"] == 1
    assert details["reservation"]["cpu_cores"] == 1.0
    assert details["reservation"]["memory_bytes"] == int(625 * 1024**2)
    assert details["reservation"]["pids"] == 25
    assert details["reservation"]["disk_bytes"] == 100 * 1024**2


def test_oversized_history_receipt_is_skipped_but_corrupt_receipt_is_not(monkeypatch):
    profile = _profile()

    class BoundedBlobs:
        def __init__(self, error):
            self.error = error

        def read(self, _sha, *, limit):
            raise self.error

    store = _Store(profile, [("receipt", b"")])
    store.blobs = BoundedBlobs(AuthorError("RESOURCE_LIMIT", "fixture exceeds bounded history sample"))
    skipped = admission._history_estimates(store, profile)
    assert skipped["history_state"] == "no_qualifying_history"
    assert skipped["receipts_examined"] == 1
    assert skipped["observations"] == 0

    store.blobs = BoundedBlobs(AuthorError("CORRUPT_STATE", "fixture receipt hash mismatch"))
    try:
        admission._history_estimates(store, profile)
    except AuthorError as exc:
        assert exc.code == "CORRUPT_STATE"
    else:
        raise AssertionError("corrupt historical evidence must not be hidden as an empty sample")

    store.blobs = BoundedBlobs(None)
    store.blobs.read = lambda _sha, *, limit: b"{malformed receipt"
    try:
        admission._history_estimates(store, profile)
    except AuthorError as exc:
        assert exc.code == "CORRUPT_STATE"
    else:
        raise AssertionError("malformed persisted receipt JSON must not be skipped")


def test_measured_estimate_also_reserves_active_same_profile(monkeypatch):
    profile = _profile()
    receipt = {"resource_usage": {"observation_state": "observed", "samples": 1,
                                  "cpu_cores_peak": .6, "rss_bytes_peak": 0,
                                  "pids_peak": 0, "disk_bytes_peak": 0}}
    store = _Store(profile, [("receipt-1", json.dumps(receipt).encode())])
    monkeypatch.setattr(admission, "host_capacity", lambda: _capacity())
    monkeypatch.setattr(admission.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    active = [{"profile": json.dumps(profile.wire()), "profile_digest": fingerprint(profile.wire())}]

    admitted, details = admission.can_admit(store, profile, active)

    assert admitted
    assert details["active_reserved"]["cpu_cores"] == .75


def test_usage_keeps_unavailable_observations_explicit_and_updates_peaks():
    usage = _empty_usage()
    assert usage["observation_state"] == "unavailable"
    _record_usage(usage, cpu=.25, rss=1000, pids=3, method="fixture sampler")
    assert usage["observation_state"] == "partial"
    assert usage["unavailable"] == ["disk"]
    _record_usage(usage, cpu=.5, rss=700, pids=2, disk=4096, method="fixture sampler")

    assert usage["observation_state"] == "observed"
    assert usage["cpu_cores_peak"] == .5
    assert usage["rss_bytes_peak"] == 1000
    assert usage["pids_peak"] == 3
    assert usage["disk_bytes_peak"] == 4096
    assert usage["unavailable"] == []


def test_missing_tool_and_storage_fault_are_environment_blocks():
    for code in ("MISSING_EXECUTABLE", "DISK_EXHAUSTED", "OOM_KILLED"):
        receipt = {"setup": [], "checks": [], "cleanup": "proven", "error": {"code": code},
                   "source_changes_during_checks": [], "commit": "c", "tree": "t",
                   "profile_digest": "p", "environment_digest": "e"}
        classify(receipt, set())
        assert receipt["classification"] == "BLOCKED_ENVIRONMENT"
        assert receipt["failure_causes"][0]["code"] == code


def test_report_incomplete_and_source_change_causes_remain_distinct():
    receipt = {"setup": [], "checks": [{"id": "unit", "state": "completed", "required": True,
                                          "verification": "incomplete", "reason": "zero_tests",
                                          "failure_kind": "ZERO_TESTS"}],
               "cleanup": "proven", "source_changes_during_checks": [],
               "commit": "c", "tree": "t", "profile_digest": "p", "environment_digest": "e"}
    classify(receipt, {"unit"})
    assert receipt["classification"] == "CHECKS_INCOMPLETE"
    assert receipt["failure_causes"][0]["code"] == "ZERO_TESTS"

    receipt["source_changes_during_checks"] = [{"path": "app.py", "reason": "tracked_source_changed"}]
    classify(receipt, {"unit"})
    assert {cause["code"] for cause in receipt["failure_causes"]} == {
        "ZERO_TESTS", "SOURCE_CHANGED_DURING_CHECKS"}


def test_missing_trusted_local_executable_is_reported_before_launch(tmp_path):
    workspace, run_root = tmp_path / "workspace", tmp_path / "run"
    workspace.mkdir()
    run_root.mkdir()
    profile = _profile()
    check = Check(id="missing", argv=["patchbay-command-that-does-not-exist"])

    try:
        environment.command(profile, check, workspace, run_root,
                            container_name="pba-resource-0", stage="checks")
    except AuthorError as exc:
        assert exc.code == "MISSING_EXECUTABLE"
    else:
        raise AssertionError("missing command must fail before starting a check")


def test_docker_inspection_preserves_explicit_oom_observation(monkeypatch):
    class Result:
        returncode = 0
        stdout = b'{"OOMKilled":true,"ExitCode":137,"Status":"exited","Error":""}'
        stderr = b""

    monkeypatch.setattr(environment.subprocess, "run", lambda *args, **kwargs: Result())

    observed = environment.inspect_container("pba-resource-0")

    assert observed == {"state": "observed", "oom_killed": True, "exit_code": 137,
                       "status": "exited", "error": ""}
