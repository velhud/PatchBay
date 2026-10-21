"""Local, audited closure of an exact Author execution uncertainty.

This module is intentionally not wired into HTTP or MCP. It never signals a
process or removes a container. An operator may release only the admission
fence after independent host-process and Docker-daemon observations prove the
recorded executor and every acknowledged container are quiescent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import AuthorError, require
from .persistence.locks import file_lock
from .persistence.store import Store, TERMINAL
from .util import canonical, fingerprint, safe_id, strict_json, utc_now

ATTESTATION = (
    "I independently verified that no executor or Docker client for this operation is active, "
    "no in-flight Docker create request remains, every exact owned container is absent from a "
    "successful Docker daemon query, and no detached process for this operation remains active. "
    "I understand execution outcome remains unknown."
)
LEGACY_CONTINUITY_ATTESTATION = (
    "For this legacy run, I independently verified continuity of the same configured private Docker engine "
    "across its recorded restart from the exact unit, configuration, socket, data-root, exec-root, flags, "
    "old service identity and stop time, and current service identity and start time evidence. I verified "
    "the old service and its clients are no longer active and no old Docker create request remains in flight. "
    "The historical daemon ID is taken from the private persistent engine-ID record created before the run, "
    "not from a historical docker-info response; when that ID is unavailable, I explicitly attest its absence. "
    "The current daemon inventory is a fresh successful query and the exact recorded container ID is absent. "
    "This remains an operator-attested legacy continuity boundary. I understand "
    "the original execution outcome remains unknown."
)
_LIFECYCLE_NAME = "docker-lifecycle.json"
_LEGACY_OBSERVATION_NAME = "legacy-docker-observation.json"
_AUDIT_NAME = "operator-resolution.json"
_MAX_LIFECYCLE_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 32 * 1024**2
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def _private_directory(path: Path, *, mode: int = 0o700) -> os.stat_result:
    require(not path.is_symlink(), "CORRUPT_STATE", "Private execution state cannot be a symlink")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == mode,
            "CORRUPT_STATE", "Private execution directory ownership or permissions are invalid")
    return info


def _private_file(path: Path, *, maximum: int) -> tuple[os.stat_result, bytes]:
    require(not path.is_symlink(), "CORRUPT_STATE", "Private execution evidence cannot be a symlink")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= maximum,
            "CORRUPT_STATE", "Private execution evidence ownership, permissions, or size are invalid")
    raw = path.read_bytes()
    require(len(raw) == info.st_size, "CORRUPT_STATE", "Private execution evidence changed while reading")
    return info, raw


def _read_proc(pid: int) -> dict[str, Any] | None:
    """Read a process identity without treating permission or parse errors as absence."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthorError("EXECUTION_UNKNOWN", "A recorded process identity could not be inspected") from exc
    try:
        fields = raw[raw.rfind(")") + 2 :].split()
        require(len(fields) > 19, "EXECUTION_UNKNOWN", "A process identity record is incomplete")
        return {
            "pid": pid,
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "start_time_ticks": fields[19],
        }
    except (ValueError, IndexError) as exc:
        raise AuthorError("EXECUTION_UNKNOWN", "A process identity record is malformed") from exc


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise AuthorError("EXECUTION_UNKNOWN", "Current host boot identity is unavailable") from exc
    require(bool(_BOOT_ID.fullmatch(value)), "EXECUTION_UNKNOWN", "Current host boot identity is malformed")
    return value


def _validate_process_identity(value: Any, *, label: str) -> dict[str, Any]:
    require(isinstance(value, dict), "CORRUPT_STATE", f"{label} identity is missing")
    try:
        pid = value.get("pid")
        group = value.get("process_group_id")
        start = str(value.get("start_time_ticks") or "")
        boot = str(value.get("boot_id") or "")
        require(type(pid) is int and pid > 0 and type(group) is int and group > 0
                and start.isdecimal() and bool(_BOOT_ID.fullmatch(boot)),
                "CORRUPT_STATE", f"{label} identity is incomplete")
    except (AttributeError, TypeError) as exc:
        raise AuthorError("CORRUPT_STATE", f"{label} identity is malformed") from exc
    return {"pid": pid, "process_group_id": group, "start_time_ticks": start, "boot_id": boot}


def _owner_identity(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), "CORRUPT_STATE", "Durable executor identity is missing")
    try:
        pid = value.get("pid")
        start = str(value.get("start") or "")
        boot = str(value.get("boot") or "")
        require(type(pid) is int and pid > 0 and start.isdecimal()
                and bool(_BOOT_ID.fullmatch(boot)),
                "CORRUPT_STATE", "Durable executor identity is incomplete")
    except (AttributeError, TypeError) as exc:
        raise AuthorError("CORRUPT_STATE", "Durable executor identity is malformed") from exc
    # Checks are launched with start_new_session=True, so the supervisor PID is
    # its process-group ID. Keep this exact launch contract local and explicit.
    return {"pid": pid, "process_group_id": pid, "start_time_ticks": start, "boot_id": boot}


def _assert_identity_absent(identity: dict[str, Any], *, label: str) -> dict[str, Any]:
    if identity["boot_id"] != _current_boot_id():
        return {**identity, "presence": "recorded_boot_ended"}
    current = _read_proc(identity["pid"])
    if current is None:
        return {**identity, "presence": "absent"}
    if current["state"] in {"Z", "X"}:
        return {**identity, "presence": "zombie_nonrunning"}
    if (current["start_time_ticks"] == identity["start_time_ticks"]):
        raise AuthorError("EXECUTION_STILL_ACTIVE", f"{label} process identity is still active")
    raise AuthorError("EXECUTION_STILL_ACTIVE", f"{label} PID is occupied by a live or recycled process")


def _assert_identity_present(identity: dict[str, Any], *, label: str) -> dict[str, Any]:
    require(identity["boot_id"] == _current_boot_id(), "EXECUTION_UNKNOWN",
            f"{label} was recorded on a different host boot")
    current = _read_proc(identity["pid"])
    if current is None or current["state"] in {"Z", "X"}:
        raise AuthorError("EXECUTION_UNKNOWN", f"{label} process identity is not currently observable")
    require(current["start_time_ticks"] == identity["start_time_ticks"],
            "EXECUTION_UNKNOWN", f"{label} PID does not match the recorded current service identity")
    return {**identity, "presence": "exact_live_identity"}


def _assert_pid_unoccupied(identity: dict[str, Any], *, label: str) -> dict[str, Any]:
    pid = identity["pid"]
    if identity["boot_id"] != _current_boot_id():
        return {**identity, "presence": "recorded_boot_ended"}
    current = _read_proc(pid)
    require(current is None, "EXECUTION_STILL_ACTIVE",
            f"{label} PID is occupied; no exact process-start identity is available to distinguish reuse")
    return {"pid": pid, "presence": "unoccupied"}


