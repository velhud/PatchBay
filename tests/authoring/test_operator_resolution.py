from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.identity import identity
from patchbay.authoring.operator_resolution import ATTESTATION, LEGACY_CONTINUITY_ATTESTATION, OperatorResolution
from patchbay.authoring.util import canonical
from patchbay.authoring.execution.runner import Checks


def _private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _fixture_unknown(setup_author, monkeypatch, *, docker_fault=None, native=False):
    monkeypatch.setattr(sys, "platform", "linux")
    service, _, _, task, lease = setup_author
    operation = Checks(service.store).submit(task["id"], revision=0, key="unknown-for-operator-resolution",
                                             token=lease["token"], epoch=lease["epoch"], dispatch=False)
    op_id = operation["id"]
    run_root = service.store.home / "runs" / op_id
    run_root.mkdir(mode=0o700)
    (run_root / "source").mkdir(mode=0o700)
    owner = {"pid": os.getpid(), "start": "12345", "boot": "00000000-0000-0000-0000-000000000001",
             "state": "S", "ppid": 1}
    image_id = "sha256:" + "a" * 64
    configured_image = image_id if native else "fixture-image:prepared"
    daemon_id = "fixture-daemon-id"
    receipt = {"format_version": 1, "operation_id": op_id, "state": "unknown_execution",
               "classification": "EXECUTION_UNKNOWN", "cleanup": "unresolved",
               "environment": {"backend": "docker", "image_id": image_id}}
    receipt_sha = service.store.blobs.put(canonical(receipt))
    original_result = canonical({"classification": "EXECUTION_UNKNOWN", "receipt_sha": receipt_sha,
                                 "cleanup": "unresolved", "message": "historical unknown"}).decode()
    original_error = canonical({"code": "EXECUTION_UNKNOWN", "message": "historical unknown"}).decode()
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='unknown_execution',cleanup='unresolved',receipt_sha=?,profile=?,updated=? WHERE id=?",
                     (receipt_sha, canonical({"backend": "docker", "image": configured_image, "setup": [],
                                             "checks": [{"id": "unit", "argv": ["/bin/sh", "-c", "true"]}]}).decode(),
                      operation["created"], op_id))
        conn.execute("UPDATE operations SET state='unknown_execution',phase='ownership_unresolved',result=?,error=?,owner_identity=?,payload=? WHERE id=?",
                     (original_result, original_error, canonical(owner).decode(),
                      canonical({"selected": ["unit"]}).decode(), op_id))

    container_id = "b" * 64
    container_name = "pba-" + op_id.replace("_", "-") + "-0"
    argv = ["/bin/sh", "-c", "true"]
    raw_docker_evidence = {
        "inspect": {"id": container_id, "name": "/" + container_name, "image": image_id,
                    "cmd": argv, "mounts": [{"type": "bind", "source": str((run_root / "source").resolve()),
                                               "destination": "/workspace"}]},
        "create_event": {"Type": "container", "Action": "create",
                         "Actor": {"ID": container_id, "Attributes": {"name": container_name, "image": image_id}}},
        "destroy_event": {"Type": "container", "Action": "destroy",
                          "Actor": {"ID": container_id, "Attributes": {"name": container_name, "image": image_id}}},
    }
    evidence = {}
    for key, value in raw_docker_evidence.items():
        payload = canonical(value)
        evidence_path = run_root / (key + ".json")
        _private(evidence_path, payload)
        evidence[key] = {"path": str(evidence_path), "sha256": hashlib.sha256(payload).hexdigest()}
    process_identity = {"pid": owner["pid"], "process_group_id": owner["pid"],
                        "start_time_ticks": owner["start"], "boot_id": owner["boot"]}
    start_identity = {"pid": owner["pid"], "start": owner["start"], "boot": owner["boot"],
                      "state": owner["state"], "ppid": owner["ppid"]}
    launch_raw = canonical({"operation_id": op_id, "supervisor": owner})
    child_raw = canonical({"operation_id": op_id, "process": start_identity,
                           "container": container_name})
    launch_path, child_path = run_root / "launch.json", run_root / "child.json"
    _private(launch_path, launch_raw)
    _private(child_path, child_raw)
    continuity = {}
    docker_endpoint = "unix:///run/patchbay-test-docker.sock"
    old_service = {"pid": 999998, "boot_id": owner["boot"], "unit_invocation_id": "old-invocation"}
    current_service = {"pid": 999999, "boot_id": owner["boot"], "process_group_id": 999999,
                       "start_time_ticks": "77777"}
    engine_id_file_bytes = daemon_id.encode()
    engine_id_file_sha = hashlib.sha256(engine_id_file_bytes).hexdigest()
    old_engine = {"engine_id_file_sha256": engine_id_file_sha,
                  "data_root": "/data", "exec_root": "/run/docker",
                  "socket": "/run/patchbay-test-docker.sock", "config_sha256": "c" * 64, "daemon_flags": ["--live-restore=false"]}
    current_engine = {**old_engine}
    current_engine["daemon_id"] = daemon_id
    old_stop = {"state": "inactive", "identity": old_service, "stopped_at": "2026-10-21T00:00:00Z"}
    current_start = {"state": "active", "identity": current_service, "started_at": "2026-10-21T00:01:00Z"}
    for key, payload in (("old_engine", canonical(old_engine)), ("current_engine", canonical(current_engine)),
                         ("old_service_stop", canonical(old_stop)),
                         ("current_service_start", canonical(current_start)),
                         ("engine_id_file", engine_id_file_bytes)):
        evidence_path = run_root / ("continuity-" + key + ".json")
        _private(evidence_path, payload)
        continuity[key] = {"path": str(evidence_path), "sha256": hashlib.sha256(payload).hexdigest()}
    observation = {
        "format_version": 1, "provenance": "operator_observed_legacy", "operation_id": op_id,
        "original_receipt_sha": receipt_sha, "no_inflight_create_request_attested": True,
        "operator": "fixture operator", "attested_at": "2026-10-21T00:00:00Z",
        "attestation_evidence": {
            "launch": {"path": str(launch_path), "sha256": hashlib.sha256(launch_raw).hexdigest()},
            "child": {"path": str(child_path), "sha256": hashlib.sha256(child_raw).hexdigest()},
        },
        "daemon": {"endpoint": docker_endpoint, "daemon_id": daemon_id,
                   "old_daemon_id": None, "same_private_engine": True,
                   "continuity_basis": "operator_attested_restart_same_private_engine"},
        "continuity_evidence": continuity,
        "old_service_identity": old_service,
        "current_service_identity": current_service,
        "old_service_stopped_at": "2026-10-21T00:00:00Z",
        "current_service_started_at": "2026-10-21T00:01:00Z",
        "persistent_engine_id_source": {"source_path": "/private/docker/data/engine-id",
                                         "owner_uid": 0, "mode": 0o600,
                                         "mtime": operation["created"] - 1, "daemon_id": daemon_id},
        "process_identity": {"supervisor": owner, "create_client": process_identity,
                             "start_client": process_identity},
        "containers": [{"sequence": 0, "container_name": container_name,
                        "container_id": container_id, "image_id": image_id,
                        "configured_image": configured_image, "source_mount_path": str((run_root / "source").resolve()),
                        "stage": "checks", "check_id": "unit", "command": ["/bin/sh", "-c", "true"], "daemon_id": daemon_id,
                        "events": [{"action": "create", "container_id": container_id,
                                    "container_name": container_name, "image_id": image_id, "daemon_id": None},
                                   {"action": "destroy", "container_id": container_id,
                                    "container_name": container_name, "image_id": image_id, "daemon_id": None}],
                        "evidence": evidence}],
    }
    if native:
        from patchbay.authoring.execution.process import _persist_lifecycle
        with service.store.transaction() as conn:
            conn.execute("UPDATE runs SET profile=? WHERE id=?",
                         (canonical({"backend": "docker", "image": image_id, "setup": [],
                                     "checks": [{"id": "unit", "argv": argv}]}).decode(), op_id))
        raw_identity = {"pid": owner["pid"], "start": owner["start"], "boot": owner["boot"],
                        "state": owner["state"], "ppid": owner["ppid"]}
        lifecycle_container = {
            "sequence": 0, "container_name": container_name, "container_id": container_id,
            "daemon_id": daemon_id, "image_id": image_id, "configured_image": image_id,
            "command": argv, "source_mount_path": str((run_root / "source").resolve()),
            "state": "cleanup_unresolved",
            "stages": [
                {"name": "create", "state": "exited", "identity": raw_identity,
                 "exit_code": 0, "signal": None, "started_at": 1, "finished_at": 2},
                {"name": "start", "state": "exited", "identity": raw_identity,
                 "exit_code": 0, "signal": None, "started_at": 2, "finished_at": 3},
            ],
            "cleanup": {"state": "unresolved", "container_id": container_id,
                        "rm_exit_code": None, "absent_confirmations": 0, "last_error": "fixture"},
        }
        _persist_lifecycle(run_root, op_id, lifecycle_container)
        _private(run_root / f"{container_name}.cid", (container_id + "\n").encode())
    else:
        _private(run_root / "legacy-docker-observation.json", canonical(observation))

    monkeypatch.setattr("patchbay.authoring.operator_resolution._read_proc",
                        lambda pid: {"pid": pid, "state": "S", "ppid": 1, "pgrp": pid,
                                     "start_time_ticks": "77777"} if pid == current_service["pid"] else None)
    monkeypatch.setattr("patchbay.authoring.operator_resolution.sys.platform", "linux")
    monkeypatch.setattr("patchbay.authoring.operator_resolution._current_boot_id", lambda: owner["boot"])
    monkeypatch.setattr("patchbay.authoring.operator_resolution._assert_groups_quiescent",
                        lambda groups: {"groups_censused": sorted(groups), "active_groups": []})

    def docker(args, **kwargs):
        if args[1:3] == ["info", "--format"]:
            status = 1 if docker_fault == "daemon" else 0
            return subprocess.CompletedProcess(args, status,
                                               (daemon_id if status == 0 else "").encode(), b"")
        if args[1:3] == ["container", "ls"]:
            assert args[args.index("--filter") + 1] == "id=" + container_id
            status = 1 if docker_fault == "inventory" else 0
            listed = b"c" * 64 + b"\n" if docker_fault == "wrong-exact-id" else b""
            return subprocess.CompletedProcess(args, status, listed, b"unavailable" if status else b"")
        raise AssertionError("Resolver must not mutate Docker or inspect by a negative-error string")
    monkeypatch.setattr("patchbay.authoring.operator_resolution.subprocess.run", docker)
    return service, task, lease, operation, run_root, receipt_sha, original_result, original_error


