"""Explicit, no-signalling operator recovery for an unproven supervisor cleanup."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from patchbay.jobs.manager import (
    JobManager,
    JobState,
    TERMINAL_CLEANUP_RECOVERY_REQUIRED_OUTCOMES,
)
from patchbay.jobs.process_supervisor import _pid_live
from patchbay.repo_locks import RepoMutationBusy, RepoMutationLockManager

_SUPERVISOR_VERSION = "_job_process_supervisor_version"
_SUPERVISOR_SPAWNED = "_job_process_supervisor_spawned"
_SUPERVISOR_PROOF = "_job_process_supervisor_cleanup_proof"
_MARKER_VERSION = "_job_process_marker_version"
_DESCENDANT_IDENTITIES = "_job_descendant_process_identities_v1"
_DESCENDANT_UNTRUSTED = "_job_descendant_untrusted_live_v1"
_DESCENDANT_SCAN_UNCERTAIN = "_job_descendant_scan_uncertain_v1"
_RECOVERY_OPTION = "_job_supervisor_operator_recovery_receipt"
_CONFIRMATION = "I independently verified that every tracked process has exited and no detached same-job process remains active."


def _require_empty_marker_census(executor: Any, job_id: str, *, attempts: int = 8) -> None:
    """Require one conclusive empty scan, retrying only transient scan uncertainty."""

    for attempt in range(attempts):
        marker_pids = executor._job_marked_process_pids(job_id, force_refresh=True)
        if marker_pids:
            raise ValueError("Same-job marker census found a live process")
        if marker_pids == set():
            return
        if attempt + 1 < attempts:
            time.sleep(0.025)
    raise ValueError("Same-job marker census remained inconclusive")


def _server_is_listening(config: Mapping[str, Any]) -> bool:
    server = config.get("server") if isinstance(config.get("server"), Mapping) else {}
    host = str(server.get("host") or "127.0.0.1")
    port = int(server.get("port") or 8000)
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Cleanup recovery requires a literal loopback server host") from error
    if not address.is_loopback:
        raise ValueError("Cleanup recovery refuses a non-loopback server configuration")
    # Probe the exact configured loopback address first: a manager bound to
    # 127.0.0.2 is not reachable through 127.0.0.1. The other loopback aliases
    # catch wildcard-style local listeners without broadening off-host access.
    targets = list(dict.fromkeys([host, "127.0.0.1", "::1"]))
    for target in targets:
        try:
            with socket.create_connection((target, port), timeout=0.15):
                return True
        except OSError:
            continue
    return False


def _write_private_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(dict(payload), sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def recover_cleanup(
    config: dict[str, Any],
    job_id: str,
    *,
    operator: str,
    apply: bool = False,
    confirmation: str = "",
) -> dict[str, Any]:
    """Review or explicitly attest quiescence for one terminal job.

    This routine never signals a process. ``apply`` only records a local operator
    attestation after all machine-readable process, marker, group, and lock checks
    succeed. The receipt deliberately records that Darwin detached-process
    absence remains partly dependent on the named operator's observation.
    """

    actor = str(operator or "").strip()
    if not actor or len(actor) > 120 or any(ord(char) < 32 for char in actor):
        raise ValueError("--operator must provide a named operator identity (1-120 printable characters)")
    manager = JobManager(config)
    job = manager.get_job(job_id)
    if job is None:
        raise ValueError("Unknown job id")
    if job.state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
        raise ValueError("Recovery requires an already terminal job")
    if str(job.wrapper_cleanup_outcome or "") not in TERMINAL_CLEANUP_RECOVERY_REQUIRED_OUTCOMES:
        raise ValueError("Job is not in an operator-recovery cleanup state")
    if manager.active_job_count():
        raise ValueError("Other jobs are active; stop and review them before recovery")
    if _server_is_listening(config):
        raise ValueError("The configured PatchBay server is still listening; stop it before offline recovery")

    options = dict(job.options or {})
    if (
        options.get(_SUPERVISOR_VERSION) != 3
        or options.get(_SUPERVISOR_SPAWNED) is not True
        or options.get(_MARKER_VERSION) != 1
    ):
        raise ValueError("Job lacks the current spawned-supervisor and marker ownership contract")
    raw_proof_path = Path(str(options.get(_SUPERVISOR_PROOF) or "")).expanduser()
    if raw_proof_path.is_symlink():
        raise ValueError("Supervisor proof must be a regular non-symlink private file")
    proof_path = raw_proof_path.resolve(strict=True)
    log_root = Path(config["logging"]["job_logs_dir"]).expanduser().resolve()
    proof_stat = proof_path.stat()
    if (
        proof_path.parent != log_root
        or not proof_path.is_file()
        or proof_stat.st_uid != os.getuid()
        or stat.S_IMODE(proof_stat.st_mode) != 0o600
    ):
        raise ValueError("Supervisor proof is not a private regular file in the configured job evidence directory")
    proof_bytes = proof_path.read_bytes()
    try:
        proof = proof_bytes.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError("Supervisor proof is not a valid ASCII lifecycle receipt") from error
    pid = int(job.process_pid or 0)
    pgid = int(job.process_pgid or 0)
    match = re.fullmatch(r"patchbay-supervisor-cleanup-unproven-v2:(\d+):(\d+)", proof)
    if pid <= 0 or pgid != pid or match is None or int(match.group(1)) != pid:
        raise ValueError("Durable unproven receipt does not exactly match the job supervisor identity")
    sentinel_pid = int(match.group(2))
    if sentinel_pid <= 0 or sentinel_pid == pid:
        raise ValueError("Durable receipt contains an invalid sentinel identity")

    # Use the same production discovery routines as the executor, but avoid its
    # mutating descendant-reconciliation helper in this job-state assessment.
    from patchbay.jobs.executor import JobExecutor

    executor = JobExecutor(config, manager)
    if executor.tasks or executor.processes:
        raise ValueError("An executor still owns process/task state")
    if _pid_live(pid) or _pid_live(sentinel_pid):
        raise ValueError("Recorded supervisor or sentinel PID is live or may have been recycled")
    group_live = executor._process_group_liveness(pgid)
    if group_live is not False:
        raise ValueError("Process-group census is live or inconclusive")
    _require_empty_marker_census(executor, job_id)
    if options.get(_DESCENDANT_UNTRUSTED) or options.get(_DESCENDANT_SCAN_UNCERTAIN):
        raise ValueError("Persisted descendant tracking contains an unresolved or untrusted observation")
    tracked = options.get(_DESCENDANT_IDENTITIES, [])
    if not isinstance(tracked, list):
        raise ValueError("Persisted descendant identity list is malformed")
    tracked_ids: list[dict[str, Any]] = []
    for item in tracked:
        if not isinstance(item, dict):
            raise ValueError("Persisted descendant identity entry is malformed")
        try:
            child_pid = int(item.get("pid") or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Persisted descendant PID is malformed") from error
        identity = str(item.get("identity") or "")
        if child_pid <= 0 or not identity:
            raise ValueError("Persisted descendant identity is incomplete")
        if _pid_live(child_pid):
            # A live PID can be either the old child or a recycled PID. Recovery
            # never authorizes signalling, but still fails closed until every
            # recorded child PID is absent and the other censuses are clear.
            raise ValueError("A tracked descendant PID is still live or may have been recycled")
        tracked_ids.append({"pid": child_pid, "identity": identity, "same_process_live": False})

    # Recheck liveness after a short settling interval. A PID that appears during
    # this window blocks recovery; no process is ever signalled by this command.
    for _ in range(3):
        time.sleep(0.05)
        if _pid_live(pid) or _pid_live(sentinel_pid):
            raise ValueError("Supervisor or sentinel became live during the quiescence recheck")
        if executor._process_group_liveness(pgid) is not False:
            raise ValueError("Process-group census changed or became inconclusive during recheck")
        _require_empty_marker_census(executor, job_id)
        for item in tracked_ids:
            if _pid_live(int(item["pid"])):
                raise ValueError("A tracked descendant PID became live or may have been recycled during recheck")

    locks = RepoMutationLockManager(config)
    try:
        lock_context = locks.hold_sync(job.repo_path, operation="operator_cleanup_recovery")
        lock_context.__enter__()
    except RepoMutationBusy as error:
        raise ValueError("Repository mutation lock is busy; recovery remains blocked") from error
    try:
        if not apply:
            return {
                "ready": True,
                "applied": False,
                "job_id": job_id,
                "operator": actor,
                "supervisor_pid_absent": True,
                "sentinel_pid_absent": True,
                "process_group_empty": True,
                "job_marker_census_empty": True,
                "tracked_descendants_absent": True,
                "repository_lock_available": True,
                "quiescence_limit": "On Darwin, a fully detached process that removed the job marker may evade retrospective machine census; apply requires the operator attestation as well as these checks.",
            }
        if confirmation != _CONFIRMATION:
            raise ValueError("--apply requires the exact --confirm-quiescence statement shown by --help")
        # Repeat the endpoint check immediately before writing the local audit.
        if _server_is_listening(config):
            raise ValueError("PatchBay server became reachable during recovery")
        receipt_path = log_root / f"{job_id}.operator-cleanup-recovery.{uuid.uuid4().hex}.json"
        receipt = {
            "schema": "patchbay-operator-cleanup-recovery-v1",
            "job_id": job_id,
            "supervisor_pid": pid,
            "sentinel_pid": sentinel_pid,
            "process_group_id": pgid,
            "cleanup_proof_sha256": hashlib.sha256(proof_bytes).hexdigest(),
            "process_group_empty": True,
            "job_marker_census_empty": True,
            "tracked_descendants_absent": True,
            "tracked_descendant_identities": tracked_ids,
            "repository_lock_available": True,
            "operator": actor,
            "operator_confirmation": _CONFIRMATION,
            "recorded_at": time.time(),
            "disposition": "operator_attested_quiescence",
            "limits": "Machine census cannot retrospectively prove absence of a fully detached process that removed the job marker; operator independently attested no such process remains active. This receipt records operator attestation, not supervisor-generated absence proof.",
        }
        _write_private_receipt(receipt_path, receipt)
        options[_RECOVERY_OPTION] = str(receipt_path)
        manager.update_job_state(
            job_id,
            job.state,
            options=options,
            wrapper_cleanup_outcome="operator_resolved_quiescence_attested",
        )
        return {
            "ready": True,
            "applied": True,
            "job_id": job_id,
            "operator": actor,
            "receipt_recorded": True,
            "supervisor_proof_preserved": True,
            "worker_terminal_result_preserved": True,
            "quiescence_limit": receipt["limits"],
        }
    finally:
        lock_context.__exit__(None, None, None)


CONFIRMATION = _CONFIRMATION