def _legacy_service_identity(value: Any, *, label: str, current: bool) -> dict[str, Any]:
    require(isinstance(value, dict), "EVIDENCE_INVALID", f"{label} service identity is missing")
    pid, boot = value.get("pid"), value.get("boot_id") or value.get("boot")
    require(type(pid) is int and pid > 0 and isinstance(boot, str)
            and _BOOT_ID.fullmatch(boot), "EVIDENCE_INVALID", f"{label} service PID/boot identity is malformed")
    if current:
        identity = {"pid": pid, "process_group_id": value.get("process_group_id"),
                    "start_time_ticks": str(value.get("start_time_ticks") or value.get("start") or ""),
                    "boot_id": boot}
        return _validate_process_identity(identity, label=label)
    require(isinstance(value.get("unit_invocation_id"), str) and value["unit_invocation_id"],
            "EVIDENCE_INVALID", f"{label} service unit invocation identity is missing")
    start = str(value.get("start_time_ticks") or value.get("start") or "")
    if start:
        identity = {"pid": pid, "process_group_id": value.get("process_group_id"),
                    "start_time_ticks": start, "boot_id": boot}
        return _validate_process_identity(identity, label=label)
    return {"pid": pid, "boot_id": boot, "unit_invocation_id": value["unit_invocation_id"],
            "presence_check": "pid_unoccupied"}


def _legacy_client_identity(value: Any, *, label: str) -> dict[str, Any]:
    """Bind old Popen records; every Author check client starts a new session."""
    require(isinstance(value, dict), "EVIDENCE_INVALID", f"{label} process identity is missing")
    identity = {
        "pid": value.get("pid"),
        "process_group_id": value.get("process_group_id") or value.get("pid"),
        "start_time_ticks": str(value.get("start_time_ticks") or value.get("start") or ""),
        "boot_id": value.get("boot_id") or value.get("boot"),
    }
    return _validate_process_identity(identity, label=label)


def _lifecycle_client_identity(value: Any, *, label: str) -> dict[str, Any]:
    """Normalize the exact raw identities written by execution.identity.identity()."""
    require(isinstance(value, dict), "EXECUTION_UNKNOWN", f"{label} process identity is unavailable")
    identity = {
        "pid": value.get("pid"),
        "process_group_id": value.get("process_group_id") or value.get("pid"),
        "start_time_ticks": str(value.get("start_time_ticks") or value.get("start") or ""),
        "boot_id": value.get("boot_id") or value.get("boot"),
    }
    return _validate_process_identity(identity, label=label)