def test_legacy_resolution_preserves_unknown_receipt_and_releases_only_admission_fence(setup_author, monkeypatch):
    service, task, lease, operation, run_root, receipt_sha, original_result, original_error = _fixture_unknown(
        setup_author, monkeypatch)
    resolver = OperatorResolution(service.store)
    reason = "Close admission after independently observed process and container quiescence"
    dry_run = resolver.resolve(operation["id"], operator="Named operator", reason=reason)
    assert dry_run["ready"] and not dry_run["applied"]
    assert dry_run["evidence_source"] == "operator_observed_legacy"
    result = resolver.resolve(operation["id"], operator="Named operator",
                              reason=reason, apply=True, attestation=LEGACY_CONTINUITY_ATTESTATION)
    assert result["applied"] and result["execution_outcome"] == "unknown"
    assert result["historical_classification"] == "EXECUTION_UNKNOWN"
    current = service.store.one("SELECT * FROM operations WHERE id=?", (operation["id"],))
    run = service.store.one("SELECT * FROM runs WHERE id=?", (operation["id"],))
    assert current["state"] == "blocked" and current["phase"] == "operator_resolved"
    assert current["result"] == original_result and current["error"] == original_error
    assert run["state"] == "blocked" and run["cleanup"] == "operator_attested"
    receipt_after = service.store.blobs.read(receipt_sha)
    assert hashlib.sha256(receipt_after).hexdigest() == receipt_sha
    assert json.loads(receipt_after)["classification"] == "EXECUTION_UNKNOWN"
    replay = resolver.resolve(operation["id"], operator="Named operator",
                              reason=reason, apply=True, attestation=LEGACY_CONTINUITY_ATTESTATION)
    assert replay["idempotent_replay"] and replay["audit_sha256"] == result["audit_sha256"]
    next_run = Checks(service.store).submit(task["id"], revision=0, key="after-operator-resolution",
                                            token=lease["token"], epoch=lease["epoch"], dispatch=False)
    assert next_run["state"] == "queued"
    audit = json.loads((run_root / "operator-resolution.json").read_text())
    assert audit["original_receipt_sha"] == receipt_sha and audit["execution_outcome"] == "unknown"
    assert audit["evidence_source"] == "operator_observed_legacy"
    assert audit["resolution_reason"] == reason


