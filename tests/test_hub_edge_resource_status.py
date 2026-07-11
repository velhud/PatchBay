from types import SimpleNamespace

from patchbay.hub import edge
from patchbay.hub.runtime import HubRuntime


def test_full_history_projection_counts_only_active_workers(monkeypatch, tmp_path):
    monkeypatch.setattr(edge, "_disk_telemetry_path", lambda config: tmp_path)
    status = edge.build_resource_status(
        {"server": {"max_concurrent_jobs": 25}},
        {
            "workers": [
                {"turn_state": "idle"},
                {"turn_state": "completed"},
                {"turn_state": "working"},
                {"state": "starting"},
                {"turn_state": "working", "liveness": "lost"},
                {"turn_state": "working", "liveness": "terminal"},
            ]
        },
    )

    assert status["active_workers"] == 2
    assert status["free_worker_slots"] == 23


def test_wsl_virtual_disk_does_not_become_effective_free(monkeypatch, tmp_path):
    monkeypatch.setattr(edge, "_is_wsl", lambda: True)
    monkeypatch.setattr(edge, "_windows_host_disk_status", lambda: {})
    monkeypatch.setattr(edge, "_disk_telemetry_path", lambda config: tmp_path)
    monkeypatch.setattr(edge.shutil, "disk_usage", lambda path: SimpleNamespace(total=1_000, used=100, free=900))

    status = edge.build_resource_status({"server": {"max_concurrent_jobs": 4}}, {"active": 0})

    assert status["disk_filesystem_free_bytes"] == 900
    assert status["disk_telemetry_confidence"] == "virtualized"
    assert status["disk_telemetry_source"] == "wsl_virtual_filesystem"
    assert "disk_free_bytes" not in status
    assert "PATCHBAY_EDGE_DISK_FREE_BYTES" in status["disk_telemetry_warning"]


def test_wsl_plain_mnt_c_directory_is_not_host_disk(monkeypatch, tmp_path):
    mnt_c = tmp_path / "mnt" / "c"
    mnt_c.mkdir(parents=True)
    monkeypatch.setattr(edge, "_is_wsl", lambda: True)
    monkeypatch.setattr(edge, "_disk_telemetry_path", lambda config: tmp_path)
    monkeypatch.setattr(edge.shutil, "disk_usage", lambda path: SimpleNamespace(total=1_000, used=100, free=900))
    monkeypatch.setattr(edge, "Path", lambda value: mnt_c if value == "/mnt/c" else type(tmp_path)(value))
    monkeypatch.setattr(edge.os.path, "ismount", lambda path: False)

    status = edge.build_resource_status({"server": {"max_concurrent_jobs": 4}}, {"active": 0})

    assert status["disk_telemetry_confidence"] == "virtualized"
    assert "disk_free_bytes" not in status


def test_configured_disk_override_is_effective_disk_status(monkeypatch, tmp_path):
    monkeypatch.setattr(edge, "_is_wsl", lambda: True)
    monkeypatch.setattr(edge, "_windows_host_disk_status", lambda: {})
    monkeypatch.setattr(edge, "_disk_telemetry_path", lambda config: tmp_path)
    monkeypatch.setattr(edge.shutil, "disk_usage", lambda path: SimpleNamespace(total=1_000, used=100, free=900))

    status = edge.build_resource_status(
        {
            "server": {"max_concurrent_jobs": 4},
            "hub": {"edge": {"resource_overrides": {"disk_free_bytes": 250, "disk_total_bytes": 1_000}}},
        },
        {"active": 0},
    )

    assert status["disk_free_bytes"] == 250
    assert status["disk_total_bytes"] == 1_000
    assert status["disk_used_percent"] == 75.0
    assert status["disk_telemetry_confidence"] == "configured"


def test_wsl_host_disk_status_caps_virtual_filesystem_free(monkeypatch, tmp_path):
    monkeypatch.setattr(edge, "_is_wsl", lambda: True)
    monkeypatch.setattr(edge, "_is_windows_host_mount", lambda path: True)
    monkeypatch.setattr(edge, "_disk_telemetry_path", lambda config: tmp_path)
    monkeypatch.setattr(edge.shutil, "disk_usage", lambda path: SimpleNamespace(total=1_000, used=100, free=900))
    monkeypatch.setattr(
        edge,
        "_windows_host_disk_status",
        lambda: {
            "disk_host_free_bytes": 300,
            "disk_host_total_bytes": 600,
            "disk_host_used_percent": 50.0,
            "disk_host_source": "/mnt/c",
        },
    )

    status = edge.build_resource_status({"server": {"max_concurrent_jobs": 4}}, {"active": 0})

    assert status["disk_free_bytes"] == 300
    assert status["disk_total_bytes"] == 600
    assert status["disk_used_percent"] == 50.0
    assert status["disk_telemetry_confidence"] == "host"
    assert status["disk_filesystem_free_bytes"] == 900