def _assert_groups_quiescent(groups: set[int]) -> dict[str, Any]:
    """Census exact process groups, accepting zombie-only groups as non-running."""
    require(sys.platform == "linux", "BLOCKED_ENVIRONMENT", "Operator resolution requires the Linux execution host")
    try:
        entries = list(os.scandir("/proc"))
    except OSError as exc:
        raise AuthorError("EXECUTION_UNKNOWN", "Host process census is unavailable") from exc
    members: dict[int, list[dict[str, Any]]] = {group: [] for group in groups}
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            raw = Path(entry.path, "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuthorError("EXECUTION_UNKNOWN", "Host process census is incomplete") from exc
        try:
            fields = raw[raw.rfind(")") + 2 :].split()
            require(len(fields) > 19, "EXECUTION_UNKNOWN", "Host process census found an incomplete identity")
            state, group, start = fields[0], int(fields[2]), fields[19]
        except (ValueError, IndexError) as exc:
            raise AuthorError("EXECUTION_UNKNOWN", "Host process census found a malformed identity") from exc
        if group in members:
            members[group].append({"pid": pid, "state": state, "start_time_ticks": start})
    active = {
        str(group): [item for item in records if item["state"] not in {"Z", "X"}]
        for group, records in members.items()
    }
    active = {group: records for group, records in active.items() if records}
    require(not active, "EXECUTION_STILL_ACTIVE", "An owned supervisor or Docker-client process group is still active")
    return {"groups_censused": sorted(groups), "active_groups": []}


def _docker_env(run_root: Path) -> dict[str, str]:
    # Match the environment used by Author's Docker execution command. No
    # task-provided env or credential-bearing inherited variables are used.
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(run_root / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _docker_container_absent(container: dict[str, Any], *, run_root: Path) -> dict[str, Any]:
    env = _docker_env(run_root)
    try:
        daemon = subprocess.run(["docker", "info", "--format", "{{.ID}}"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("DOCKER_QUERY_UNKNOWN", "Docker daemon readiness could not be established") from exc
    daemon_id = daemon.stdout.decode("utf-8", "replace").strip()
    require(daemon.returncode == 0 and daemon_id and len(daemon_id) <= 256
            and daemon_id == container["daemon_id"],
            "DOCKER_QUERY_UNKNOWN", "Docker daemon query failed; container absence is unproven")
    container_id = container["container_id"]
    try:
        listed = subprocess.run(
            ["docker", "container", "ls", "--all", "--no-trunc",
             "--filter", "id=" + container_id, "--format", "{{.ID}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("DOCKER_QUERY_UNKNOWN", "Exact Docker container query could not complete") from exc
    require(listed.returncode == 0, "DOCKER_QUERY_UNKNOWN",
            "Docker exact-container inventory query failed; absence is unproven")
    require(len(listed.stdout) <= 512, "DOCKER_QUERY_UNKNOWN",
            "Docker exact-container query exceeded its response bound")
    try:
        listed_ids = listed.stdout.decode("ascii", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise AuthorError("DOCKER_QUERY_UNKNOWN", "Docker returned a malformed exact-container inventory") from exc
    require(all(_HEX64.fullmatch(item) for item in listed_ids)
            and all(item == container_id for item in listed_ids)
            and len(listed_ids) <= 1, "DOCKER_QUERY_UNKNOWN",
            "Docker returned an invalid exact-container query result")
    if container_id not in listed_ids:
        return {"container_id": container_id, "container_name": container["container_name"],
                "daemon_id": daemon_id, "daemon_query": "succeeded",
                "container_query": "successful_full_inventory_exact_id_absent"}
    try:
        inspected = subprocess.run(
            ["docker", "container", "inspect", "--format", "{{json .}}", container_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("DOCKER_QUERY_UNKNOWN", "Exact Docker container query could not complete") from exc
    require(inspected.returncode == 0, "DOCKER_QUERY_UNKNOWN",
            "Docker inventory showed the exact container but inspect failed")
    try:
        observed = strict_json(inspected.stdout[:1024 * 1024])
    except (AuthorError, ValueError) as exc:
        raise AuthorError("DOCKER_QUERY_UNKNOWN", "Docker returned an invalid exact-container response") from exc
    require(isinstance(observed, dict), "DOCKER_QUERY_UNKNOWN", "Docker returned an incomplete exact-container response")
    observed_id = str(observed.get("Id") or "").lower()
    observed_name = str(observed.get("Name") or "").lstrip("/")
    observed_image = str(observed.get("Image") or "").lower()
    mounts = observed.get("Mounts")
    source_mount = container["source_mount_path"]
    exact_mount = isinstance(mounts, list) and any(
        isinstance(item, dict) and item.get("Type") == "bind"
        and str(item.get("Source") or "") == source_mount
        and item.get("Destination") == "/workspace"
        for item in mounts
    )
    require(observed_id == container_id and observed_name == container["container_name"]
            and observed_image == container["image_id"] and exact_mount,
            "DOCKER_IDENTITY_MISMATCH", "Docker returned a container that does not match the persisted exact identity")
    raise AuthorError("CONTAINER_STILL_PRESENT", "The exact owned Docker container still exists; no cleanup was attempted")


def _read_lifecycle(store: Store, operation_id: str, receipt: dict, receipt_sha: str,
                    run_profile: dict, selected_checks: list[str], run_created: float) -> tuple[Path, dict[str, Any]]:
    home = store.home
    _private_directory(home)
    runs = home / "runs"
    _private_directory(runs)
    run_root = runs / operation_id
    _private_directory(run_root)
    lifecycle_path = run_root / _LIFECYCLE_NAME
    if not lifecycle_path.exists() and not lifecycle_path.is_symlink():
        return _read_legacy_observation(run_root, operation_id, receipt, receipt_sha,
                                        run_profile, selected_checks, run_created)
    _, raw = _private_file(lifecycle_path, maximum=_MAX_LIFECYCLE_BYTES)
    lifecycle = strict_json(raw)
    require(isinstance(lifecycle, dict) and lifecycle.get("format_version") == 1
            and lifecycle.get("operation_id") == operation_id,
            "CORRUPT_STATE", "Docker lifecycle metadata does not match the exact operation")
    source = run_root / "source"
    require(not source.is_symlink() and source.is_dir()
            and source.resolve(strict=True) == source.absolute(),
            "CORRUPT_STATE", "Persisted Docker source mount is missing or redirected")
    expected_mount = str(source.resolve(strict=True))
    records = lifecycle.get("containers")
    require(isinstance(records, list) and 1 <= len(records) <= 100,
            "CORRUPT_STATE", "Docker lifecycle container inventory is missing or out of bounds")
    expected_image = str((receipt.get("environment") or {}).get("image_id") or "").lower()
    require((receipt.get("environment") or {}).get("backend") == "docker"
            and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image)),
            "CORRUPT_STATE", "Immutable receipt lacks the exact qualified Docker image ID")
    expected_checks = [("setup", item) for item in run_profile.get("setup", [])]
    expected_checks += [("checks", item) for item in run_profile.get("checks", [])
                        if item.get("id") in selected_checks]
    require(run_profile.get("backend") == "docker" and expected_checks,
            "CORRUPT_STATE", "Durable run profile does not define the historical Docker commands")
    seen_sequences, seen_ids = set(), set()
    validated = []
    operation_container_prefix = "pba-" + operation_id.replace("_", "-") + "-"
    for record in records:
        require(isinstance(record, dict), "CORRUPT_STATE", "Docker lifecycle container entry is malformed")
        sequence = record.get("sequence")
        container_id = record.get("container_id")
        name = record.get("container_name")
        image_id = str(record.get("image_id") or "").lower()
        daemon_id = record.get("daemon_id")
        source_mount = record.get("source_mount_path")
        require(type(sequence) is int and 0 <= sequence <= 999 and sequence not in seen_sequences,
                "CORRUPT_STATE", "Docker lifecycle sequence is invalid or duplicated")
        seen_sequences.add(sequence)
        require(isinstance(container_id, str) and bool(_HEX64.fullmatch(container_id))
                and container_id not in seen_ids,
                "EXECUTION_UNKNOWN", "A Docker create identity was not durably acknowledged or is duplicated")
        seen_ids.add(container_id)
        expected_name = operation_container_prefix + str(sequence)
        require(name == expected_name and bool(re.fullmatch(r"pba-[a-z0-9-]+", str(name or ""))),
                "CORRUPT_STATE", "Docker container name does not match the operation and sequence")
        require(image_id == expected_image and source_mount == expected_mount,
                "CORRUPT_STATE", "Docker container image or source mount differs from the immutable operation")
        require(sequence < len(expected_checks) and record.get("configured_image") == expected_image
                and record.get("command") == expected_checks[sequence][1].get("argv"),
                "CORRUPT_STATE", "Docker lifecycle command differs from the immutable run profile")
        require(isinstance(daemon_id, str) and 0 < len(daemon_id) <= 256,
                "EXECUTION_UNKNOWN", "Docker lifecycle metadata lacks the acknowledged daemon identity")
        require(record.get("state") in {"reserved", "creating", "created", "starting", "running",
                                         "unknown", "cleaned", "cleanup_unresolved", "removed", "not_started"},
                "CORRUPT_STATE", "Docker lifecycle state is invalid")
        cid_path = run_root / f"{name}.cid"
        _, cid_bytes = _private_file(cid_path, maximum=256)
        require(cid_bytes.decode("ascii", "strict").strip() == container_id,
                "CORRUPT_STATE", "Docker create acknowledgement does not match its private cidfile")
        stages = record.get("stages")
        require(isinstance(stages, list) and 1 <= len(stages) <= 2,
                "CORRUPT_STATE", "Docker lifecycle stage inventory is incomplete")
        stage_names = [stage.get("name") if isinstance(stage, dict) else None for stage in stages]
        require(stage_names[0] == "create" and stage_names.count("create") == 1
                and stage_names.count("start") <= 1
                and all(name in {"create", "start"} for name in stage_names),
                "CORRUPT_STATE", "Docker create/start stage order is invalid")
        identity_records = []
        groups = set()
        for stage in stages:
            require(stage.get("state") in {"launching", "running", "exited", "unknown", "not_started"},
                    "CORRUPT_STATE", "Docker client stage state is invalid")
            raw_identity = stage.get("identity")
            if raw_identity is None:
                require(stage.get("state") == "not_started", "EXECUTION_UNKNOWN",
                        f"Docker {stage['name']} client identity was not recorded")
                continue
            identity = _lifecycle_client_identity(raw_identity, label=f"Docker {stage['name']} client")
            identity_records.append(identity)
            groups.add(identity["process_group_id"])
        create = stages[0]
        require(create.get("state") == "exited" and create.get("exit_code") == 0,
                "EXECUTION_UNKNOWN", "Docker create did not complete with a conclusive acknowledged result")
        cleanup = record.get("cleanup")
        require(isinstance(cleanup, dict) and cleanup.get("container_id") in {None, container_id}
                and cleanup.get("state") in {"not_started", "running", "proven_absent", "unresolved"},
                "CORRUPT_STATE", "Docker cleanup record is malformed or identifies another container")
        validated.append({
            "sequence": sequence, "container_id": container_id, "container_name": name,
            "image_id": image_id, "source_mount_path": source_mount,
            "daemon_id": daemon_id, "evidence_source": "native_lifecycle",
            "identities": identity_records, "process_groups": sorted(groups),
        })
    require(sorted(seen_sequences) == list(range(len(seen_sequences))),
            "CORRUPT_STATE", "Docker lifecycle sequence inventory has a gap")
    require(receipt.get("cleanup") == "unresolved" and receipt.get("classification") == "EXECUTION_UNKNOWN"
            and receipt.get("state") == "unknown_execution"
            and receipt.get("operation_id") == operation_id,
            "EVIDENCE_INVALID", "Historical execution receipt is not the exact unknown-execution receipt")
    return run_root, {"containers": validated, "source_mount_path": expected_mount,
                      "evidence_source": "native_lifecycle"}


def _read_legacy_observation(run_root: Path, operation_id: str, receipt: dict,
                             receipt_sha: str, run_profile: dict,
                             selected_checks: list[str], run_created: float) -> tuple[Path, dict[str, Any]]:
    """Validate separate operator observations for runs predating native lifecycle journaling."""
    path = run_root / _LEGACY_OBSERVATION_NAME
    _, raw = _private_file(path, maximum=_MAX_LIFECYCLE_BYTES)
    observation = strict_json(raw)
    require(isinstance(observation, dict) and observation.get("format_version") == 1
            and observation.get("provenance") == "operator_observed_legacy"
            and observation.get("operation_id") == operation_id
            and observation.get("original_receipt_sha") == receipt_sha,
            "EVIDENCE_INVALID", "Legacy operator observation is not bound to this exact immutable receipt")
    require(receipt.get("cleanup") == "unresolved" and receipt.get("classification") == "EXECUTION_UNKNOWN"
            and receipt.get("state") == "unknown_execution" and receipt.get("operation_id") == operation_id,
            "EVIDENCE_INVALID", "Historical execution receipt is not the exact unknown-execution receipt")
    require(observation.get("no_inflight_create_request_attested") is True,
            "EXECUTION_UNKNOWN", "Legacy observation does not attest that no Docker create request remains in flight")
    require(isinstance(observation.get("operator"), str) and observation["operator"].strip()
            and len(observation["operator"]) <= 120 and isinstance(observation.get("attested_at"), str)
            and isinstance(observation.get("attestation_evidence"), dict),
            "EVIDENCE_INVALID", "Legacy operator observation lacks its independent attestation record")
    daemon = observation.get("daemon")
    require(isinstance(daemon, dict) and isinstance(daemon.get("daemon_id"), str)
            and daemon.get("daemon_id") and daemon.get("endpoint"),
            "EVIDENCE_INVALID", "Legacy operator observation lacks exact Docker daemon evidence")
    old_daemon_id = daemon.get("old_daemon_id")
    require(daemon.get("continuity_basis") == "operator_attested_restart_same_private_engine"
            and daemon.get("same_private_engine") is True,
            "EVIDENCE_INVALID", "Legacy observation lacks the explicit same-engine continuity boundary")
    continuity = observation.get("continuity_evidence")
    require(isinstance(continuity, dict)
            and all(isinstance(continuity.get(key), dict)
                    for key in ("old_engine", "current_engine", "old_service_stop", "current_service_start",
                                "engine_id_file")),
            "EVIDENCE_INVALID", "Legacy observation lacks the old/current engine continuity evidence")
    require(isinstance(observation.get("old_service_identity"), dict)
            and isinstance(observation.get("current_service_identity"), dict)
            and isinstance(observation.get("old_service_stopped_at"), str)
            and isinstance(observation.get("current_service_started_at"), str),
            "EVIDENCE_INVALID", "Legacy engine continuity lacks exact service identities and transition times")
    for reference in continuity.values():
        ref_path, ref_hash = reference.get("path"), reference.get("sha256")
        evidence_path = Path(ref_path) if isinstance(ref_path, str) else Path("/")
        require(isinstance(ref_path, str) and evidence_path.is_absolute()
                and evidence_path.is_relative_to(run_root) and not evidence_path.is_symlink()
                and isinstance(ref_hash, str) and _HEX64.fullmatch(ref_hash),
                "EVIDENCE_INVALID", "Legacy continuity evidence reference is malformed")
        _, contents = _private_file(evidence_path, maximum=1024 * 1024)
        require(hashlib.sha256(contents).hexdigest() == ref_hash,
                "EVIDENCE_INVALID", "Legacy continuity evidence content identity changed")
        if reference is continuity["engine_id_file"]:
            reference["_raw"] = contents
        else:
            parsed = strict_json(contents)
            require(isinstance(parsed, dict), "EVIDENCE_INVALID",
                    "Legacy continuity source files must contain structured JSON evidence")
            reference["_parsed"] = parsed
    old_engine = continuity["old_engine"]["_parsed"]
    current_engine = continuity["current_engine"]["_parsed"]
    old_stop = continuity["old_service_stop"]["_parsed"]
    current_start = continuity["current_service_start"]["_parsed"]
    require(old_engine.get("data_root") == current_engine.get("data_root")
            and old_engine.get("exec_root") == current_engine.get("exec_root")
            and old_engine.get("socket") == current_engine.get("socket")
            and old_engine.get("config_sha256") == current_engine.get("config_sha256")
            and old_engine.get("daemon_flags") == current_engine.get("daemon_flags")
            and current_engine.get("daemon_id") == daemon["daemon_id"],
            "EVIDENCE_INVALID", "Legacy private engine identity/configuration changed across restart")
    require(old_stop.get("state") == "inactive" and current_start.get("state") == "active"
            and old_stop.get("identity") == observation["old_service_identity"]
            and current_start.get("identity") == observation["current_service_identity"]
            and old_stop.get("stopped_at") == observation.get("old_service_stopped_at")
            and current_start.get("started_at") == observation.get("current_service_started_at"),
            "EVIDENCE_INVALID", "Legacy service stop/start evidence does not bind to exact identities and timestamps")
    if old_daemon_id is not None:
        require(isinstance(old_daemon_id, str) and 0 < len(old_daemon_id) <= 256,
                "EVIDENCE_INVALID", "Legacy historical daemon identity is malformed")
        engine_ref = continuity["engine_id_file"]
        engine_bytes = engine_ref["_raw"]
        source_stat = observation.get("persistent_engine_id_source")
        require(isinstance(source_stat, dict)
                and source_stat.get("daemon_id") == old_daemon_id
                and old_daemon_id == daemon["daemon_id"]
                and old_engine.get("daemon_id") == old_daemon_id
                and old_engine.get("engine_id_file_sha256") == engine_ref["sha256"]
                and engine_bytes.decode("ascii", "strict").strip() == old_daemon_id
                and source_stat.get("owner_uid") == 0 and source_stat.get("mode") == 0o600
                and isinstance(source_stat.get("source_path"), str)
                and type(source_stat.get("mtime")) in (int, float)
                and source_stat["mtime"] <= run_created,
                "EVIDENCE_INVALID", "Historical daemon ID is not bound to the pre-run persistent engine-ID evidence")
    old_service_identity = _legacy_service_identity(
        observation["old_service_identity"], label="Old Docker service", current=False)
    current_service_identity = _legacy_service_identity(
        observation["current_service_identity"], label="Current Docker service", current=True)
    expected_image = str((receipt.get("environment") or {}).get("image_id") or "").lower()
    require((receipt.get("environment") or {}).get("backend") == "docker"
            and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image)),
            "EVIDENCE_INVALID", "Immutable receipt lacks the qualified Docker image identity")
    source = run_root / "source"
    require(not source.is_symlink() and source.is_dir()
            and source.resolve(strict=True) == source.absolute(),
            "CORRUPT_STATE", "Persisted Docker source mount is missing or redirected")
    expected_mount = str(source.resolve(strict=True))
    records = observation.get("containers")
    require(isinstance(records, list) and 1 <= len(records) <= 100,
            "EVIDENCE_INVALID", "Legacy observation lacks a bounded exact container inventory")
    expected_prefix = "pba-" + operation_id.replace("_", "-") + "-"
    profile_image = run_profile.get("image")
    expected_checks = [("setup", item) for item in run_profile.get("setup", [])]
    expected_checks += [("checks", item) for item in run_profile.get("checks", [])
                        if item.get("id") in selected_checks]
    require(run_profile.get("backend") == "docker" and isinstance(profile_image, str)
            and isinstance(selected_checks, list) and expected_checks,
            "EVIDENCE_INVALID", "Durable run profile does not define the historical Docker commands")
    seen_ids, validated = set(), []
    owner_identity = observation.get("process_identity")
    require(isinstance(owner_identity, dict) and isinstance(owner_identity.get("supervisor"), dict),
            "EVIDENCE_INVALID", "Legacy observation lacks the original supervisor identity")
    attestation_evidence = observation["attestation_evidence"]
    launch_ref = attestation_evidence.get("launch")
    child_ref = attestation_evidence.get("child")
    require(isinstance(launch_ref, dict) and isinstance(child_ref, dict),
            "EVIDENCE_INVALID", "Legacy observation must bind the original launch.json and child.json")

    def read_bound_run_file(reference: dict, expected_name: str) -> tuple[dict, bytes]:
        ref_path = reference.get("path")
        digest = reference.get("sha256")
        require(isinstance(ref_path, str) and Path(ref_path) == run_root / expected_name
                and isinstance(digest, str) and _HEX64.fullmatch(digest),
                "EVIDENCE_INVALID", f"Legacy observation does not bind the exact {expected_name}")
        target = Path(ref_path)
        _, contents = _private_file(target, maximum=1024 * 1024)
        require(hashlib.sha256(contents).hexdigest() == digest,
                "EVIDENCE_INVALID", f"Legacy {expected_name} content identity changed")
        parsed = strict_json(contents)
        require(isinstance(parsed, dict) and parsed.get("operation_id") == operation_id,
                "EVIDENCE_INVALID", f"Legacy {expected_name} does not match this exact operation")
        return parsed, contents

    launch_record, _ = read_bound_run_file(launch_ref, "launch.json")
    child_record, _ = read_bound_run_file(child_ref, "child.json")
    require(launch_record.get("supervisor") == owner_identity["supervisor"],
            "EVIDENCE_INVALID", "Legacy supervisor identity differs from the original launch record")
    for sequence, record in enumerate(records):
        require(isinstance(record, dict) and record.get("sequence") == sequence,
                "EVIDENCE_INVALID", "Legacy container sequences must be exact and contiguous")
        container_id = record.get("container_id")
        name = record.get("container_name")
        image_id = str(record.get("image_id") or "").lower()
        source_mount = record.get("source_mount_path")
        require(isinstance(container_id, str) and _HEX64.fullmatch(container_id)
                and container_id not in seen_ids,
                "EVIDENCE_INVALID", "Legacy observation lacks a unique immutable full container ID")
        seen_ids.add(container_id)
        require(name == expected_prefix + str(sequence) and image_id == expected_image
                and source_mount == expected_mount,
                "EVIDENCE_INVALID", "Legacy container identity differs from the deterministic run profile")
        require(sequence < len(expected_checks), "EVIDENCE_INVALID",
                "Legacy observation has more containers than the durable run profile")
        expected_stage, expected_check = expected_checks[sequence]
        require(record.get("daemon_id") == daemon["daemon_id"]
                and record.get("configured_image") == profile_image
                and record.get("stage") == expected_stage
                and record.get("check_id") == expected_check.get("id"),
                "EVIDENCE_INVALID", "Legacy container daemon or configured image differs from the run profile")
        require(record.get("command") == expected_check.get("argv"),
                "EVIDENCE_INVALID", "Legacy container command differs from the immutable run profile")
        events = record.get("events")
        require(isinstance(events, list) and events and all(
            isinstance(event, dict) and event.get("container_id") == container_id
            and event.get("daemon_id") == old_daemon_id
            and event.get("container_name") == name and event.get("image_id") == image_id
            for event in events), "EVIDENCE_INVALID", "Legacy create/destroy observations do not bind to the exact container")
        actions = {event.get("action") for event in events}
        require("create" in actions and "destroy" in actions,
                "EVIDENCE_INVALID", "Legacy observation lacks separate create and destroy evidence")
        require(child_record.get("container") == name,
                "EVIDENCE_INVALID", "Legacy child record does not identify this exact deterministic container")
        child_process = child_record.get("process")
        recorded_client = owner_identity.get("start_client") or owner_identity.get("create_client")
        require(isinstance(child_process, dict) and isinstance(recorded_client, dict)
                and child_process.get("pid") == recorded_client.get("pid")
                and child_process.get("start") == (recorded_client.get("start_time_ticks") or recorded_client.get("start"))
                and child_process.get("boot") == (recorded_client.get("boot_id") or recorded_client.get("boot")),
                "EVIDENCE_INVALID", "Legacy client identity differs from original child.json")
        # Each source capture is private and content-bound. A successful Docker
        # daemon query and fresh exact-ID inventory below remain mandatory.
        evidence = record.get("evidence")
        require(isinstance(evidence, dict) and all(
            isinstance(evidence.get(key), dict) and isinstance(evidence[key].get("path"), str)
            and isinstance(evidence[key].get("sha256"), str)
            and _HEX64.fullmatch(evidence[key]["sha256"])
            for key in ("inspect", "create_event", "destroy_event")),
            "EVIDENCE_INVALID", "Legacy private inspect and Docker event evidence references are incomplete")
        parsed_evidence: dict[str, dict[str, Any]] = {}
        for evidence_key, source_record in evidence.items():
            evidence_path = Path(source_record["path"])
            require(evidence_path.is_absolute() and not evidence_path.is_symlink()
                    and evidence_path.is_relative_to(run_root)
                    and evidence_path.resolve(strict=True).is_relative_to(run_root.resolve(strict=True)),
                    "EVIDENCE_INVALID", "Legacy evidence path is not an exact regular path")
            _, evidence_raw = _private_file(evidence_path, maximum=1024 * 1024)
            require(hashlib.sha256(evidence_raw).hexdigest() == source_record["sha256"],
                    "EVIDENCE_INVALID", "Legacy private source evidence content identity changed")
            parsed = strict_json(evidence_raw)
            if isinstance(parsed, list) and len(parsed) == 1:
                parsed = parsed[0]
            require(isinstance(parsed, dict), "EVIDENCE_INVALID",
                    "Legacy Docker evidence must contain one structured observation")
            parsed_evidence[evidence_key] = parsed
        inspect_record = parsed_evidence["inspect"]
        inspect_id = str(inspect_record.get("id") or inspect_record.get("Id") or "").lower()
        inspect_name = str(inspect_record.get("name") or inspect_record.get("Name") or "").lstrip("/")
        inspect_image = str(inspect_record.get("image") or inspect_record.get("Image") or "").lower()
        inspect_mounts = inspect_record.get("mounts") or inspect_record.get("Mounts")
        require(isinstance(inspect_mounts, list), "EVIDENCE_INVALID",
                "Legacy inspect evidence lacks its mount inventory")
        inspect_mount_matches = any(
            isinstance(mount, dict)
            and str(mount.get("source") or mount.get("Source") or "") == expected_mount
            and (mount.get("destination") or mount.get("Destination")) == "/workspace"
            and (mount.get("type") or mount.get("Type")) == "bind"
            for mount in inspect_mounts
        )
        require(inspect_id == container_id and inspect_name == name
                and inspect_image == image_id and inspect_mount_matches
                and inspect_record.get("cmd", inspect_record.get("Command")) == expected_check.get("argv"),
                "EVIDENCE_INVALID", "Legacy Docker inspect does not bind the exact container, image, mount, and command")
        for evidence_key, expected_action in (("create_event", "create"), ("destroy_event", "destroy")):
            event = parsed_evidence[evidence_key]
            actor_data = event.get("Actor") if isinstance(event.get("Actor"), dict) else {}
            attributes = actor_data.get("Attributes") if isinstance(actor_data.get("Attributes"), dict) else {}
            require(event.get("Type") == "container" and event.get("Action") == expected_action
                    and str(actor_data.get("ID") or "").lower() == container_id
                    and attributes.get("name") == name and str(attributes.get("image") or "").lower() == image_id,
                    "EVIDENCE_INVALID", f"Legacy Docker {expected_action} event does not bind the exact container")
        identities = []
        groups = set()
        for label in ("create_client", "start_client"):
            item = owner_identity.get(label)
            if item is None:
                continue
            identity = _legacy_client_identity(item, label=f"Legacy Docker {label}")
            identities.append(identity)
            groups.add(identity["process_group_id"])
        validated.append({
            "sequence": sequence, "container_id": container_id, "container_name": name,
            "image_id": image_id, "source_mount_path": source_mount,
            "daemon_id": daemon["daemon_id"], "evidence_source": "operator_observed_legacy",
            "identities": identities, "process_groups": sorted(groups),
        })
    supervisor = owner_identity["supervisor"]
    return run_root, {
        "containers": validated, "source_mount_path": expected_mount,
        "evidence_source": "operator_observed_legacy", "legacy_supervisor": supervisor,
        "legacy_operator": observation["operator"],
        "legacy_old_daemon_id": old_daemon_id,
        "legacy_current_daemon_id": daemon["daemon_id"],
        "legacy_continuity_basis": daemon.get("continuity_basis"),
        "legacy_old_service_identity": old_service_identity,
        "legacy_current_service_identity": current_service_identity,
        "observation_sha256": hashlib.sha256(raw).hexdigest(),
        "no_inflight_create_request_attested": True,
    }


class OperatorResolution:
    """Local-only command core for explicit, no-signalling quiescence resolution."""

    def __init__(self, store: Store):
        self.store = store

    def resolve(self, operation_id: str, *, operator: str, apply: bool = False,
                attestation: str = "", reason: str = "") -> dict[str, Any]:
        safe_id(operation_id)
        actor = str(operator or "").strip()
        require(actor and len(actor) <= 120 and all(ord(char) >= 32 for char in actor),
                "INVALID_REQUEST", "A named operator identity is required")
        resolution_reason = str(reason or "").strip()
        require(resolution_reason and len(resolution_reason) <= 500
                and all(ord(char) >= 32 for char in resolution_reason),
                "INVALID_REQUEST", "A bounded manual resolution reason is required")
        require(sys.platform == "linux", "BLOCKED_ENVIRONMENT", "Operator resolution is available only on the Linux execution host")
        initial = self.store.one("SELECT task_id FROM operations WHERE id=?", (operation_id,))
        require(initial is not None and initial["task_id"], "NOT_FOUND", "Author operation is unavailable")
        task_id = initial["task_id"]
        audit_path = self.store.home / "runs" / operation_id / _AUDIT_NAME

        # Follow the established maintenance -> task lock order used by checks.
        with file_lock(self.store.home / "locks/maintenance", blocking=False):
            with file_lock(self.store.home / ("locks/task-" + task_id), blocking=False):
                with file_lock(self.store.home / ("locks/run-" + operation_id), blocking=False):
                    operation = self.store.one("SELECT * FROM operations WHERE id=? AND kind='checks.run'", (operation_id,), required=False)
                    run = self.store.one("SELECT * FROM runs WHERE id=?", (operation_id,), required=False)
                    require(operation is not None and run is not None and operation["task_id"] == task_id
                            and run["task_id"] == task_id,
                            "NOT_FOUND", "Exact Author check operation is unavailable")
                    if operation["state"] == "blocked" and operation["phase"] == "operator_resolved" \
                            and run["state"] == "blocked" and run["cleanup"] == "operator_attested":
                        return self._replay_applied(operation, run, audit_path, actor, attestation,
                                                    resolution_reason, apply)
                    require(operation["state"] == "unknown_execution" and run["state"] == "unknown_execution"
                            and run["cleanup"] == "unresolved",
                            "EXECUTION_UNKNOWN", "Only an exact unresolved unknown-execution operation can be resolved")
                    require(operation["result"],
                            "CORRUPT_STATE", "Unknown execution result evidence is missing")
                    original_result = strict_json(operation["result"])
                    original_error = strict_json(operation["error"]) if operation["error"] else None
                    require(isinstance(original_result, dict)
                            and (original_error is None or isinstance(original_error, dict))
                            and original_result.get("classification") == "EXECUTION_UNKNOWN"
                            and original_result.get("receipt_sha") == run["receipt_sha"]
                            and original_result.get("cleanup") == "unresolved"
                            and (original_error is None or original_error.get("code") == "EXECUTION_UNKNOWN"),
                            "CORRUPT_STATE", "Operation result/error does not match its unknown-execution receipt")
                    receipt_raw = self.store.blobs.read(run["receipt_sha"], limit=_MAX_RECEIPT_BYTES)
                    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
                    require(receipt_sha == run["receipt_sha"] and self.store.blobs.verify(receipt_sha),
                            "EVIDENCE_INVALID", "Immutable execution receipt failed its stored content identity")
                    receipt = strict_json(receipt_raw)
                    owner = _owner_identity(strict_json(operation["owner_identity"] or "null"))
                    operation_payload = strict_json(operation["payload"] or "{}")
                    run_profile = strict_json(run["profile"] or "{}")
                    run_root, lifecycle = _read_lifecycle(
                        self.store, operation_id, receipt, receipt_sha, run_profile,
                        operation_payload.get("selected", []), run["created"],
                    )
                    if lifecycle["evidence_source"] == "operator_observed_legacy":
                        legacy_owner = lifecycle["legacy_supervisor"]
                        require(legacy_owner.get("pid") == owner["pid"]
                                and str(legacy_owner.get("start") or "") == owner["start_time_ticks"]
                                and legacy_owner.get("boot") == owner["boot_id"],
                                "EVIDENCE_INVALID", "Legacy supervisor observation differs from the durable operation identity")

                    active = self.store.one(
                        "SELECT 1 FROM runs WHERE state IN ('launching','running','cancelling') LIMIT 1",
                        required=False,
                    )
                    require(not active, "EXECUTION_STILL_ACTIVE", "Another execution is active; resolution is blocked")
                    other_uncertain = self.store.one(
                        "SELECT 1 FROM runs WHERE task_id=? AND id<>? AND (state='unknown_execution' OR cleanup='unresolved') LIMIT 1",
                        (task_id, operation_id), required=False,
                    )
                    require(not other_uncertain, "EXECUTION_UNKNOWN", "Another uncertain execution still fences this task")

                    process_evidence = [_assert_identity_absent(owner, label="Recorded Author supervisor")]
                    if lifecycle["evidence_source"] == "operator_observed_legacy":
                        old_service = lifecycle["legacy_old_service_identity"]
                        if old_service.get("presence_check") == "pid_unoccupied":
                            process_evidence.append(_assert_pid_unoccupied(old_service, label="Old Docker service"))
                        else:
                            process_evidence.append(_assert_identity_absent(old_service, label="Old Docker service"))
                        current_service_evidence = _assert_identity_present(
                            lifecycle["legacy_current_service_identity"], label="Current Docker service")
                    else:
                        current_service_evidence = None
                    groups = {owner["process_group_id"]}
                    for container in lifecycle["containers"]:
                        for index, identity in enumerate(container["identities"]):
                            process_evidence.append(_assert_identity_absent(identity, label=f"Docker client {container['sequence']}.{index}"))
                            groups.add(identity["process_group_id"])
                    group_evidence = _assert_groups_quiescent(groups)
                    container_evidence = [
                        _docker_container_absent(container, run_root=run_root)
                        for container in lifecycle["containers"]
                    ]
                    # A second census after all daemon calls closes transient host
                    # process changes while locks still exclude new Author work.
                    process_recheck = [_assert_identity_absent(owner, label="Recorded Author supervisor")]
                    if lifecycle["evidence_source"] == "operator_observed_legacy":
                        old_service = lifecycle["legacy_old_service_identity"]
                        if old_service.get("presence_check") == "pid_unoccupied":
                            process_recheck.append(_assert_pid_unoccupied(old_service, label="Old Docker service"))
                        else:
                            process_recheck.append(_assert_identity_absent(old_service, label="Old Docker service"))
                        current_service_evidence = _assert_identity_present(
                            lifecycle["legacy_current_service_identity"], label="Current Docker service")
                    for container in lifecycle["containers"]:
                        for index, identity in enumerate(container["identities"]):
                            process_recheck.append(_assert_identity_absent(identity, label=f"Docker client {container['sequence']}.{index}"))
                    group_recheck = _assert_groups_quiescent(groups)

                    evidence = {
                        "evidence_source": lifecycle["evidence_source"],
                        "legacy_observation_sha256": lifecycle.get("observation_sha256"),
                        "no_inflight_create_request_attested": lifecycle.get("no_inflight_create_request_attested"),
                        "process_identities": process_evidence,
                        "process_group_census": group_evidence,
                        "docker_containers_absent": container_evidence,
                        "process_identity_recheck": process_recheck,
                        "process_group_recheck": group_recheck,
                        "current_service_identity": current_service_evidence,
                        "active_executor_count": 0,
                    }
                    if not apply:
                        return {
                            "ready": True, "applied": False, "operation_id": operation_id,
                            "task_id": task_id, "operator": actor,
                            "resolution_reason": resolution_reason,
                            "historical_classification": "EXECUTION_UNKNOWN",
                            "original_receipt_sha": receipt_sha,
                            "evidence_source": lifecycle["evidence_source"],
                            "evidence": evidence,
                            "quiescence_limit": (
                                "The checks prove recorded host identities, their process groups, and exact Docker IDs are absent. "
                                "They cannot prove that arbitrary detached code did not escape all recorded ownership; the named "
                                "operator attestation remains necessary. Execution outcome remains unknown."
                            ),
                        }
                    required_attestation = (LEGACY_CONTINUITY_ATTESTATION
                                            if lifecycle["evidence_source"] == "operator_observed_legacy"
                                            else ATTESTATION)
                    require(attestation == required_attestation, "INVALID_REQUEST",
                            "Apply requires the exact --attest-quiescence statement shown by --help")
                    return self._apply(
                        operation, run, audit_path, actor, receipt_sha,
                        original_result, original_error, evidence, required_attestation,
                        resolution_reason,
                    )

    def _replay_applied(self, operation: dict, run: dict, audit_path: Path,
                        actor: str, attestation: str, reason: str, apply: bool) -> dict[str, Any]:
        require(apply and attestation in {ATTESTATION, LEGACY_CONTINUITY_ATTESTATION},
                "INVALID_REQUEST", "This operation is already resolved; replay apply with the exact attestation")
        _, raw = _private_file(audit_path, maximum=1024 * 1024)
        audit = strict_json(raw)
        require(isinstance(audit, dict) and audit.get("operation_id") == operation["id"]
                and audit.get("task_id") == run["task_id"]
                and audit.get("operator") == actor
                and audit.get("resolution_reason") == reason
                and audit.get("disposition") == "operator_attested_quiescence"
                and audit.get("original_receipt_sha") == run["receipt_sha"]
                and audit.get("attestation") == attestation,
                "CORRUPT_STATE", "Existing operator resolution audit does not match this exact replay")
        expected_attestation = (LEGACY_CONTINUITY_ATTESTATION
                                if audit.get("evidence_source") == "operator_observed_legacy"
                                else ATTESTATION)
        require(attestation == expected_attestation,
                "INVALID_REQUEST", "Replay attestation does not match the recorded legacy/native evidence boundary")
        raw_hash = hashlib.sha256(raw).hexdigest()
        resolution = self.store.one("SELECT * FROM operations WHERE id=? AND kind='checks.operator_resolution'",
                                    (audit.get("resolution_operation_id"),), required=False)
        require(resolution is not None and resolution["state"] == "completed",
                "CORRUPT_STATE", "Resolution operation receipt is missing or incomplete")
        result = strict_json(resolution["result"] or "null")
        require(result.get("audit_sha256") == raw_hash and result.get("original_receipt_sha") == run["receipt_sha"],
                "CORRUPT_STATE", "Resolution operation does not match its immutable audit file")
        return {"ready": True, "applied": True, "idempotent_replay": True,
                "operation_id": operation["id"], "resolution_operation_id": resolution["id"],
                "task_id": run["task_id"], "operator": actor,
                "resolution_reason": reason,
                "historical_classification": "EXECUTION_UNKNOWN", "original_receipt_sha": run["receipt_sha"],
                "audit_sha256": raw_hash, "audit_receipt": _AUDIT_NAME,
                "admission_fence": "released_by_operator_attested_disposition",
                "execution_outcome": "unknown"}

    def _apply(self, operation: dict, run: dict, audit_path: Path, actor: str,
               receipt_sha: str, original_result: dict, original_error: dict,
               evidence: dict, attestation: str, reason: str) -> dict[str, Any]:
        resolution_operation_id = "op_" + hashlib.sha256(
            ("patchbay-author-operator-resolution-v1:" + operation["id"]).encode()
        ).hexdigest()[:32]
        audit = {
            "format_version": 1,
            "disposition": "operator_attested_quiescence",
            "operation_id": operation["id"],
            "resolution_operation_id": resolution_operation_id,
            "task_id": run["task_id"],
            "revision": run["revision"],
            "original_receipt_sha": receipt_sha,
            "historical_classification": "EXECUTION_UNKNOWN",
            "historical_operation_state": "unknown_execution",
            "historical_operation_phase": operation["phase"],
            "original_operation_result": original_result,
            "original_operation_error": original_error,
            "operator": actor,
            "resolution_reason": reason,
            "attestation": attestation,
            "recorded_at": utc_now(),
            "evidence": evidence,
            "evidence_source": evidence.get("evidence_source"),
            "execution_outcome": "unknown",
            "limits": (
                "No process or container was signalled or removed. Machine census cannot exclude arbitrary detached work "
                "outside recorded process groups and exact Docker IDs; the operator attestation covers that residual uncertainty."
            ),
        }
        encoded = canonical(audit) + b"\n"
        require(len(encoded) <= 1024 * 1024, "RESOURCE_LIMIT", "Operator resolution audit exceeds its size bound")
        if audit_path.exists() or audit_path.is_symlink():
            _, existing_bytes = _private_file(audit_path, maximum=1024 * 1024)
            existing = strict_json(existing_bytes)
            require(isinstance(existing, dict)
                    and existing.get("operation_id") == operation["id"]
                    and existing.get("task_id") == run["task_id"]
                    and existing.get("operator") == actor
                    and existing.get("resolution_reason") == reason
                    and existing.get("original_receipt_sha") == receipt_sha
                    and existing.get("attestation") == attestation
                    and existing.get("disposition") == "operator_attested_quiescence"
                    and existing.get("evidence") == evidence,
                    "CORRUPT_STATE", "Existing operator audit cannot be replayed for this exact operation")
            audit_bytes = existing_bytes
            resolution_operation_id = existing.get("resolution_operation_id")
            require(isinstance(resolution_operation_id, str)
                    and resolution_operation_id == "op_" + hashlib.sha256(
                        ("patchbay-author-operator-resolution-v1:" + operation["id"]).encode()
                    ).hexdigest()[:32],
                    "CORRUPT_STATE", "Existing operator audit has an invalid resolution operation identity")
        else:
            try:
                descriptor = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError as exc:
                raise AuthorError("STATE_CONFLICT", "An operator resolution audit appeared concurrently") from exc
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
                audit_path.chmod(0o600)
                directory = os.open(audit_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except BaseException:
                # Never unlink a path after an uncertain write or directory fsync.
                # A later invocation can validate and complete the durable journal.
                raise
            audit_bytes = encoded
        audit_sha = hashlib.sha256(audit_bytes).hexdigest()
        result = {
            "resolution_operation_id": resolution_operation_id,
            "operation_id": operation["id"], "task_id": run["task_id"],
            "operator": actor, "disposition": "operator_attested_quiescence",
            "resolution_reason": reason,
            "historical_classification": "EXECUTION_UNKNOWN",
            "original_receipt_sha": receipt_sha, "audit_sha256": audit_sha,
            "execution_outcome": "unknown",
            "evidence": {
                "recorded_process_identities_absent_or_zombie": len(evidence["process_identities"]),
                "process_group_census_empty": True,
                "exact_docker_containers_absent": len(evidence["docker_containers_absent"]),
                "active_executor_count": 0,
            },
        }
        key = "operator-resolution:" + operation["id"]
        request = {"operation_id": operation["id"], "operator": actor,
                   "resolution_reason": reason,
                   "original_receipt_sha": receipt_sha, "attestation": attestation}
        request_fingerprint = fingerprint(request)
        stored_key = self.store.scoped_key(key)
        stored_fingerprint = self.store.scoped_fingerprint(request_fingerprint)
        now = utc_now()
        with self.store.transaction() as conn:
            current_operation = conn.execute("SELECT * FROM operations WHERE id=?", (operation["id"],)).fetchone()
            current_run = conn.execute("SELECT * FROM runs WHERE id=?", (operation["id"],)).fetchone()
            require(current_operation is not None and current_run is not None
                    and current_operation["state"] == "unknown_execution"
                    and current_run["state"] == "unknown_execution"
                    and current_run["cleanup"] == "unresolved"
                    and current_run["receipt_sha"] == receipt_sha,
                    "STATE_CONFLICT", "Unknown execution changed during operator resolution")
            prior = conn.execute(
                "SELECT * FROM operations WHERE task_id=? AND kind='checks.operator_resolution' AND idempotency_key=?",
                (run["task_id"], stored_key),
            ).fetchone()
            require(prior is None, "STATE_CONFLICT", "A resolution operation already exists for this execution")
            other = conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND id<>? AND (state='unknown_execution' OR cleanup='unresolved') LIMIT 1",
                (run["task_id"], operation["id"]),
            ).fetchone()
            require(other is None, "EXECUTION_UNKNOWN", "Another uncertain execution appeared during resolution")
            active = conn.execute("SELECT 1 FROM runs WHERE state IN ('launching','running','cancelling') LIMIT 1").fetchone()
            require(active is None, "EXECUTION_STILL_ACTIVE", "Another execution started during operator resolution")
            columns = {
                "id": resolution_operation_id, "task_id": run["task_id"],
                "kind": "checks.operator_resolution", "idempotency_key": stored_key,
                "fingerprint": stored_fingerprint, "revision": run["revision"],
                "state": "completed", "phase": "terminal",
                "payload": canonical(request).decode(), "result": canonical(result).decode(),
                "error": None, "owner_identity": None, "cancel_requested": 0,
                "created": now, "updated": now,
            }
            conn.execute("INSERT INTO operations (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                         tuple(columns.values()))
            # The original error/result and receipt stay byte-identical. Only the
            # admission state and cleanup disposition change; blocked is not a
            # test outcome or a claim that the execution never ran.
            conn.execute("UPDATE runs SET state='blocked',cleanup='operator_attested',updated=? WHERE id=?",
                         (now, operation["id"]))
            conn.execute("UPDATE operations SET state='blocked',phase='operator_resolved',updated=? WHERE id=?",
                         (now, operation["id"]))
            self.store.event(conn, "checks.operator_resolution_completed", run["task_id"], result)
        return {"ready": True, "applied": True, "idempotent_replay": False,
                **result, "audit_receipt": _AUDIT_NAME,
                "admission_fence": "released_by_operator_attested_disposition"}