def test_native_resolution_uses_persisted_lifecycle_and_actual_cidfile_name(setup_author, monkeypatch):
    service, _, _, operation, run_root, receipt_sha, original_result, original_error = _fixture_unknown(
        setup_author, monkeypatch, native=True)
    result = OperatorResolution(service.store).resolve(
        operation["id"], operator="Codex", reason="Resolve after independent quiescence review",
        apply=True, attestation=ATTESTATION)
    assert result["applied"] and result["execution_outcome"] == "unknown"
    current = service.store.one("SELECT * FROM operations WHERE id=?", (operation["id"],))
    run = service.store.one("SELECT * FROM runs WHERE id=?", (operation["id"],))
    assert current["state"] == "blocked" and current["result"] == original_result and current["error"] == original_error
    assert run["state"] == "blocked" and run["cleanup"] == "operator_attested"
    assert hashlib.sha256(service.store.blobs.read(receipt_sha)).hexdigest() == receipt_sha
    audit = json.loads((run_root / "operator-resolution.json").read_text())
    assert audit["evidence_source"] == "native_lifecycle"


def test_resolution_preserves_a_missing_historical_error_as_null(setup_author, monkeypatch):
    service, _, _, operation, run_root, _, original_result, _ = _fixture_unknown(setup_author, monkeypatch)
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET error=NULL WHERE id=?", (operation["id"],))
    result = OperatorResolution(service.store).resolve(
        operation["id"], operator="Codex", reason="Resolve only admission after exact quiescence",
        apply=True, attestation=LEGACY_CONTINUITY_ATTESTATION)
    current = service.store.one("SELECT result,error FROM operations WHERE id=?", (operation["id"],))
    assert result["applied"]
    assert current["result"] == original_result and current["error"] is None
    audit = json.loads((run_root / "operator-resolution.json").read_text())
    assert audit["original_operation_error"] is None