def test_cpu_status_uses_proc_stat_delta_after_first_sample(monkeypatch):
    samples = iter([(1_000, 800), (1_100, 850)])
    monkeypatch.setattr(edge, "_LAST_CPU_SAMPLE", None)
    monkeypatch.setattr(edge, "_read_proc_stat_cpu", lambda: next(samples))
    monkeypatch.setattr(edge.os, "getloadavg", lambda: (0.5, 0.5, 0.5))
    monkeypatch.setattr(edge.os, "cpu_count", lambda: 2)

    first = edge._cpu_percent_status()
    second = edge._cpu_percent_status()

    assert first["cpu_telemetry_confidence"] == "pressure_estimate"
    assert second["cpu_percent"] == 50.0
    assert second["cpu_telemetry_source"] == "/proc/stat_delta"
    assert second["cpu_telemetry_confidence"] == "sampled"


def test_router_penalizes_virtualized_disk_confidence(tmp_path):
    config = {
        "hub": {
            "state_file": str(tmp_path / "hub-state.json"),
            "heartbeat_stale_seconds": 90,
            "routing": {
                "enabled": True,
                "min_disk_free_bytes": 1024,
                "allow_queue_when_full": False,
                "weights": {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20},
            },
        }
    }
    runtime = HubRuntime(config)
    for machine_id, confidence in (("normal", "filesystem"), ("wsl", "virtualized")):
        code = runtime.create_enrollment_code(name=machine_id)["code"]
        token = runtime.enroll_machine(code=code, machine_id=machine_id, display_name=machine_id)["node_token"]
        resources = {
            "active_workers": 0,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 4,
            "queue_enabled": True,
            "cpu_percent": 10,
            "memory_used_percent": 10,
            "memory_available_bytes": 8_000_000_000,
            "disk_used_percent": 20,
            "disk_telemetry_confidence": confidence,
        }
        if confidence != "virtualized":
            resources["disk_free_bytes"] = 10_000_000_000
        runtime.heartbeat(
            machine_id=machine_id,
            token=token,
            capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
            resource_status=resources,
        )

    result = runtime.recommend_machine()

    assert result["selected_machine_id"] == "normal"
    wsl = next(candidate for candidate in result["ranked_candidates"] if candidate["machine_id"] == "wsl")
    assert wsl["score_reasons"]["disk_telemetry_confidence"] == "virtualized"
    assert wsl["score_reasons"]["disk_penalty"] == 0.03


def test_router_distrusts_legacy_wsl_disk_free_without_confidence(tmp_path):
    config = {
        "hub": {
            "state_file": str(tmp_path / "hub-state.json"),
            "heartbeat_stale_seconds": 90,
            "routing": {
                "enabled": True,
                "min_disk_free_bytes": 1024,
                "allow_queue_when_full": False,
                "weights": {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20},
            },
        }
    }
    runtime = HubRuntime(config)
    code = runtime.create_enrollment_code(name="dell")["code"]
    token = runtime.enroll_machine(
        code=code,
        machine_id="dell",
        display_name="Dell",
        tags=["wsl2"],
        role="windows-wsl-worker-edge",
    )["node_token"]
    runtime.heartbeat(
        machine_id="dell",
        token=token,
        capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 4,
            "queue_enabled": True,
            "cpu_percent": 10,
            "memory_used_percent": 10,
            "memory_available_bytes": 8_000_000_000,
            "disk_free_bytes": 1_000_000_000_000,
            "disk_used_percent": 5,
        },
    )

    result = runtime.recommend_machine()

    candidate = result["ranked_candidates"][0]
    assert candidate["machine_id"] == "dell"
    assert candidate["disk_free_bytes"] is None
    assert candidate["score_reasons"]["disk_telemetry_confidence"] == "legacy_wsl_untrusted"
    assert "Legacy WSL edge" in candidate["disk_telemetry_warning"]