def test_legacy_resolution_fails_closed_on_daemon_query_uncertainty(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch, docker_fault="daemon")
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "DOCKER_QUERY_UNKNOWN"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_legacy_resolution_fails_closed_on_exact_inventory_failure(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch, docker_fault="inventory")
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "DOCKER_QUERY_UNKNOWN"
    assert service.store.one("SELECT cleanup FROM runs WHERE id=?", (operation["id"],))["cleanup"] == "unresolved"


def test_legacy_resolution_rejects_nonmatching_filtered_container_id(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(
        setup_author, monkeypatch, docker_fault="wrong-exact-id")
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "DOCKER_QUERY_UNKNOWN"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_legacy_resolution_requires_exact_operator_attestation(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review", apply=True,
                                                  attestation="I think it is gone")
    assert error.value.code == "INVALID_REQUEST"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_legacy_resolution_rejects_live_recorded_supervisor(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    owner = json.loads(service.store.one("SELECT owner_identity FROM operations WHERE id=?", (operation["id"],))["owner_identity"])
    monkeypatch.setattr("patchbay.authoring.operator_resolution._read_proc",
                        lambda pid: {"pid": pid, "state": "S", "ppid": 1, "pgrp": pid,
                                     "start_time_ticks": owner["start"]})
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EXECUTION_STILL_ACTIVE"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_legacy_resolution_rejects_recycled_supervisor_pid(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    owner = json.loads(service.store.one("SELECT owner_identity FROM operations WHERE id=?", (operation["id"],))["owner_identity"])
    monkeypatch.setattr("patchbay.authoring.operator_resolution._read_proc",
                        lambda pid: {"pid": pid, "state": "S", "ppid": 1, "pgrp": pid,
                                     "start_time_ticks": "999999"})
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EXECUTION_STILL_ACTIVE"
    assert owner["start"] != "999999"


def test_legacy_resolution_rejects_live_recorded_create_client(setup_author, monkeypatch):
    service, _, _, operation, run_root, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    observation = json.loads((run_root / "legacy-docker-observation.json").read_text())
    client = observation["process_identity"]["create_client"]
    current_service = observation["current_service_identity"]

    def proc(pid):
        if pid == client["pid"]:
            return {"pid": pid, "state": "S", "ppid": 1, "pgrp": pid,
                    "start_time_ticks": client.get("start_time_ticks", client.get("start"))}
        if pid == current_service["pid"]:
            return {"pid": pid, "state": "S", "ppid": 1, "pgrp": pid,
                    "start_time_ticks": current_service["start_time_ticks"]}
        return None

    monkeypatch.setattr("patchbay.authoring.operator_resolution._read_proc", proc)
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EXECUTION_STILL_ACTIVE"
    assert service.store.one("SELECT cleanup FROM runs WHERE id=?", (operation["id"],))["cleanup"] == "unresolved"


def test_legacy_resolution_rejects_inspect_evidence_for_another_mount(setup_author, monkeypatch):
    service, _, _, operation, run_root, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    observation_path = run_root / "legacy-docker-observation.json"
    observation = json.loads(observation_path.read_text())
    inspect_ref = observation["containers"][0]["evidence"]["inspect"]
    inspect_path = Path(inspect_ref["path"])
    inspect = json.loads(inspect_path.read_text())
    inspect["mounts"][0]["source"] = "/outside/unrelated"
    inspect_raw = canonical(inspect)
    _private(inspect_path, inspect_raw)
    inspect_ref["sha256"] = hashlib.sha256(inspect_raw).hexdigest()
    _private(observation_path, canonical(observation))
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EVIDENCE_INVALID"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_legacy_resolution_rejects_unbound_observation_and_active_peer(setup_author, monkeypatch):
    service, _, lease, operation, run_root, receipt_sha, _, _ = _fixture_unknown(setup_author, monkeypatch)
    path = run_root / "legacy-docker-observation.json"
    observation = json.loads(path.read_text())
    observation["original_receipt_sha"] = "0" * 64
    _private(path, canonical(observation))
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EVIDENCE_INVALID"
    # Restore the fixture, then introduce a separately recorded unresolved peer.
    observation["original_receipt_sha"] = receipt_sha
    _private(path, canonical(observation))
    peer = Checks(service.store).submit(service.store.one("SELECT task_id FROM runs WHERE id=?", (operation["id"],))["task_id"],
                                        revision=0, key="unresolved-peer", token=lease["token"], epoch=lease["epoch"],
                                        dispatch=False)
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='unknown_execution',cleanup='unresolved' WHERE id=?", (peer["id"],))
    with pytest.raises(AuthorError) as error:
        OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    assert error.value.code == "EXECUTION_UNKNOWN"


def test_legacy_resolution_respects_busy_maintenance_lock(setup_author, monkeypatch):
    from patchbay.authoring.persistence.locks import file_lock
    import time

    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def holder():
        with file_lock(service.store.home / "locks/maintenance"):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(2)
    try:
        with pytest.raises(AuthorError) as error:
            OperatorResolution(service.store).resolve(operation["id"], operator="Codex", reason="Quiescence review")
    finally:
        release.set()
        thread.join(timeout=3)
    assert error.value.code == "MAINTENANCE_BLOCKED"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"


def test_resolution_cli_uses_only_store_and_local_resolver(setup_author, monkeypatch):
    service, _, _, operation, _, _, _, _ = _fixture_unknown(setup_author, monkeypatch)
    import patchbay.authoring.interfaces.cli as cli

    def forbidden_service(*args, **kwargs):
        raise AssertionError("The local recovery command must not initialize AuthorService")

    monkeypatch.setattr(cli, "AuthorService", forbidden_service)
    args = cli.parser().parse_args([
        "--home", str(service.store.home), "resolve-unknown-execution", operation["id"],
        "--operator", "Named operator",
        "--reason", "Confirm no executor or exact owned container remains",
    ])
    result = cli.run(args)
    assert result["ready"] and result["evidence_source"] == "operator_observed_legacy"

    args.expected_instance = "instance_wrong_deployment"
    with pytest.raises(AuthorError) as error:
        cli.run(args)
    assert error.value.code == "INSTANCE_MISMATCH"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (operation["id"],))["state"] == "unknown_execution"
