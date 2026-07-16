"""Job execution engine for running Codex CLI commands."""
import asyncio
import hashlib
import json
import logging
import subprocess
import re
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, Dict, Any, Optional

try:  # pragma: no cover - supported PatchBay hosts are Unix-like.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from patchbay.codex_home import resolve_codex_home
from patchbay.jobs.manager import (
    JobManager,
    JobState,
    terminal_cleanup_pending,
    terminal_cleanup_recovery_required,
)
from patchbay.jobs.process_supervisor import cleanup_proof_budget_seconds
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver
from patchbay.connector.profiles import normalize_logging_paths, resolve_runtime_path
from patchbay.repo_locks import (
    REPO_LOCK_OPERATION_OPTION,
    REPO_LOCK_OPTION,
    RepoMutationLockManager,
)
from patchbay.security import (
    internal_log_error,
    public_error_message,
    redact_sensitive_output,
    redact_text,
    validate_allowed_path,
)

logger = logging.getLogger(__name__)

STALE_RUNNING_JOB_ERROR = (
    "Job was marked running, but no live Codex process is tracked. "
    "PatchBay marked it failed so it can be inspected or restarted."
)
STALE_RUNNING_JOB_CATEGORY = "patchbay_runtime_tracking_lost"

MAX_JOB_CHECKPOINTS = 8
MAX_CHECKPOINT_TEXT_CHARS = 2_000

_SESSION_OBSERVATION_ID_OPTION = "_session_terminal_observation_session_id"
_SESSION_OBSERVATION_OFFSET_OPTION = "_session_terminal_observation_initial_offset"
_JOB_PROCESS_MARKER_ENV = "PATCHBAY_JOB_MARKER"
_JOB_PROCESS_MARKER_VERSION_OPTION = "_job_process_marker_version"
_JOB_PROCESS_LOGIN_UID_OPTION = "_job_process_login_uid"
_JOB_PROCESS_MARKER_VERSION = 1
_JOB_PROCESS_SUPERVISOR_VERSION_OPTION = "_job_process_supervisor_version"
_JOB_PROCESS_SUPERVISOR_PROOF_OPTION = "_job_process_supervisor_cleanup_proof"
_JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION = "_job_process_supervisor_launching"
_JOB_PROCESS_SUPERVISOR_SPAWNED_OPTION = "_job_process_supervisor_spawned"
_JOB_PROCESS_SUPERVISOR_VERSION = 3
_JOB_DESCENDANT_IDENTITIES_OPTION = "_job_descendant_process_identities_v1"
_JOB_DESCENDANT_UNTRUSTED_OPTION = "_job_descendant_untrusted_live_v1"
_JOB_DESCENDANT_SCAN_UNCERTAIN_OPTION = "_job_descendant_scan_uncertain_v1"
_MAX_PERSISTED_JOB_DESCENDANTS = 4096
_LINUX_PROCESS_IDENTITY_PREFIX = "linux-proc-start-v2:"
_DISCOVERY_NOT_PROVIDED = object()
# The supervisor owns descendant cleanup and may need several bounded process
# discovery passes under host contention before it can publish absence proof.
# Killing it earlier destroys the strongest proof and leaves the job correctly
# fail-closed, but unnecessarily recovery-pending.
_SUPERVISOR_CLEANUP_GRACE_FLOOR_SECONDS = cleanup_proof_budget_seconds()

_CODEX_STARTUP_LOCKS: Dict[tuple[int, str], asyncio.Lock] = {}


@dataclass
class ProcessCapture:
    stdout: bytes
    stderr: bytes
    session_id: Optional[str] = None
    session_start_timed_out: bool = False
    total_timed_out: bool = False
    semantic_terminal_seen: bool = False
    stdout_turn_completed_seen: bool = False
    stdout_turn_completed_at: Optional[float] = None
    terminal_source: str = ""
    terminal_observed_at: Optional[float] = None
    session_final_message: str = ""
    wrapper_cleanup_outcome: str = ""
    cleanup_reaped: bool = False


class _BoundedByteCapture:
    """Retain a bounded tail while streams continue draining without backpressure."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max(1, int(max_bytes))
        self._buffer = bytearray()
        self.total_bytes = 0

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        if len(chunk) >= self.max_bytes:
            self._buffer = bytearray(chunk[-self.max_bytes :])
            return
        self._buffer.extend(chunk)
        overflow = len(self._buffer) - self.max_bytes
        if overflow > 0:
            del self._buffer[:overflow]

    def value(self) -> bytes:
        return bytes(self._buffer)


@dataclass
class StartupGateLease:
    """Held while a Codex process passes through auth/session startup."""

    key: str
    lock: asyncio.Lock
    acquired_at: float
    file_handle: BinaryIO | None = None
    file_lock_path: str = ""
    released: bool = False
    release_reason: str = ""

    def release(self, reason: str = "") -> None:
        if self.released:
            return
        self.released = True
        self.release_reason = reason
        if self.file_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.file_handle.close()
        self.lock.release()


class JobExecutor:
    """
    Executes Codex jobs with conservative defaults.
    """
    
    def __init__(self, config: Dict[str, Any], job_manager: JobManager):
        normalize_logging_paths(config)
        self.config = config
        self.job_manager = job_manager
        self.schema_path = files("patchbay.protocol.schemas").joinpath("codex_output_schema.json")
        self.job_logs_dir = Path(config['logging']['job_logs_dir'])
        self.job_logs_dir.mkdir(parents=True, exist_ok=True)
        self.processes: Dict[str, asyncio.subprocess.Process] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.cleanup_tasks: Dict[str, asyncio.Task] = {}
        self.cleanup_threads: Dict[str, threading.Thread] = {}
        self._cleanup_threads_lock = threading.Lock()
        self._process_marker_cache_lock = threading.Lock()
        self._process_marker_cache_at = 0.0
        self._process_marker_cache: dict[str, set[int]] = {}
        self._process_tree_cache_lock = threading.Lock()
        self._process_tree_cache_at = 0.0
        self._process_tree_cache: Optional[dict[int, int]] = None
        self._live_job_descendants: dict[str, dict[int, Optional[str]]] = {}
        self._runtime_liveness_cache_lock = threading.Lock()
        self._runtime_liveness_cache: dict[str, Dict[str, bool]] = {}
        self._terminal_cleanup_transition_lock = threading.RLock()
        self._terminal_cleanup_completed: set[str] = set()
        self._cancellation_intents: set[str] = set()
        self.repo_locks = RepoMutationLockManager(config)
        server_config = config.get("server", {})
        max_concurrent = int(server_config.get("max_concurrent_jobs", 1) or 0)
        queue_enabled = bool(server_config.get("queue_enabled", False))
        self._execution_semaphore = asyncio.Semaphore(max_concurrent) if queue_enabled and max_concurrent > 0 else None
        self._codex_startup_locks = _CODEX_STARTUP_LOCKS

    def schedule_job(self, job_id: str) -> asyncio.Task:
        """Start a background Codex job and keep a strong task reference."""
        existing = self.tasks.get(job_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self.execute_job(job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda done_task, scheduled_job_id=job_id: self._job_task_done(scheduled_job_id, done_task))
        return task

    def _job_task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self.tasks.get(job_id) is task:
            self.tasks.pop(job_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Job %s execution task was cancelled", job_id)
        except Exception as error:
            logger.error("Job %s execution task failed: %s", job_id, internal_log_error(error))
        finally:
            job = self.job_manager.get_job(job_id)
            if job is None or not self._terminal_cleanup_pending(job):
                return
            process = self.processes.get(job_id)
            if process is not None:
                self._schedule_terminal_cleanup(job_id, process)
            else:
                self._schedule_recorded_terminal_cleanup(job_id)

    def reconcile_stale_running_jobs(
        self,
        *,
        grace_seconds: Optional[float] = None,
        now: Optional[float] = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> Dict[str, Any]:
        """Fail durable running jobs that no longer have a tracked subprocess."""
        grace = self._stale_running_grace_seconds(grace_seconds)
        current_time = time.time() if now is None else float(now)
        checked = 0
        reconciled: list[str] = []
        recovered_completed: list[str] = []
        cleanup_reconciled: list[str] = []

        for job_id, job in list(self.job_manager.jobs.items()):
            if job.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            } and self._terminal_cleanup_pending(job):
                checked += 1
                if self._terminal_cleanup_has_active_owner(job_id):
                    continue
                process = self.processes.get(job_id)
                if process is not None and getattr(process, "returncode", None) is None:
                    self._schedule_terminal_cleanup(
                        job_id, process, event_loop=event_loop
                    )
                    continue
                if self._reconcile_terminal_cleanup(job, event_loop=event_loop):
                    cleanup_reconciled.append(job_id)
                continue
            if job.state != JobState.RUNNING:
                continue
            checked += 1
            if job.terminal_source in {
                "session_task_complete",
                "stdout_turn_completed",
            } and self._recover_completed_session(job, event_loop=event_loop):
                recovered_completed.append(job_id)
                continue
            task = self.tasks.get(job_id)
            process = self.processes.get(job_id)
            if not (task is not None and not task.done()):
                if self._recover_completed_session(job, event_loop=event_loop):
                    recovered_completed.append(job_id)
                    continue
                if self._recover_completion_evidence(job, event_loop=event_loop):
                    recovered_completed.append(job_id)
                    continue
            runtime_live = self._job_has_live_runtime(job_id)
            untrusted_live = self._recorded_cleanup_has_untrusted_live_members(job)
            if runtime_live or untrusted_live:
                self._ensure_cleanup_repo_block(job)
            if runtime_live:
                continue
            if job.last_heartbeat_at is not None and current_time - float(job.last_heartbeat_at) < max(grace, 10.0):
                continue
            if job.started_at is not None and current_time - float(job.started_at) < grace:
                continue

            cleanup_outcome = (
                "cleanup_blocked_untrusted_process_identity"
                if untrusted_live
                else None
            )
            transitioned = self._transition_job_terminal_with_cleanup(
                job_id,
                JobState.FAILED,
                error=STALE_RUNNING_JOB_ERROR,
                result=self._stale_running_result(job),
                wrapper_cleanup_outcome=cleanup_outcome,
            )
            if not transitioned:
                current = self.job_manager.get_job(job_id)
                if current is not None and current.state == JobState.COMPLETED:
                    recovered_completed.append(job_id)
                continue
            if not untrusted_live:
                self.repo_locks.release_job(job_id)
            reconciled.append(job_id)
            logger.warning("Reconciled stale running job %s with no tracked process", job_id)

        orphaned_repo_leases_released = self._release_proven_terminal_repo_leases()
        self._refresh_runtime_liveness_cache()
        return {
            "checked": checked,
            "reconciled": len(reconciled),
            "job_ids": reconciled,
            "recovered_completed": len(recovered_completed),
            "recovered_completed_job_ids": recovered_completed,
            "cleanup_reconciled": len(cleanup_reconciled),
            "cleanup_reconciled_job_ids": cleanup_reconciled,
            "orphaned_repo_leases_released": len(orphaned_repo_leases_released),
            "orphaned_repo_lease_job_ids": orphaned_repo_leases_released,
            "grace_seconds": grace,
        }

    def _release_proven_terminal_repo_leases(self) -> list[str]:
        """Repair lease bookkeeping only after durable cleanup is proven.

        A non-pending terminal cleanup outcome is the durable contract that the
        complete owned process tree is gone and the repository lease was
        released. If an in-process lease or cleanup barrier still exists for
        that job, it is orphaned bookkeeping. Missing jobs and every pending or
        uncertain cleanup outcome remain fail-closed.
        """

        released: list[str] = []
        for job_id in sorted(self.repo_locks.bound_job_ids()):
            job = self.job_manager.get_job(job_id)
            cleanup_outcome = str(
                getattr(job, "wrapper_cleanup_outcome", "") or ""
            )
            if (
                job is None
                or job.state
                not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                or not cleanup_outcome
                or terminal_cleanup_pending(cleanup_outcome)
                or terminal_cleanup_recovery_required(cleanup_outcome)
                or self._terminal_cleanup_has_active_owner(job_id)
                or self._job_has_live_runtime(job_id)
                or self._recorded_cleanup_has_untrusted_live_members(job)
            ):
                continue
            self.repo_locks.release_job(job_id)
            released.append(job_id)
            logger.warning(
                "Released orphaned repository lease for proven terminal job %s",
                job_id,
            )
        return released

    async def reconcile_stale_running_jobs_async(self) -> Dict[str, Any]:
        """Run discovery off-loop and return asyncio cleanup to its owning loop."""

        event_loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            self.reconcile_stale_running_jobs,
            event_loop=event_loop,
        )

    def _recover_completed_session(
        self,
        job: Any,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        """Recover a persisted running job whose exact Codex session is terminal."""
        if getattr(job, "state", None) in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return False
        options = getattr(job, "options", None)
        resume_session_id = (
            str(options.get("resume_session_id") or "")
            if isinstance(options, dict)
            else ""
        )
        session_id = str(getattr(job, "session_id", "") or resume_session_id).strip()
        if not session_id:
            return False
        not_before = float(getattr(job, "process_started_at", None) or getattr(job, "started_at", None) or 0)
        snapshot = self._session_terminal_observer(
            session_id,
            not_before=not_before,
            initial_offset=self._session_observation_offset(job, session_id),
        ).poll()
        if not snapshot.completed:
            return False

        with self._terminal_cleanup_transition_lock:
            current = self.job_manager.get_job(job.job_id)
            if current is None:
                return False
            if current.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                self.job_manager.transition_job_terminal(
                    job.job_id,
                    JobState.COMPLETED,
                    terminal_source=snapshot.source,
                    terminal_observed_at=snapshot.observed_at or time.time(),
                )
                return False
            result_file = self.job_logs_dir / f"{job.job_id}_result.json"
            result = self._result_from_session_message(
                snapshot.final_message, result_file
            )
            # Recovery has no surviving in-process lease. Establish the
            # turnstile before terminal state is visible to another process.
            if job.job_id not in self._terminal_cleanup_completed:
                self._ensure_cleanup_repo_block(job)
            transitioned = self._transition_job_terminal_with_cleanup(
                job.job_id,
                JobState.COMPLETED,
                result=result,
                terminal_source=snapshot.source,
                terminal_observed_at=snapshot.observed_at or time.time(),
                wrapper_cleanup_outcome="cleanup_pending",
                last_heartbeat_at=time.time(),
                last_event="session_task_complete_recovered",
            )
        if not transitioned:
            return False

        recovered = self.job_manager.get_job(job.job_id)
        if recovered is not None:
            if not self._terminal_cleanup_has_active_owner(job.job_id):
                self._reconcile_terminal_cleanup(recovered, event_loop=event_loop)
        logger.info("Recovered completed Codex session for durable job %s", job.job_id)
        return True

    def _completion_evidence_result(self, job: Any) -> Dict[str, Any]:
        persisted = getattr(job, "completion_evidence_result", None)
        if isinstance(persisted, dict) and persisted:
            result = redact_sensitive_output(dict(persisted))
            result.setdefault("completion_evidence_recovered", True)
            result.setdefault(
                "completion_evidence_result_status",
                str(
                    getattr(job, "completion_evidence_result_status", "")
                    or "missing"
                ),
            )
            result.setdefault("report_completeness", "recovered")
            return result
        checkpoints = list(getattr(job, "checkpoints", None) or [])
        latest = checkpoints[-1] if checkpoints else {}
        summary = str(latest.get("summary") or "").strip()
        if not summary:
            summary = (
                "Codex emitted turn.completed, but PatchBay restarted before "
                "the final structured report artifact was persisted."
            )
        return redact_sensitive_output(
            {
                "summary": summary,
                "detailed_report": summary,
                "parsed_output_schema_valid": False,
                "final_structured_result": False,
                "report_completeness": "recovered_partial",
                "completion_evidence_recovered": True,
                "completion_evidence_result_status": (
                    "checkpoint" if latest else "missing"
                ),
                "notes": (
                    "This bounded fallback is backed by a durable stdout "
                    "turn.completed event. Ask the same worker for a fuller "
                    "report when implementation details are required."
                ),
            }
        )

    def _transition_from_completion_evidence(self, job: Any) -> bool:
        source = str(getattr(job, "completion_evidence_source", "") or "")
        if source != "stdout_turn_completed" or job.state != JobState.RUNNING:
            return False
        if int(getattr(job, "completion_evidence_version", 0) or 0) != 1:
            return False
        status = str(
            getattr(job, "completion_evidence_result_status", "") or ""
        )
        if status not in {
            "structured",
            "text",
            "checkpoint",
            "missing",
            "malformed",
            "truncated",
        }:
            return False
        evidence_session_id = str(
            getattr(job, "completion_evidence_session_id", "") or ""
        )
        options = dict(getattr(job, "options", None) or {})
        current_session_id = str(
            getattr(job, "session_id", "")
            or options.get("resume_session_id")
            or ""
        )
        if (
            evidence_session_id
            and current_session_id
            and evidence_session_id != current_session_id
        ):
            return False
        result_file = self.job_logs_dir / f"{job.job_id}_result.json"
        result = self._write_result_file(
            result_file, self._completion_evidence_result(job)
        )
        if job.job_id not in self._terminal_cleanup_completed:
            self._ensure_cleanup_repo_block(job)
        return self._transition_job_terminal_with_cleanup(
            job.job_id,
            JobState.COMPLETED,
            result=result,
            terminal_source=source,
            terminal_observed_at=(
                float(getattr(job, "completion_evidence_observed_at", None))
                if getattr(job, "completion_evidence_observed_at", None) is not None
                else time.time()
            ),
            wrapper_cleanup_outcome="cleanup_pending",
            last_heartbeat_at=time.time(),
            last_event="stdout_turn_completed_recovered",
        )

    def _recover_completion_evidence(
        self,
        job: Any,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        """Promote durable stdout completion only after exact-session recovery fails."""

        with self._terminal_cleanup_transition_lock:
            transitioned = self._transition_from_completion_evidence(job)
        if not transitioned:
            return False
        recovered = self.job_manager.get_job(job.job_id)
        if recovered is not None and not self._terminal_cleanup_has_active_owner(
            job.job_id
        ):
            self._reconcile_terminal_cleanup(
                recovered, event_loop=event_loop
            )
        logger.info(
            "Recovered durable stdout completion evidence for job %s", job.job_id
        )
        return True

    def _session_observation_offset(self, job: Any, session_id: str) -> int:
        options = getattr(job, "options", None)
        if not isinstance(options, dict):
            return 0
        observed_session_id = str(options.get(_SESSION_OBSERVATION_ID_OPTION) or "").strip()
        if observed_session_id != str(session_id or "").strip():
            return 0
        try:
            return max(0, int(options.get(_SESSION_OBSERVATION_OFFSET_OPTION) or 0))
        except (TypeError, ValueError):
            return 0

    def _session_terminal_observer(
        self,
        session_id: str,
        *,
        not_before: float,
        initial_offset: int = 0,
    ) -> CodexSessionTerminalObserver:
        """Create an exact-session observer positioned at a durable turn cursor."""

        observer = CodexSessionTerminalObserver(
            self.config,
            session_id,
            not_before=not_before,
            initial_offset=initial_offset,
        )
        return observer

    def _prepare_session_observation(self, job_id: str, job: Any) -> int:
        """Persist the pre-resume end offset before Codex can append a new turn."""

        if str(getattr(job, "mode", "") or "") != "resume":
            return 0
        options = dict(getattr(job, "options", None) or {})
        session_id = str(options.get("resume_session_id") or getattr(job, "session_id", "") or "").strip()
        if not session_id:
            return 0
        existing_offset = self._session_observation_offset(job, session_id)
        if (
            str(options.get(_SESSION_OBSERVATION_ID_OPTION) or "").strip() == session_id
            and _SESSION_OBSERVATION_OFFSET_OPTION in options
        ):
            return existing_offset

        observer = CodexSessionTerminalObserver(
            self.config,
            session_id,
            not_before=0,
        )
        initial_offset = observer.prime_to_end()
        self.job_manager.mutate_job_options(
            job_id,
            lambda current: {
                **current,
                _SESSION_OBSERVATION_ID_OPTION: session_id,
                _SESSION_OBSERVATION_OFFSET_OPTION: initial_offset,
            },
        )
        # A restart may happen after this durable cursor is recorded but before
        # Codex emits another event. Persist the resumed thread identity at the
        # same pre-launch boundary so recovery never depends on a later event.
        self.job_manager.update_job_state(job_id, job.state, session_id=session_id)
        return initial_offset

    def _persist_process_marker_contract(self, job_id: str) -> None:
        """Record marker injection capability durably before child launch."""

        job = self.job_manager.get_job(job_id)
        if job is None:
            return
        updates: dict[str, Any] = {
            _JOB_PROCESS_MARKER_VERSION_OPTION: _JOB_PROCESS_MARKER_VERSION,
        }
        if os.name == "posix":
            proof_path = str(
                (self.job_logs_dir / f"{job_id}_supervisor_cleanup.proof").resolve()
            )
            updates[_JOB_PROCESS_SUPERVISOR_VERSION_OPTION] = (
                _JOB_PROCESS_SUPERVISOR_VERSION
            )
            updates[_JOB_PROCESS_SUPERVISOR_PROOF_OPTION] = proof_path
            Path(proof_path).unlink(missing_ok=True)
        login_uid = self._linux_login_uid(Path("/proc/self"))
        if login_uid is not None:
            updates[_JOB_PROCESS_LOGIN_UID_OPTION] = login_uid
        def install(current: dict[str, Any]) -> dict[str, Any]:
            current.pop(_JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION, None)
            current.pop(_JOB_PROCESS_SUPERVISOR_SPAWNED_OPTION, None)
            current.update(updates)
            return current

        self.job_manager.mutate_job_options(job_id, install)

    def _mark_process_supervisor_launching(self, job_id: str) -> None:
        self.job_manager.mutate_job_options(
            job_id,
            lambda current: {
                **current,
                _JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION: True,
            },
        )

    def _mark_process_supervisor_spawned(self, job_id: str) -> None:
        def mark(current: dict[str, Any]) -> dict[str, Any]:
            current.pop(_JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION, None)
            current[_JOB_PROCESS_SUPERVISOR_SPAWNED_OPTION] = True
            return current

        self.job_manager.mutate_job_options(
            job_id,
            mark,
        )

    def _publish_supervisor_gated_state(self, job_id: str, supervisor_pid: int) -> None:
        """Durably confirm that a ready supervisor has not released its target."""

        job = self.job_manager.get_job(job_id)
        options = dict(getattr(job, "options", None) or {}) if job else {}
        path_value = str(options.get(_JOB_PROCESS_SUPERVISOR_PROOF_OPTION) or "")
        if not path_value or supervisor_pid <= 0:
            raise RuntimeError("Codex process supervisor has no durable gated-state path")
        path = Path(path_value).expanduser().resolve(strict=False)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"patchbay-supervisor-gated-v3:{supervisor_pid}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _persist_descendant_tracking_options(
        self,
        job_id: str,
        *,
        identities: Optional[dict[int, str]] = None,
        untrusted_live: Optional[bool] = None,
        scan_uncertain: Optional[bool] = None,
    ) -> None:
        updates: dict[str, Any] = {}
        removals: set[str] = set()
        if identities is not None:
            normalized = [
                {"pid": int(pid), "identity": str(identity)}
                for pid, identity in sorted(identities.items())
                if int(pid) > 0 and str(identity)
            ][:_MAX_PERSISTED_JOB_DESCENDANTS]
            if normalized:
                updates[_JOB_DESCENDANT_IDENTITIES_OPTION] = normalized
            else:
                removals.add(_JOB_DESCENDANT_IDENTITIES_OPTION)
        for key, value in (
            (_JOB_DESCENDANT_UNTRUSTED_OPTION, untrusted_live),
            (_JOB_DESCENDANT_SCAN_UNCERTAIN_OPTION, scan_uncertain),
        ):
            if value is None:
                continue
            if value:
                updates[key] = True
            else:
                removals.add(key)
        if updates or removals:
            def mutate(current: dict[str, Any]) -> dict[str, Any]:
                for key in removals:
                    current.pop(key, None)
                current.update(updates)
                return current

            self.job_manager.mutate_job_options(job_id, mutate)

    def _persisted_job_descendant_identities(self, job: Any) -> dict[int, str]:
        options = getattr(job, "options", None)
        raw = (
            options.get(_JOB_DESCENDANT_IDENTITIES_OPTION)
            if isinstance(options, dict)
            else None
        )
        identities: dict[int, str] = {}
        if not isinstance(raw, list):
            return identities
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            identity = str(item.get("identity") or "").strip()
            if pid > 0 and identity:
                identities[pid] = identity
        return identities

    def _process_parent_snapshot(
        self, *, force_refresh: bool = False
    ) -> Optional[dict[int, int]]:
        now = time.monotonic()
        with self._process_tree_cache_lock:
            if (
                not force_refresh
                and self._process_tree_cache is not None
                and now - self._process_tree_cache_at < 0.2
            ):
                return dict(self._process_tree_cache)

        proc = Path("/proc")
        parents: dict[int, int] = {}
        complete = True
        if proc.is_dir():
            try:
                entries = list(proc.iterdir())
            except OSError:
                entries = []
                complete = False
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    stat_text = (entry / "stat").read_text(encoding="utf-8")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    complete = False
                    continue
                _, separator, suffix = stat_text.rpartition(")")
                fields = suffix.strip().split() if separator else []
                if len(fields) < 2 or fields[0] == "Z":
                    continue
                try:
                    parents[int(entry.name)] = int(fields[1])
                except ValueError:
                    complete = False
        else:
            try:
                observed = subprocess.run(
                    ["ps", "-axo", "pid=,ppid=,stat="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                observed = None
            if observed is None or observed.returncode != 0:
                complete = False
            else:
                for line in observed.stdout.splitlines():
                    fields = line.strip().split(None, 2)
                    if len(fields) < 3:
                        complete = False
                        continue
                    try:
                        pid = int(fields[0])
                        parent = int(fields[1])
                    except ValueError:
                        complete = False
                        continue
                    if not fields[2].startswith("Z"):
                        parents[pid] = parent

        snapshot = parents if complete else None
        with self._process_tree_cache_lock:
            self._process_tree_cache = dict(snapshot) if snapshot is not None else None
            self._process_tree_cache_at = now
        return dict(snapshot) if snapshot is not None else None

    def _current_descendant_pids(
        self, root_pid: int, *, force_refresh: bool = False
    ) -> Optional[set[int]]:
        if root_pid <= 0:
            return set()
        parents = self._process_parent_snapshot(force_refresh=force_refresh)
        if parents is None:
            return None
        descendants: set[int] = set()
        frontier = {root_pid}
        while frontier:
            found = {
                pid
                for pid, parent in parents.items()
                if parent in frontier and pid not in descendants and pid != root_pid
            }
            if not found:
                break
            descendants.update(found)
            frontier = found
        return descendants

    def _capture_job_descendants(
        self, job_id: str, root_pid: int, *, force_refresh: bool = False
    ) -> Optional[set[int]]:
        descendants = self._current_descendant_pids(
            root_pid, force_refresh=force_refresh
        )
        if descendants is None:
            self._persist_descendant_tracking_options(
                job_id, scan_uncertain=True
            )
            return None
        tracked = self._live_job_descendants.setdefault(job_id, {})
        exact: dict[int, str] = {}
        untrusted = False
        for pid in descendants:
            identity = self._process_identity(pid)
            tracked[pid] = identity
            if identity:
                exact[pid] = identity
            else:
                untrusted = True
        job = self.job_manager.get_job(job_id)
        if job is not None:
            exact = {**self._persisted_job_descendant_identities(job), **exact}
        if len(exact) > _MAX_PERSISTED_JOB_DESCENDANTS:
            untrusted = True
        self._persist_descendant_tracking_options(
            job_id,
            identities=exact,
            untrusted_live=(
                True
                if untrusted
                else None
            ),
            scan_uncertain=False,
        )
        return set(descendants)

    def _tracked_descendant_liveness(self, job_id: str) -> Optional[bool]:
        job = self.job_manager.get_job(job_id)
        if job is None:
            return False
        options = dict(getattr(job, "options", None) or {})
        exact = self._persisted_job_descendant_identities(job)
        tracked = self._live_job_descendants.get(job_id, {})
        for pid, identity in tracked.items():
            if identity:
                exact.setdefault(pid, identity)
        exact_live = False
        for pid, identity in list(exact.items()):
            if self._process_pid_is_live(pid) and self._process_identity(pid) == identity:
                exact_live = True
            else:
                exact.pop(pid, None)
        unknown_pids = {
            pid
            for pid, identity in tracked.items()
            if not identity and self._process_pid_is_live(pid)
        }
        if tracked:
            self._live_job_descendants[job_id] = {
                pid: identity
                for pid, identity in tracked.items()
                if (identity and pid in exact) or (not identity and pid in unknown_pids)
            }
            if not self._live_job_descendants[job_id]:
                self._live_job_descendants.pop(job_id, None)
        persisted_unknown = bool(options.get(_JOB_DESCENDANT_UNTRUSTED_OPTION))
        scan_uncertain = bool(options.get(_JOB_DESCENDANT_SCAN_UNCERTAIN_OPTION))
        if (
            scan_uncertain
            and job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
            and not exact_live
            and not unknown_pids
            and self._fresh_discovery_disproves_uncertain_descendants(job_id, job)
        ):
            self._persist_descendant_tracking_options(
                job_id,
                identities=exact,
                scan_uncertain=False,
            )
            scan_uncertain = False
        self._persist_descendant_tracking_options(
            job_id,
            identities=exact,
            untrusted_live=(False if persisted_unknown and not unknown_pids else None),
        )
        if exact_live:
            return True
        if unknown_pids or (persisted_unknown and not tracked) or scan_uncertain:
            return None
        return False

    def _fresh_discovery_disproves_uncertain_descendants(
        self, job_id: str, job: Any
    ) -> bool:
        """Clear stale scan uncertainty only from a complete fresh observation."""

        if self._supervisor_cleanup_contract_installed(job):
            return self._supervisor_cleanup_proven(job_id)
        if self._process_parent_snapshot(force_refresh=True) is None:
            return False
        raw_pid = getattr(job, "process_pid", None)
        if isinstance(raw_pid, int) and self._process_pid_is_live(raw_pid):
            return False
        raw_pgid = getattr(job, "process_pgid", None)
        group_liveness = self._process_group_liveness(
            int(raw_pgid) if isinstance(raw_pgid, int) else 0
        )
        if group_liveness is not False:
            return False
        options = dict(getattr(job, "options", None) or {})
        if options.get(_JOB_PROCESS_MARKER_VERSION_OPTION) == _JOB_PROCESS_MARKER_VERSION:
            marked = self._job_marked_process_pids(job_id, force_refresh=True)
            if marked is None or marked:
                return False
        return all(
            not self._process_pid_is_live(pid)
            or self._process_identity(pid) != identity
            for pid, identity in self._persisted_job_descendant_identities(job).items()
        )

    def _signal_exact_job_descendants(self, job_id: str, sig: signal.Signals) -> bool:
        job = self.job_manager.get_job(job_id)
        if job is None:
            return False
        signalled = False
        for pid, identity in sorted(
            self._persisted_job_descendant_identities(job).items(), reverse=True
        ):
            if self._process_identity(pid) != identity:
                continue
            try:
                os.kill(pid, sig)
                signalled = True
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                logger.warning(
                    "Could not signal exact descendant for job %s pid=%s",
                    job_id,
                    pid,
                )
        return signalled

    def _signal_live_job_descendants(
        self, job_id: str, sig: signal.Signals
    ) -> bool:
        """Signal descendants captured by this live executor instance.

        An in-memory ancestry capture is sufficient authority during the same
        executor lifetime. Persisted/restarted cleanup still requires an exact
        Linux identity and uses ``_signal_exact_job_descendants`` instead.
        """

        signalled = False
        tracked = dict(self._live_job_descendants.get(job_id, {}))
        for pid, identity in sorted(tracked.items(), reverse=True):
            if identity and self._process_identity(pid) != identity:
                continue
            if not self._process_pid_is_live(pid):
                continue
            try:
                os.kill(pid, sig)
                signalled = True
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                self._persist_descendant_tracking_options(
                    job_id, untrusted_live=True
                )
        return signalled

    async def _drain_current_job_descendants(
        self,
        job_id: str,
        root_pid: int,
        *,
        graceful_timeout: float,
    ) -> bool:
        """Capture and terminate descendants without adding a second grace window."""

        del graceful_timeout
        self._capture_job_descendants(job_id, root_pid, force_refresh=True)
        signalled = self._signal_live_job_descendants(job_id, signal.SIGTERM)
        if self._signal_exact_job_descendants(job_id, signal.SIGTERM):
            signalled = True
        await asyncio.sleep(0)
        return signalled

    def _terminal_cleanup_pending(self, job: Any) -> bool:
        return terminal_cleanup_pending(getattr(job, "wrapper_cleanup_outcome", ""))

    def _transition_job_terminal_with_cleanup(
        self,
        job_id: str,
        state: JobState,
        **kwargs: Any,
    ) -> bool:
        """Keep a proven cleanup outcome from regressing to a pending state."""

        with self._terminal_cleanup_transition_lock:
            current = self.job_manager.get_job(job_id)
            incoming = str(kwargs.get("wrapper_cleanup_outcome") or "")
            existing = str(
                getattr(current, "wrapper_cleanup_outcome", "") or ""
            )
            if (
                current is not None
                and current.state
                in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                and existing
                and not terminal_cleanup_pending(existing)
                and incoming != existing
            ):
                kwargs.pop("wrapper_cleanup_outcome", None)
            return self.job_manager.transition_job_terminal(job_id, state, **kwargs)

    def _complete_terminal_cleanup(self, job_id: str, outcome: str) -> None:
        """Release the cleanup barrier before making completion durable."""

        if not outcome or terminal_cleanup_pending(outcome):
            outcome = "process_not_live_after_terminal"
        with self._terminal_cleanup_transition_lock:
            self.processes.pop(job_id, None)
            self.repo_locks.release_job(job_id)
            self._terminal_cleanup_completed.add(job_id)
            current = self.job_manager.get_job(job_id)
            if current is None or current.state not in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                return
            existing_outcome = str(current.wrapper_cleanup_outcome or "")
            if existing_outcome and not terminal_cleanup_pending(existing_outcome):
                return
            self._transition_job_terminal_with_cleanup(
                job_id,
                current.state,
                wrapper_cleanup_outcome=outcome,
                last_heartbeat_at=time.time(),
            )

    def _terminal_cleanup_has_active_owner(self, job_id: str) -> bool:
        cleanup_task = self.cleanup_tasks.get(job_id)
        if cleanup_task is not None and not cleanup_task.done():
            return True
        with self._cleanup_threads_lock:
            cleanup_thread = self.cleanup_threads.get(job_id)
        if cleanup_thread is not None and cleanup_thread.is_alive():
            return True
        process = self.processes.get(job_id)
        task = self.tasks.get(job_id)
        return bool(
            process is not None
            and self._tracked_process_or_group_is_live(job_id, process)
            and task is not None
            and not task.done()
        )

    @staticmethod
    def _cancel_task_on_owner_loop(
        task: asyncio.Task,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """Marshal task cancellation to the loop that owns the task."""

        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
        loop = event_loop
        if loop is None:
            try:
                loop = task.get_loop()
            except (AttributeError, RuntimeError):
                loop = None
        if loop is not None:
            loop.call_soon_threadsafe(task.cancel)
            return
        # Only loop-owned asyncio tasks reach this path in normal execution.
        # A test double without loop metadata is safe to cancel synchronously.
        task.cancel()

    def _job_requires_cleanup_repo_block(self, job: Any) -> bool:
        options = getattr(job, "options", None)
        return bool(isinstance(options, dict) and options.get(REPO_LOCK_OPTION))

    def _ensure_cleanup_repo_block(self, job: Any) -> None:
        if not self._job_requires_cleanup_repo_block(job):
            return
        options = job.options or {}
        self.repo_locks.block_job_cleanup(
            job.job_id,
            job.repo_path,
            operation=str(options.get(REPO_LOCK_OPERATION_OPTION) or "codex_terminal_cleanup"),
        )

    def _reconcile_terminal_cleanup(
        self,
        job: Any,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        with self._terminal_cleanup_transition_lock:
            return self._reconcile_terminal_cleanup_locked(
                job, event_loop=event_loop
            )

    def _reconcile_terminal_cleanup_locked(
        self,
        job: Any,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        """Finish or safely retain ownership of one semantically complete turn."""

        if job.job_id in self._terminal_cleanup_completed:
            existing_outcome = str(
                getattr(job, "wrapper_cleanup_outcome", "") or ""
            )
            self._complete_terminal_cleanup(
                job.job_id,
                existing_outcome
                if existing_outcome and not terminal_cleanup_pending(existing_outcome)
                else "process_not_live_after_terminal",
            )
            return True
        self._ensure_cleanup_repo_block(job)
        job_options = dict(getattr(job, "options", None) or {})
        if (
            job_options.get(_JOB_PROCESS_SUPERVISOR_VERSION_OPTION)
            == _JOB_PROCESS_SUPERVISOR_VERSION
            and job_options.get(_JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION)
            is not True
            and job_options.get(_JOB_PROCESS_SUPERVISOR_SPAWNED_OPTION) is not True
        ):
            # The durable launch-request boundary was never crossed. No
            # supervisor or target could have existed, so global process-table
            # discovery is both unnecessary and less reliable on macOS.
            self._complete_terminal_cleanup(
                job.job_id, "cancelled_before_supervisor_launch"
            )
            return True
        if self._supervisor_cleanup_proven(job.job_id):
            self._complete_terminal_cleanup(
                job.job_id,
                "supervisor_proved_no_descendants_after_terminal",
            )
            return True
        process = self.processes.get(job.job_id)
        tracked_live = bool(
            process is not None
            and self._tracked_process_or_group_is_live(job.job_id, process)
        )
        pid = getattr(job, "process_pid", None)
        raw_pid_live = bool(isinstance(pid, int) and self._process_pid_is_live(pid))
        pid_trusted = self._recorded_process_pid_is_trustworthy(job)
        pid_live = bool(raw_pid_live and pid_trusted)
        if pid_live:
            self._schedule_recorded_terminal_cleanup(job.job_id)
            return False
        pgid = getattr(job, "process_pgid", None)
        group_liveness = self._process_group_liveness(
            int(pgid) if isinstance(pgid, int) else 0
        )
        raw_group_live = group_liveness is True
        group_trusted = bool(
            raw_group_live and self._recorded_process_group_is_trustworthy(job)
        )
        group_live = bool(raw_group_live and group_trusted)
        job_options = getattr(job, "options", None)
        marker_contract_installed = bool(
            isinstance(job_options, dict)
            and job_options.get(_JOB_PROCESS_MARKER_VERSION_OPTION)
            == _JOB_PROCESS_MARKER_VERSION
        )
        marked_pids = (
            self._job_marked_process_pids(job.job_id)
            if marker_contract_installed
            else set()
        )
        marked_live = bool(marked_pids)
        descendant_liveness = self._tracked_descendant_liveness(job.job_id)
        cleanup_outcome = "process_not_live_after_terminal"

        if tracked_live and process is not None:
            self._schedule_terminal_cleanup(
                job.job_id, process, event_loop=event_loop
            )
            return False
        if group_live or marked_live or descendant_liveness is True:
            self._schedule_recorded_terminal_cleanup(job.job_id)
            return False
        if (
            descendant_liveness is None
            or self._recorded_cleanup_has_untrusted_live_members(
                job,
                marked_pids=marked_pids,
                group_liveness=group_liveness,
                descendant_liveness=descendant_liveness,
            )
        ):
            cleanup_outcome = "cleanup_blocked_untrusted_process_identity"

        cleanup_complete = cleanup_outcome != "cleanup_blocked_untrusted_process_identity"
        if cleanup_complete:
            task = self.tasks.get(job.job_id)
            if task is not None and not task.done():
                self._cancel_task_on_owner_loop(task, event_loop)
                self._transition_job_terminal_with_cleanup(
                    job.job_id,
                    job.state,
                    wrapper_cleanup_outcome="cleanup_pending",
                    last_heartbeat_at=time.time(),
                )
                return False
            self._complete_terminal_cleanup(job.job_id, cleanup_outcome)
            return True

        self._transition_job_terminal_with_cleanup(
            job.job_id,
            job.state,
            wrapper_cleanup_outcome=cleanup_outcome,
            last_heartbeat_at=time.time(),
        )
        return False

    def _schedule_recorded_terminal_cleanup(self, job_id: str) -> None:
        """Move restart cleanup off the Edge request/event-loop thread."""

        with self._cleanup_threads_lock:
            existing = self.cleanup_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._recorded_terminal_cleanup_worker,
                args=(job_id,),
                name=f"patchbay-recorded-cleanup-{job_id}",
                daemon=True,
            )
            self.cleanup_threads[job_id] = thread
            thread.start()

    def _recorded_terminal_cleanup_worker(self, job_id: str) -> None:
        try:
            job = self.job_manager.get_job(job_id)
            if job is None:
                self.repo_locks.release_job(job_id)
                return
            outcome = self._terminate_recorded_process(job)
            current = self.job_manager.get_job(job_id)
            if current is None:
                self.repo_locks.release_job(job_id)
                return
            pid = getattr(current, "process_pid", None)
            trusted_pid_live = bool(
                isinstance(pid, int)
                and self._recorded_process_pid_is_trustworthy(current)
                and self._process_pid_is_live(pid)
            )
            trusted_group_live = bool(
                self._recorded_process_group_is_trustworthy(current)
                and self._process_group_has_live_members(
                    int(getattr(current, "process_pgid", None) or 0)
                )
            )
            marked_live = bool(self._job_marked_process_pids(job_id))
            descendant_liveness = self._tracked_descendant_liveness(job_id)
            untrusted_live = self._recorded_cleanup_has_untrusted_live_members(current)
            if (
                not trusted_pid_live
                and not trusted_group_live
                and not marked_live
                and descendant_liveness is False
                and not untrusted_live
            ):
                self._complete_terminal_cleanup(job_id, outcome)
            else:
                if untrusted_live:
                    outcome = "cleanup_blocked_untrusted_process_identity"
                self._transition_job_terminal_with_cleanup(
                    job_id,
                    current.state,
                    wrapper_cleanup_outcome=outcome,
                    last_heartbeat_at=time.time(),
                )
        except Exception as error:
            logger.error(
                "Recorded terminal cleanup for job %s failed: %s",
                job_id,
                internal_log_error(error),
            )
        finally:
            with self._cleanup_threads_lock:
                current_thread = self.cleanup_threads.get(job_id)
                if current_thread is threading.current_thread():
                    self.cleanup_threads.pop(job_id, None)

    def _terminate_recorded_process(self, job: Any) -> str:
        """Terminate a persisted process only after its exact start marker matches."""
        raw_pid = getattr(job, "process_pid", None)
        pid = int(raw_pid) if isinstance(raw_pid, int) and raw_pid > 0 else 0
        raw_pgid = getattr(job, "process_pgid", None)
        pgid = int(raw_pgid) if isinstance(raw_pgid, int) and raw_pgid > 0 else pid
        leader_trusted = self._recorded_process_pid_is_trustworthy(job)
        leader_live = bool(pid > 0 and leader_trusted and self._process_pid_is_live(pid))
        group_trusted = self._recorded_process_group_is_trustworthy(job)
        group_live = bool(pgid > 0 and group_trusted and self._process_group_has_live_members(pgid))
        marked_live = bool(self._job_marked_process_pids(job.job_id))
        descendant_liveness = self._tracked_descendant_liveness(job.job_id)
        if not leader_live and not group_live and not marked_live and descendant_liveness is False:
            if self._recorded_cleanup_has_untrusted_live_members(job):
                return "cleanup_blocked_untrusted_process_identity"
            return "process_not_live"
        if descendant_liveness is None:
            return "cleanup_blocked_untrusted_process_identity"
        try:
            if group_live:
                os.killpg(pgid, signal.SIGTERM)
            elif leader_live:
                os.kill(pid, signal.SIGTERM)
            self._signal_job_marked_processes(job.job_id, signal.SIGTERM)
            self._signal_exact_job_descendants(job.job_id, signal.SIGTERM)
        except ProcessLookupError:
            if not self._recorded_cleanup_members_live(job):
                return "process_not_live"
            return "cleanup_signal_failed"
        except (PermissionError, OSError):
            return "cleanup_signal_failed"
        deadline = time.time() + self._post_completion_exit_grace_seconds()
        while self._recorded_cleanup_members_live(job) and time.time() < deadline:
            time.sleep(0.05)
        if self._recorded_cleanup_members_live(job):
            try:
                if self._recorded_process_group_is_trustworthy(job) and self._process_group_has_live_members(pgid):
                    os.killpg(pgid, signal.SIGKILL)
                elif self._recorded_process_pid_is_trustworthy(job):
                    os.kill(pid, signal.SIGKILL)
                self._signal_job_marked_processes(job.job_id, signal.SIGKILL)
                self._signal_exact_job_descendants(job.job_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                return "cleanup_kill_failed"
            kill_deadline = time.time() + self._post_completion_cleanup_timeout_seconds()
            while self._recorded_cleanup_members_live(job) and time.time() < kill_deadline:
                time.sleep(0.05)
        if self._recorded_cleanup_members_live(job):
            return "cleanup_retry_pending_process_live"
        return "terminated_after_terminal_recovery"

    def _recorded_cleanup_members_live(self, job: Any) -> bool:
        pid = getattr(job, "process_pid", None)
        pgid = getattr(job, "process_pgid", None)
        descendant_liveness = self._tracked_descendant_liveness(
            str(getattr(job, "job_id", "") or "")
        )
        return bool(
            (
                isinstance(pid, int)
                and self._recorded_process_pid_is_trustworthy(job)
                and self._process_pid_is_live(pid)
            )
            or (
                isinstance(pgid, int)
                and self._recorded_process_group_is_trustworthy(job)
                and self._process_group_has_live_members(pgid)
            )
            or self._job_marked_process_pids(str(getattr(job, "job_id", "") or ""))
            or descendant_liveness is not False
            or self._supervisor_cleanup_unproven(
                str(getattr(job, "job_id", "") or "")
            )
        )

    def _recorded_cleanup_has_untrusted_live_members(
        self,
        job: Any,
        *,
        marked_pids: Any = _DISCOVERY_NOT_PROVIDED,
        group_liveness: Any = _DISCOVERY_NOT_PROVIDED,
        descendant_liveness: Any = _DISCOVERY_NOT_PROVIDED,
    ) -> bool:
        """Detect live persisted references whose ownership cannot be resolved."""

        pid = getattr(job, "process_pid", None)
        pgid = getattr(job, "process_pgid", None)
        pid_ownership_disproved = self._exact_linux_identity_disproves_ownership(job)
        options = getattr(job, "options", None)
        marker_contract_installed = bool(
            isinstance(options, dict)
            and options.get(_JOB_PROCESS_MARKER_VERSION_OPTION)
            == _JOB_PROCESS_MARKER_VERSION
        )
        job_id = str(getattr(job, "job_id", "") or "")
        if marked_pids is _DISCOVERY_NOT_PROVIDED:
            marked_pids = (
                self._job_marked_process_pids(job_id)
                if marker_contract_installed
                else set()
            )
        if group_liveness is _DISCOVERY_NOT_PROVIDED:
            group_liveness = self._process_group_liveness(
                int(pgid) if isinstance(pgid, int) else 0
            )
        group_ownership_disproved = bool(
            marker_contract_installed and marked_pids is not None and not marked_pids
        )
        marker_scan_unknown = bool(
            marker_contract_installed
            and marked_pids is None
        )
        if descendant_liveness is _DISCOVERY_NOT_PROVIDED:
            descendant_liveness = self._tracked_descendant_liveness(job_id)
        return bool(
            marker_scan_unknown
            or descendant_liveness is None
            or self._supervisor_cleanup_unproven(job_id)
            or (isinstance(pgid, int) and pgid > 0 and group_liveness is None)
            or
            (
                isinstance(pid, int)
                and self._process_pid_is_live(pid)
                and not self._recorded_process_pid_is_trustworthy(job)
                and not pid_ownership_disproved
            )
            or (
                isinstance(pgid, int)
                and group_liveness is True
                and not self._recorded_process_group_is_trustworthy(job)
                and not group_ownership_disproved
            )
        )

    def _exact_linux_identity_disproves_ownership(self, job: Any) -> bool:
        """Return true when exact Linux start ticks prove PID reuse."""

        recorded_identity = str(getattr(job, "process_identity", "") or "")
        current_identity = str(
            self._process_identity(getattr(job, "process_pid", None)) or ""
        )
        return bool(
            recorded_identity.startswith(_LINUX_PROCESS_IDENTITY_PREFIX)
            and current_identity.startswith(_LINUX_PROCESS_IDENTITY_PREFIX)
            and recorded_identity != current_identity
        )

    def _result_from_session_message(self, final_message: str, result_file: Path) -> Dict[str, Any]:
        text = str(final_message or "").strip()
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict):
            schema_valid = self._structured_result_is_valid(parsed)
            result = dict(parsed)
            result.setdefault("files_changed", [])
            result["parsed_output_schema_valid"] = schema_valid
            result["final_structured_result"] = schema_valid
        else:
            result = {
                "summary": text or "Codex completed the turn without a final message.",
                "files_changed": [],
                "parsed_output_schema_valid": False,
                "final_structured_result": False,
            }
        result["result_source"] = "session_task_complete"
        result["turn_completed_seen"] = True
        return self._write_result_file(result_file, result)

    def _structured_result_is_valid(self, value: Any) -> bool:
        """Validate the small Codex result contract without an extra runtime dependency."""

        if not isinstance(value, dict):
            return False
        try:
            schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        required = set(schema.get("required") or [])
        properties = schema.get("properties") or {}
        if not required.issubset(value):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        for key, item in value.items():
            spec = properties.get(key)
            if not isinstance(spec, dict) or not self._value_matches_schema(item, spec):
                return False
        return True

    @classmethod
    def _value_matches_schema(cls, value: Any, schema: Dict[str, Any]) -> bool:
        expected = schema.get("type")
        if expected == "string":
            return isinstance(value, str)
        if expected == "array":
            if not isinstance(value, list):
                return False
            item_schema = schema.get("items")
            return not isinstance(item_schema, dict) or all(
                cls._value_matches_schema(item, item_schema) for item in value
            )
        if expected == "object":
            return isinstance(value, dict)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return True

    def _runtime_liveness(self, job_id: str) -> Dict[str, bool]:
        """Separate executor-task liveness from real Codex process liveness."""
        task = self.tasks.get(job_id)
        # Once the exact cleanup barrier is complete, the executor coroutine
        # may still be unwinding its final ``finally`` blocks. That epilogue
        # owns no Codex process or repository lock and must not reject the next
        # turn as an active runtime.
        executor_task_alive = bool(
            task is not None
            and not task.done()
            and job_id not in self._terminal_cleanup_completed
        )
        process = self.processes.get(job_id)
        tracked_process_alive = bool(
            process is not None and getattr(process, "returncode", None) is None
        )
        job = self.job_manager.get_job(job_id)
        tracked_group_alive = bool(
            process is not None
            and self._process_group_has_live_members(int(getattr(process, "pid", 0) or 0))
        )
        recorded_pid_alive = bool(
            job
            and job.process_pid
            and self._recorded_process_pid_is_trustworthy(job)
            and self._process_pid_is_live(int(job.process_pid))
        )
        recorded_group_alive = bool(
            job
            and self._recorded_process_group_is_trustworthy(job)
            and self._process_group_has_live_members(int(job.process_pgid))
        )
        marked_descendants_alive = bool(job and self._job_marked_process_pids(job_id))
        descendant_liveness = self._tracked_descendant_liveness(job_id)
        tracked_descendants_alive = descendant_liveness is True
        supervisor_cleanup_unproven = bool(
            job and self._supervisor_cleanup_unproven(job_id, process=process)
        )
        descendant_liveness_unknown = (
            descendant_liveness is None or supervisor_cleanup_unproven
        )
        process_alive = (
            tracked_process_alive
            or tracked_group_alive
            or recorded_pid_alive
            or recorded_group_alive
            or marked_descendants_alive
            or tracked_descendants_alive
        )
        return {
            "executor_task_alive": executor_task_alive,
            "tracked_process_alive": tracked_process_alive,
            "tracked_group_alive": tracked_group_alive,
            "recorded_pid_alive": recorded_pid_alive,
            "recorded_group_alive": recorded_group_alive,
            "marked_descendants_alive": marked_descendants_alive,
            "tracked_descendants_alive": tracked_descendants_alive,
            "descendant_liveness_unknown": descendant_liveness_unknown,
            "supervisor_cleanup_unproven": supervisor_cleanup_unproven,
            "process_alive": process_alive,
            "runtime_alive": executor_task_alive or process_alive,
        }

    def _refresh_runtime_liveness_cache(self) -> None:
        snapshot: dict[str, Dict[str, bool]] = {}
        for job_id in list(self.job_manager.jobs):
            try:
                snapshot[job_id] = self._runtime_liveness(job_id)
            except Exception:
                continue
        with self._runtime_liveness_cache_lock:
            self._runtime_liveness_cache = snapshot

    def runtime_liveness_snapshot(self, job_id: str) -> Dict[str, bool]:
        """Return the latest reconciled liveness without process discovery."""

        with self._runtime_liveness_cache_lock:
            cached = self._runtime_liveness_cache.get(job_id)
            if cached is not None:
                return dict(cached)
        task = self.tasks.get(job_id)
        process = self.processes.get(job_id)
        executor_task_alive = bool(
            task is not None
            and not task.done()
            and job_id not in self._terminal_cleanup_completed
        )
        tracked_process_alive = bool(
            process is not None and getattr(process, "returncode", None) is None
        )
        return {
            "executor_task_alive": executor_task_alive,
            "tracked_process_alive": tracked_process_alive,
            "tracked_group_alive": False,
            "recorded_pid_alive": False,
            "recorded_group_alive": False,
            "marked_descendants_alive": False,
            "tracked_descendants_alive": False,
            "descendant_liveness_unknown": False,
            "supervisor_cleanup_unproven": False,
            "process_alive": tracked_process_alive,
            "runtime_alive": executor_task_alive or tracked_process_alive,
        }

    def _job_has_live_runtime(self, job_id: str) -> bool:
        return self._runtime_liveness(job_id)["runtime_alive"]

    def _recorded_process_pid_is_trustworthy(self, job: Any) -> bool:
        """Trust a persisted pid only while its exact start identity matches."""
        recorded_identity = str(getattr(job, "process_identity", "") or "")
        return bool(
            recorded_identity
            and self._process_identity(getattr(job, "process_pid", None))
            == recorded_identity
        )

    def _recorded_process_group_is_trustworthy(self, job: Any) -> bool:
        """Trust only the dedicated session group recorded at process launch."""

        pid = getattr(job, "process_pid", None)
        pgid = getattr(job, "process_pgid", None)
        if not isinstance(pid, int) or not isinstance(pgid, int) or pid <= 0 or pgid != pid:
            return False
        try:
            if pgid == os.getpgrp():
                return False
        except OSError:
            return False
        if self._recorded_process_pid_is_trustworthy(job):
            return True
        options = getattr(job, "options", None)
        if not (
            isinstance(options, dict)
            and options.get(_JOB_PROCESS_MARKER_VERSION_OPTION)
            == _JOB_PROCESS_MARKER_VERSION
        ):
            return False
        group_members = self._process_group_members_from_proc(pgid)
        marked_members = self._job_marked_process_pids(str(getattr(job, "job_id", "") or ""))
        return bool(
            group_members is not None
            and marked_members is not None
            and group_members.intersection(marked_members)
        )

    def _tracked_process_or_group_is_live(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
    ) -> bool:
        return self._tracked_process_or_group_liveness(job_id, process) is not False

    def _tracked_process_or_group_liveness(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
    ) -> Optional[bool]:
        """Return true/false only when tracked cleanup presence is proven."""

        if getattr(process, "returncode", None) is None:
            return True
        if self._supervisor_cleanup_proven(job_id, process=process):
            return False
        job = self.job_manager.get_job(job_id)
        pgid = int(
            (getattr(job, "process_pgid", None) if job is not None else None)
            or getattr(process, "pid", 0)
            or 0
        )
        group_liveness = self._process_group_liveness(pgid)
        if group_liveness is True:
            return True
        marked_pids = self._job_marked_process_pids(job_id)
        if marked_pids:
            return True
        descendant_liveness = self._tracked_descendant_liveness(job_id)
        if descendant_liveness is True:
            return True
        if group_liveness is None:
            return None
        if marked_pids is None:
            return None
        if descendant_liveness is None:
            return None
        if self._supervisor_cleanup_unproven(job_id, process=process):
            return None
        return False

    def _supervisor_cleanup_proven(
        self,
        job_id: str,
        *,
        process: Optional[asyncio.subprocess.Process] = None,
    ) -> bool:
        job = self.job_manager.get_job(job_id)
        options = dict(getattr(job, "options", None) or {}) if job else {}
        if not self._supervisor_cleanup_contract_installed(job):
            return False
        if process is not None and getattr(process, "returncode", None) is None:
            return False
        pid = int(
            getattr(process, "pid", 0)
            if process is not None
            else getattr(job, "process_pid", 0) or 0
        )
        path_value = str(options.get(_JOB_PROCESS_SUPERVISOR_PROOF_OPTION) or "")
        if not path_value:
            return False
        try:
            proof = Path(path_value).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return False
        if pid > 0:
            if proof == f"patchbay-supervisor-cleanup-v2:{pid}":
                return True
            return bool(
                proof == f"patchbay-supervisor-gated-v3:{pid}"
                and not self._process_pid_is_live(pid)
            )
        matched = re.fullmatch(r"patchbay-supervisor-cleanup-v2:(\d+)", proof)
        gated = re.fullmatch(r"patchbay-supervisor-gated-v3:(\d+)", proof)
        if matched is None and gated is None:
            return False
        proof_pid = int((matched or gated).group(1))
        # The supervisor is still behind its parent launch gate if PatchBay
        # crashes before PID persistence. It writes this job-private proof and
        # exits without releasing the target; trust it only after that PID is no
        # longer live. PID reuse can delay recovery but cannot forge absence.
        return proof_pid > 0 and not self._process_pid_is_live(proof_pid)

    def _supervisor_cleanup_uncertain(
        self,
        job_id: str,
        *,
        process: Optional[asyncio.subprocess.Process] = None,
    ) -> bool:
        """Return whether the supervisor explicitly withheld an absence proof."""

        job = self.job_manager.get_job(job_id)
        options = dict(getattr(job, "options", None) or {}) if job else {}
        if not self._supervisor_cleanup_contract_installed(job):
            return False
        pid = int(
            getattr(process, "pid", 0)
            if process is not None
            else getattr(job, "process_pid", 0) or 0
        )
        path_value = str(options.get(_JOB_PROCESS_SUPERVISOR_PROOF_OPTION) or "")
        if pid <= 0 or not path_value:
            return False
        try:
            record = Path(path_value).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return False
        expected = f"patchbay-supervisor-cleanup-unproven-v2:{pid}"
        return bool(
            record == expected
            or record.startswith(f"{expected}:")
        )

    def _supervisor_cleanup_contract_installed(self, job: Any) -> bool:
        options = dict(getattr(job, "options", None) or {}) if job else {}
        version = options.get(_JOB_PROCESS_SUPERVISOR_VERSION_OPTION)
        proof_path = str(options.get(_JOB_PROCESS_SUPERVISOR_PROOF_OPTION) or "")
        if version == 2:
            # Legacy releases had no spawned boundary. Keep their existing
            # records fail-closed rather than silently relaxing cleanup.
            return bool(proof_path)
        return bool(
            version == _JOB_PROCESS_SUPERVISOR_VERSION
            and options.get(_JOB_PROCESS_SUPERVISOR_SPAWNED_OPTION) is True
            and proof_path
        )

    def _supervisor_cleanup_unproven(
        self,
        job_id: str,
        *,
        process: Optional[asyncio.subprocess.Process] = None,
    ) -> bool:
        job = self.job_manager.get_job(job_id)
        return bool(
            self._supervisor_cleanup_contract_installed(job)
            and not self._supervisor_cleanup_proven(job_id, process=process)
        )

    def _process_group_members_from_proc(self, pgid: int) -> Optional[set[int]]:
        proc = Path("/proc")
        if pgid <= 0 or not proc.is_dir():
            return None
        try:
            entries = list(proc.iterdir())
        except OSError:
            return None
        members: set[int] = set()
        scan_complete = True
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8")
            except (FileNotFoundError, ProcessLookupError):
                # PIDs can disappear between directory enumeration and read.
                # Their absence is evidence of exit, not an unreadable entry.
                continue
            except OSError:
                scan_complete = False
                continue
            _, separator, suffix = stat_text.rpartition(")")
            fields = suffix.strip().split() if separator else []
            if len(fields) <= 2 or fields[0] == "Z":
                continue
            try:
                member_pgid = int(fields[2])
                member_pid = int(entry.name)
            except ValueError:
                continue
            if member_pgid == pgid:
                members.add(member_pid)
        return members if scan_complete else None

    def _process_group_members_from_ps(self, pgid: int) -> Optional[set[int]]:
        if pgid <= 0:
            return set()
        try:
            observed = subprocess.run(
                ["ps", "-eo", "pid=,pgid=,stat="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if observed.returncode != 0:
            return None
        members: set[int] = set()
        for line in observed.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3 or fields[2].startswith("Z"):
                continue
            try:
                member_pid = int(fields[0])
                member_pgid = int(fields[1])
            except ValueError:
                continue
            if member_pgid == pgid:
                members.add(member_pid)
        return members

    def _process_group_liveness(self, pgid: int) -> Optional[bool]:
        """Return group liveness without treating failed discovery as absence."""

        if pgid <= 0:
            return False
        members = self._process_group_members_from_proc(pgid)
        if members is None:
            members = self._process_group_members_from_ps(pgid)
        if members is not None:
            return bool(members)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    def _process_group_has_live_members(self, pgid: int) -> bool:
        return self._process_group_liveness(pgid) is True

    def _process_pid_is_live(self, pid: int) -> bool:
        if pid <= 0:
            return False
        # A zombie still answers kill(pid, 0), but it cannot make progress and
        # must not keep a durable worker in the running state.
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            _, separator, suffix = stat_text.rpartition(")")
            fields = suffix.strip().split() if separator else []
            if fields and fields[0] == "Z":
                return False
        except OSError:
            pass
        try:
            observed = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            state = observed.stdout.strip()
            if observed.returncode == 0 and state.startswith("Z"):
                return False
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _linux_boot_id(self) -> Optional[str]:
        """Return the kernel boot identity used to scope recyclable start ticks."""

        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            return None
        return boot_id or None

    @staticmethod
    def _parse_linux_process_identity(identity: Any) -> Optional[tuple[str, int]]:
        text = str(identity or "")
        if not text.startswith(_LINUX_PROCESS_IDENTITY_PREFIX):
            return None
        payload = text[len(_LINUX_PROCESS_IDENTITY_PREFIX) :]
        boot_id, separator, ticks_text = payload.rpartition(":")
        if not separator or not boot_id:
            return None
        try:
            ticks = int(ticks_text)
        except ValueError:
            return None
        return (boot_id, ticks) if ticks >= 0 else None

    def _linux_proc_start_ticks(self, entry: Path) -> Optional[int]:
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            return None
        _, separator, suffix = stat_text.rpartition(")")
        fields = suffix.strip().split() if separator else []
        if len(fields) <= 19:
            return None
        try:
            return int(fields[19])
        except ValueError:
            return None

    def _linux_login_uid(self, entry: Path) -> Optional[int]:
        try:
            value = (entry / "loginuid").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _unreadable_proc_entry_cannot_match_job_marker(
        self,
        entry: Path,
        job_id: str,
    ) -> bool:
        """Return true only with proof an unreadable process predates this job."""

        job = self.job_manager.get_job(job_id)
        options = getattr(job, "options", None) if job is not None else None
        recorded_login_uid = (
            options.get(_JOB_PROCESS_LOGIN_UID_OPTION)
            if isinstance(options, dict)
            else None
        )
        try:
            recorded_login_uid = int(recorded_login_uid)
        except (TypeError, ValueError):
            recorded_login_uid = None
        entry_login_uid = self._linux_login_uid(entry)
        if (
            recorded_login_uid is not None
            and entry_login_uid is not None
            and entry_login_uid != recorded_login_uid
        ):
            return True
        recorded = self._parse_linux_process_identity(
            getattr(job, "process_identity", None) if job is not None else None
        )
        if recorded is None:
            return False
        boot_id, job_start_ticks = recorded
        if self._linux_boot_id() != boot_id:
            return False
        entry_start_ticks = self._linux_proc_start_ticks(entry)
        return bool(
            entry_start_ticks is not None and entry_start_ticks < job_start_ticks
        )

    def _process_identity(self, pid: Any) -> Optional[str]:
        """Return Linux boot identity plus exact process start tick."""
        if not isinstance(pid, int) or pid <= 0:
            return None
        start_ticks = self._linux_proc_start_ticks(Path(f"/proc/{pid}"))
        boot_id = self._linux_boot_id()
        if start_ticks is not None and boot_id:
            return f"{_LINUX_PROCESS_IDENTITY_PREFIX}{boot_id}:{start_ticks}"
        return None

    @staticmethod
    def _job_process_marker(job_id: str) -> str:
        return hashlib.sha256(f"patchbay-job:{job_id}".encode("utf-8")).hexdigest()

    def _job_marked_process_pids(
        self, job_id: str, *, force_refresh: bool = False
    ) -> Optional[set[int]]:
        """Return live processes carrying this job's inherited marker.

        The marker survives forks, new sessions, and wrapper exit. It gives
        restart cleanup an ownership proof that does not depend on recyclable
        PID or PGID numbers. Linux uses ``/proc``; other Unix hosts use a
        conservative two-pass ``ps`` environment scan.
        """

        if not job_id:
            return set()
        proc = Path("/proc")
        if not proc.is_dir():
            return self._job_marked_process_pids_from_ps(job_id)
        marker = self._job_process_marker(job_id)
        now = time.monotonic()
        with self._process_marker_cache_lock:
            cached = self._process_marker_cache.get(marker)
            if (
                not force_refresh
                and cached
                and now - self._process_marker_cache_at < 0.1
            ):
                return set(cached)
        try:
            entries = list(proc.iterdir())
        except OSError:
            return None
        snapshot: dict[str, set[int]] = {}
        scan_complete = True
        prefix = f"{_JOB_PROCESS_MARKER_ENV}=".encode("utf-8")
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                stat_text = ""
            _, separator, suffix = stat_text.rpartition(")")
            fields = suffix.strip().split() if separator else []
            if fields and fields[0] == "Z":
                # A zombie cannot make progress or retain a useful inherited
                # environment. Reading its environ may fail and must not turn
                # a proven-dead child into unknown live cleanup evidence.
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                if not self._unreadable_proc_entry_cannot_match_job_marker(
                    entry, job_id
                ):
                    scan_complete = False
                continue
            marker_values = [
                item[len(prefix) :].decode("utf-8", errors="ignore")
                for item in environment
                if item.startswith(prefix)
            ]
            if not marker_values:
                continue
            process_pid = int(entry.name)
            for marker_value in marker_values:
                snapshot.setdefault(marker_value, set()).add(process_pid)
        with self._process_marker_cache_lock:
            self._process_marker_cache = snapshot
            self._process_marker_cache_at = now
        marked_pids = set(snapshot.get(marker, set()))
        if marked_pids:
            return marked_pids
        return set() if scan_complete else None

    @staticmethod
    def _parse_portable_process_rows(
        output: str,
    ) -> Optional[dict[int, tuple[str, str]]]:
        rows: dict[int, tuple[str, str]] = {}
        for line in str(output or "").splitlines():
            fields = line.strip().split(None, 2)
            if not fields:
                continue
            if len(fields) < 2:
                return None
            try:
                pid = int(fields[0])
            except ValueError:
                return None
            rows[pid] = (fields[1], fields[2] if len(fields) > 2 else "")
        return rows

    @staticmethod
    def _portable_pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _portable_process_rows(
        self,
        *,
        include_environment: bool,
    ) -> Optional[dict[int, tuple[str, str]]]:
        command = ["ps"]
        if include_environment:
            command.append("eww")
        command.extend(["-axo", "pid=,stat=,command="])
        try:
            observed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if observed.returncode != 0:
            return None
        return self._parse_portable_process_rows(observed.stdout)

    def _job_marked_process_pids_from_ps(self, job_id: str) -> Optional[set[int]]:
        """Find marker env values without treating marker-looking argv as proof."""

        plain_rows = self._portable_process_rows(include_environment=False)
        expanded_rows = self._portable_process_rows(include_environment=True)
        if plain_rows is None or expanded_rows is None:
            return None

        marker_token = f"{_JOB_PROCESS_MARKER_ENV}={self._job_process_marker(job_id)}"
        marked_pids: set[int] = set()
        scan_complete = True
        for pid in set(plain_rows).union(expanded_rows):
            plain = plain_rows.get(pid)
            expanded = expanded_rows.get(pid)
            if plain is None or expanded is None:
                if self._portable_pid_exists(pid):
                    scan_complete = False
                continue
            plain_state, plain_command = plain
            expanded_state, expanded_command = expanded
            if plain_state.startswith("Z") or expanded_state.startswith("Z"):
                continue
            if expanded_command == plain_command:
                environment_suffix = ""
            elif expanded_command.startswith(f"{plain_command} "):
                environment_suffix = expanded_command[len(plain_command) + 1 :]
            else:
                scan_complete = False
                continue
            if marker_token in environment_suffix.split():
                marked_pids.add(pid)
        if marked_pids:
            return marked_pids
        return set() if scan_complete else None

    def _signal_job_marked_processes(self, job_id: str, sig: signal.Signals) -> bool:
        members = self._job_marked_process_pids(job_id, force_refresh=True)
        if not members:
            return False
        signalled = False
        for pid in sorted(members, reverse=True):
            try:
                # Re-read the marker immediately before signalling so a PID
                # recycled during enumeration cannot be targeted.
                current = self._job_marked_process_pids(job_id, force_refresh=True)
                if current is None or pid not in current:
                    continue
                os.kill(pid, sig)
                signalled = True
            except ProcessLookupError:
                continue
            except (PermissionError, OSError):
                logger.warning(
                    "Could not signal marked process for job %s pid=%s",
                    job_id,
                    pid,
                )
        return signalled

    def _stale_running_result(self, job: Any) -> Dict[str, Any]:
        """Return a diagnostic payload for a job PatchBay can no longer track."""
        return redact_sensitive_output(
            {
                "summary": STALE_RUNNING_JOB_ERROR,
                "files_changed": [],
                "parsed_output_schema_valid": False,
                "final_structured_result": False,
                "failure_diagnostic": {
                    "category": STALE_RUNNING_JOB_CATEGORY,
                    "public_message": STALE_RUNNING_JOB_ERROR,
                    "manager_guidance": (
                        "This is PatchBay runtime/process tracking recovery, not proof that the Codex worker "
                        "reasoned badly. Inspect any preserved checkpoints, result artifacts, stdout/stderr byte "
                        "counts, and then restart or continue the worker if needed."
                    ),
                    "operator_action": "Inspect the worker status/report artifacts; restart or continue the worker if no useful evidence was preserved.",
                    "retry_without_operator_action": True,
                },
                "runtime_diagnostics": {
                    "last_event": str(getattr(job, "last_event", "") or ""),
                    "event_count": int(getattr(job, "event_count", 0) or 0),
                    "stdout_bytes_seen": int(getattr(job, "stdout_bytes_seen", 0) or 0),
                    "stderr_bytes_seen": int(getattr(job, "stderr_bytes_seen", 0) or 0),
                    "process_started": bool(getattr(job, "process_started_at", None)),
                    "session_created": bool(getattr(job, "session_id", None)),
                },
            }
        )

    def _stale_running_grace_seconds(self, override: Optional[float] = None) -> float:
        if override is not None:
            return max(0.0, float(override))
        try:
            configured = float(self.config.get("server", {}).get("stale_running_job_grace_seconds", 600))
        except (TypeError, ValueError):
            configured = 600.0
        return max(0.0, configured)

    def _job_timeout_seconds(self) -> Optional[float]:
        configured = self.config.get("server", {}).get("job_timeout_seconds", 1800)
        if configured is None:
            return None
        if isinstance(configured, str):
            normalized = configured.strip().lower()
            if normalized in {"", "0", "none", "never", "unlimited", "disabled", "false"}:
                return None
            try:
                timeout = float(normalized)
            except ValueError:
                return 1800.0
        else:
            try:
                timeout = float(configured)
            except (TypeError, ValueError):
                return 1800.0
        if timeout <= 0:
            return None
        return timeout

    def _codex_startup_gate_enabled(self) -> bool:
        configured = self.config.get("server", {}).get("codex_startup_serialization_enabled", True)
        if isinstance(configured, str):
            return configured.strip().lower() not in {"", "0", "false", "off", "disabled", "no"}
        return bool(configured)

    def _codex_startup_gate_key(self) -> str:
        return str(resolve_codex_home(self.config))

    def _codex_startup_lock_path(self, key: str) -> Path:
        configured = (self.config.get("locks") or {}).get("root") if isinstance(self.config.get("locks"), dict) else None
        lock_dir = resolve_runtime_path(configured, "locks")
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return lock_dir / f"codex_startup_{digest}.lock"

    async def _acquire_codex_startup_file_lock(self, key: str) -> tuple[BinaryIO | None, str]:
        """Acquire a host-wide Codex startup lock for this Codex home."""
        if fcntl is None:
            return None, ""
        path = self._codex_startup_lock_path(key)
        file_handle: BinaryIO | None = path.open("a+b")
        try:
            while True:
                try:
                    fcntl.flock(
                        file_handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
        except BaseException:
            file_handle.close()
            raise
        return file_handle, str(path)

    async def _acquire_codex_startup_gate(self, job_id: str) -> StartupGateLease | None:
        """Serialize the auth-sensitive part of Codex startup without serializing full turns."""
        if not self._codex_startup_gate_enabled():
            return None
        key = self._codex_startup_gate_key()
        lock_key = (id(asyncio.get_running_loop()), key)
        lock = self._codex_startup_locks.setdefault(lock_key, asyncio.Lock())
        self.job_manager.update_job_state(
            job_id,
            JobState.RUNNING,
            last_heartbeat_at=time.time(),
            current_phase="waiting_for_codex_startup_gate",
            progress="Waiting for the Codex auth/session startup gate. This protects shared Codex login state without serializing full worker turns.",
        )
        await lock.acquire()
        lease = StartupGateLease(key=key, lock=lock, acquired_at=time.time())
        try:
            file_handle, file_lock_path = await self._acquire_codex_startup_file_lock(key)
            lease.file_handle = file_handle
            lease.file_lock_path = file_lock_path
            self.job_manager.update_job_state(
                job_id,
                JobState.RUNNING,
                last_heartbeat_at=time.time(),
                current_phase="launching_codex_process",
                progress=(
                    "Codex startup/auth gate acquired for this host; launching Codex process. "
                    "Parallel worker turns resume after Codex creates the session."
                ),
            )
        except BaseException:
            lease.release("startup_file_lock_failed")
            raise
        return lease

    def _session_start_timeout_seconds(self) -> Optional[float]:
        server_config = self.config.get("server", {})
        configured = server_config.get(
            "codex_session_start_timeout_seconds",
            server_config.get("codex_startup_timeout_seconds", 180),
        )
        if configured is None:
            return None
        if isinstance(configured, str):
            normalized = configured.strip().lower()
            if normalized in {"", "0", "none", "never", "unlimited", "disabled", "false"}:
                return None
            try:
                timeout = float(normalized)
            except ValueError:
                return 180.0
        else:
            try:
                timeout = float(configured)
            except (TypeError, ValueError):
                return 180.0
        if timeout <= 0:
            return None
        return timeout

    def _post_completion_exit_grace_seconds(self) -> float:
        """Return wrapper cleanup grace after Codex has semantically completed."""
        configured = self.config.get("server", {}).get("codex_post_completion_exit_grace_seconds", 2)
        try:
            return max(0.0, min(float(configured), 30.0))
        except (TypeError, ValueError):
            return 2.0

    def _post_completion_cleanup_timeout_seconds(self) -> float:
        """Bound transport cleanup after the worker result is already durable."""
        configured = self.config.get("server", {}).get(
            "codex_post_completion_cleanup_timeout_seconds", 3
        )
        try:
            return max(0.1, min(float(configured), 30.0))
        except (TypeError, ValueError):
            return 3.0

    @staticmethod
    def _post_completion_cleanup_call_timeout_seconds(
        cleanup_timeout: float, *, supervisor_contract: bool
    ) -> float:
        cleanup = max(0.1, float(cleanup_timeout))
        supervisor_grace = (
            _SUPERVISOR_CLEANUP_GRACE_FLOOR_SECONDS
            if supervisor_contract and cleanup >= 1.0
            else 0.0
        )
        bounds = [1.0, cleanup * 2.0 + 0.5]
        if supervisor_grace:
            # Supervisor proof and executor-side process-tree discovery have
            # independent bounded scans. Cover both, plus TERM/KILL waits,
            # without turning this cleanup bound into a worker timeout.
            bounds.append(supervisor_grace * 2.0 + cleanup * 4.0 + 5.0)
        return max(bounds)

    def _persist_semantic_completion(
        self,
        job_id: str,
        *,
        session_id: str,
        source: str,
        observed_at: float,
        final_message: str,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        """Persist authoritative Codex completion before wrapper cleanup can stall."""
        result_file = self.job_logs_dir / f"{job_id}_result.json"
        stdout_log = self.job_logs_dir / f"{job_id}_stdout.log"
        stderr_log = self.job_logs_dir / f"{job_id}_stderr.log"
        with self._terminal_cleanup_transition_lock:
            current = self.job_manager.get_job(job_id)
            if current is None:
                return
            if current.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                self.job_manager.transition_job_terminal(
                    job_id,
                    JobState.COMPLETED,
                    terminal_source=source,
                    terminal_observed_at=observed_at,
                )
                return
            result = self._result_from_session_message(final_message, result_file)
            self._write_process_artifact(stdout_log, stdout)
            self._write_process_artifact(stderr_log, stderr)
            if (
                job_id not in self._terminal_cleanup_completed
            ):
                self._ensure_cleanup_repo_block(current)
            self._transition_job_terminal_with_cleanup(
                job_id,
                JobState.COMPLETED,
                result=result,
                session_id=session_id or None,
                terminal_source=source,
                terminal_observed_at=observed_at,
                wrapper_cleanup_outcome="cleanup_pending",
                last_heartbeat_at=time.time(),
                last_event=source,
            )

    def _retain_or_release_terminal_cleanup(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
        cleanup_reaped: bool = False,
        cleanup_complete_outcome: Optional[str] = None,
    ) -> bool:
        """Release ownership only after tracked cleanup is affirmatively empty."""

        with self._terminal_cleanup_transition_lock:
            current = self.job_manager.get_job(job_id)
            if (
                current is not None
                and current.state == JobState.RUNNING
                and current.completion_evidence_source == "stdout_turn_completed"
            ):
                # Parsing or wrapper teardown can fail after Codex has emitted
                # turn.completed. Prefer an exact session report when one is
                # already durable; otherwise promote the bounded completion
                # evidence before any repository lock can be released.
                if not self._recover_completed_session(
                    current, event_loop=event_loop
                ):
                    self._transition_from_completion_evidence(current)
                current = self.job_manager.get_job(job_id)
            terminal = bool(
                current is not None
                and current.state
                in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
            )
            if cleanup_reaped or job_id in self._terminal_cleanup_completed:
                self._complete_terminal_cleanup(
                    job_id,
                    cleanup_complete_outcome or "terminated_after_terminal",
                )
                return False

            cleanup_liveness = self._tracked_process_or_group_liveness(
                job_id, process
            )
            if cleanup_liveness is False:
                self._complete_terminal_cleanup(
                    job_id,
                    cleanup_complete_outcome or "process_not_live_after_terminal",
                )
                return False

            self.processes[job_id] = process
            if terminal and current is not None:
                self._ensure_cleanup_repo_block(current)
                if not self._terminal_cleanup_pending(current):
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        current.state,
                        wrapper_cleanup_outcome="cleanup_pending",
                        last_heartbeat_at=time.time(),
                    )
                self._schedule_terminal_cleanup(
                    job_id,
                    process,
                    event_loop=event_loop,
                )
            return True

    async def _settle_process_tasks_bounded(
        self,
        tasks: list[asyncio.Task],
        *,
        timeout_seconds: float,
    ) -> bool:
        """Drain or cancel process I/O tasks without an unbounded post-result wait."""
        pending = {task for task in tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout_seconds)
        if not pending:
            await asyncio.gather(*tasks, return_exceptions=True)
            return True
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=min(0.5, timeout_seconds))
        return all(task.done() for task in pending)
        
    async def execute_job(self, job_id: str):
        """Execute a Codex job, optionally waiting for an execution slot."""
        current_task = asyncio.current_task()
        if current_task is not None and self.tasks.get(job_id) is not current_task:
            self.tasks[job_id] = current_task
        try:
            if self._execution_semaphore is None:
                await self._execute_job_now(job_id)
                return
            await self._execution_semaphore.acquire()
            try:
                await self._execute_job_now(job_id)
            finally:
                self._execution_semaphore.release()
        finally:
            if current_task is not None and self.tasks.get(job_id) is current_task:
                self.tasks.pop(job_id, None)

    async def _execute_job_now(self, job_id: str):
        """Execute a Codex job asynchronously."""
        job = self.job_manager.get_job(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            self.repo_locks.release_job(job_id)
            return
        if job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            logger.info("Job %s was terminal before execution started: %s", job_id, job.state)
            self.repo_locks.release_job(job_id)
            return
        
        try:
            self.job_manager.update_job_state(
                job_id,
                JobState.RUNNING,
                launch_started_at=time.time(),
                last_heartbeat_at=time.time(),
            )

            initial_session_id = (
                str((job.options or {}).get("resume_session_id") or job.session_id or "").strip()
                if job.mode == "resume"
                else ""
            )
            initial_session_observation_offset = self._prepare_session_observation(job_id, job)

            # Build command and keep prompt text off argv when the Codex CLI supports stdin.
            cmd = self._build_codex_command(job.mode, job.prompt, job.worktree_path, job.options)
            stdin_data = self._stdin_for_command(job.prompt, cmd)
            if self._job_launch_blocked(job_id):
                logger.info(f"Job {job_id} was cancelled before process launch")
                self._complete_terminal_cleanup(
                    job_id, "cancelled_before_startup_gate"
                )
                return
            
            logger.info(
                "Executing job %s: mode=%s sandbox=%s stdin_prompt=%s structured_output=%s",
                job_id,
                job.mode,
                (job.options or {}).get("sandbox"),
                stdin_data is not None,
                (job.options or {}).get("structured_output", True),
            )
            
            # Log files
            stdout_log = self.job_logs_dir / f"{job_id}_stdout.log"
            stderr_log = self.job_logs_dir / f"{job_id}_stderr.log"
            result_file = self.job_logs_dir / f"{job_id}_result.json"
            
            timeout = self._job_timeout_seconds()
            startup_gate = await self._acquire_codex_startup_gate(job_id)

            if self._job_launch_blocked(job_id):
                if startup_gate is not None:
                    startup_gate.release("cancelled_before_process_launch")
                self._complete_terminal_cleanup(
                    job_id, "cancelled_before_process_launch"
                )
                return

            # Install the supervisor proof contract only after the cancellable
            # startup-gate wait. Before this point no supervisor can exist, so a
            # cancelled queued worker can release its repository lock directly.
            self._persist_process_marker_contract(job_id)
            job = self.job_manager.get_job(job_id) or job
            
            launch_gate_read: int | None = None
            launch_gate_write: int | None = None
            supervisor_ready_read: int | None = None
            supervisor_ready_write: int | None = None
            repo_lock_fd: int | None = None
            launch_cancelled = False
            spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
            ready_task: asyncio.Task[bytes] | None = None
            try:
                spawn_cmd = list(cmd)
                spawn_options: dict[str, Any] = {}
                if os.name == "posix":
                    launch_gate_read, launch_gate_write = os.pipe()
                    os.set_inheritable(launch_gate_read, True)
                    supervisor_ready_read, supervisor_ready_write = os.pipe()
                    os.set_inheritable(supervisor_ready_write, True)
                    repo_lock_fd = self.repo_locks.duplicate_job_lock_fd(job_id)
                    supervisor = Path(__file__).with_name("process_supervisor.py")
                    spawn_cmd = [
                        sys.executable,
                        str(supervisor),
                        "--gate-fd",
                        str(launch_gate_read),
                        "--ready-fd",
                        str(supervisor_ready_write),
                        "--cleanup-proof-path",
                        str(
                            (job.options or {}).get(
                                _JOB_PROCESS_SUPERVISOR_PROOF_OPTION, ""
                            )
                        ),
                    ]
                    inherited_fds = [launch_gate_read, supervisor_ready_write]
                    if repo_lock_fd is not None:
                        spawn_cmd.extend(["--repo-lock-fd", str(repo_lock_fd)])
                        inherited_fds.append(repo_lock_fd)
                    spawn_cmd.extend(["--", *cmd])
                    spawn_options["pass_fds"] = tuple(inherited_fds)

                # Persist the uncertain OS-spawn boundary before yielding to
                # create_subprocess_exec. Restart recovery then knows whether
                # missing supervisor proof is possible or not.
                self._mark_process_supervisor_launching(job_id)
                spawn_task = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *spawn_cmd,
                        cwd=job.worktree_path,
                        stdin=(
                            asyncio.subprocess.PIPE
                            if stdin_data is not None
                            else asyncio.subprocess.DEVNULL
                        ),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=self._build_env(job_id=job_id),
                        start_new_session=True,
                        **spawn_options,
                    ),
                    name=f"patchbay-process-spawn-{job_id}",
                )
                while not spawn_task.done():
                    try:
                        await asyncio.shield(spawn_task)
                    except asyncio.CancelledError:
                        # The OS process may already exist even though asyncio
                        # has not returned its handle.  Keep the gated spawn
                        # alive until PatchBay can register and reap it.
                        launch_cancelled = True
                        current_task = asyncio.current_task()
                        if current_task is not None and hasattr(
                            current_task, "uncancel"
                        ):
                            current_task.uncancel()
                process = spawn_task.result()
                self._mark_process_supervisor_spawned(job_id)
                if supervisor_ready_write is not None:
                    os.close(supervisor_ready_write)
                    supervisor_ready_write = None
                if supervisor_ready_read is not None:
                    ready_task = asyncio.create_task(
                        asyncio.to_thread(os.read, supervisor_ready_read, 1),
                        name=f"patchbay-supervisor-ready-{job_id}",
                    )
                    while not ready_task.done():
                        try:
                            await asyncio.shield(ready_task)
                        except asyncio.CancelledError:
                            # Cancellation is recorded, but the supervisor is
                            # not signalled until it confirms that its TERM/INT
                            # handlers and cleanup-proof path are installed.
                            launch_cancelled = True
                            current_task = asyncio.current_task()
                            if current_task is not None and hasattr(
                                current_task, "uncancel"
                            ):
                                current_task.uncancel()
                    if ready_task.result() != b"1":
                        raise RuntimeError(
                            "Codex process supervisor exited before publishing readiness"
                        )
                    self._publish_supervisor_gated_state(
                        job_id, int(getattr(process, "pid", 0) or 0)
                    )
                    os.close(supervisor_ready_read)
                    supervisor_ready_read = None
                if launch_gate_read is not None:
                    os.close(launch_gate_read)
                    launch_gate_read = None
                if repo_lock_fd is not None:
                    os.close(repo_lock_fd)
                    repo_lock_fd = None

                self.processes[job_id] = process
                process_pid = getattr(process, "pid", None)
                current = self.job_manager.get_job(job_id)
                state_for_metadata = (
                    current.state
                    if current is not None
                    and current.state
                    in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                    else JobState.RUNNING
                )
                self.job_manager.update_job_state(
                    job_id,
                    state_for_metadata,
                    process_started_at=time.time(),
                    process_pid=process_pid,
                    process_pgid=process_pid,
                    process_identity=self._process_identity(process_pid),
                    last_heartbeat_at=time.time(),
                    current_phase="codex_process_registered_before_launch",
                    progress=(
                        "Codex process supervisor registered; preparing the worker turn."
                    ),
                )
                self._capture_job_descendants(
                    job_id, int(process_pid or 0), force_refresh=True
                )

                with self._terminal_cleanup_transition_lock:
                    cancelled_during_launch = (
                        launch_cancelled or self._job_launch_blocked(job_id)
                    )
                    if not cancelled_during_launch and launch_gate_write is not None:
                        os.write(launch_gate_write, b"1")
                        os.close(launch_gate_write)
                        launch_gate_write = None
                if cancelled_during_launch:
                    if launch_gate_write is not None:
                        os.close(launch_gate_write)
                        launch_gate_write = None
                    startup_gate.release("cancelled_during_process_launch")
                    current = self.job_manager.get_job(job_id)
                    if current is not None and current.state not in {
                        JobState.COMPLETED,
                        JobState.FAILED,
                        JobState.CANCELLED,
                    }:
                        self._transition_job_terminal_with_cleanup(
                            job_id,
                            JobState.CANCELLED,
                            result=self._minimal_cancelled_result(
                                "Execution cancelled during process launch"
                            ),
                            error="Execution cancelled during process launch",
                            terminal_source="manager_cancellation",
                            terminal_observed_at=time.time(),
                            wrapper_cleanup_outcome="cleanup_pending",
                        )
                    cleanup_task = asyncio.create_task(
                        self._terminate_process(job_id, process),
                        name=f"patchbay-launch-cancel-cleanup-{job_id}",
                    )
                    while not cleanup_task.done():
                        try:
                            await asyncio.shield(cleanup_task)
                        except asyncio.CancelledError:
                            # Once the OS process exists, cancellation cannot
                            # transfer cleanup ownership away from this task.
                            current_task = asyncio.current_task()
                            if current_task is not None and hasattr(
                                current_task, "uncancel"
                            ):
                                current_task.uncancel()
                    cleanup_task.result()
                    current = self.job_manager.get_job(job_id)
                    if current is not None:
                        self._finish_cancelled_cleanup(
                            current,
                            process,
                            recorded_cleanup_outcome="",
                            recorded_cleanup_reaped=False,
                        )
                    return

                self.job_manager.update_job_state(
                    job_id,
                    JobState.RUNNING,
                    current_phase="codex_process_started_waiting_for_session",
                    progress="Codex process started; waiting for session creation.",
                )
                logger.info(
                    "Job %s Codex process supervisor started: pid=%s",
                    job_id,
                    process_pid,
                )
            except Exception:
                if startup_gate is not None:
                    startup_gate.release("process_launch_failed")
                raise
            finally:
                for descriptor in (
                    launch_gate_read,
                    launch_gate_write,
                    supervisor_ready_read,
                    supervisor_ready_write,
                    repo_lock_fd,
                ):
                    if descriptor is None:
                        continue
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            
            cleanup_reaped = False
            cleanup_complete_outcome: Optional[str] = None
            try:
                if not bool((job.options or {}).get("json_events", True)) and startup_gate is not None:
                    startup_gate.release("json_events_disabled")
                capture = await self._communicate_with_progress(
                    job_id,
                    process,
                    stdin_data=stdin_data,
                    total_timeout=timeout,
                    session_start_timeout=self._session_start_timeout_seconds(),
                    expect_session=bool((job.options or {}).get("json_events", True)),
                    initial_session_id=initial_session_id,
                    initial_session_observation_offset=initial_session_observation_offset,
                    startup_gate=startup_gate,
                )
                cleanup_reaped = capture.cleanup_reaped
                if capture.semantic_terminal_seen and capture.wrapper_cleanup_outcome:
                    cleanup_complete_outcome = "terminated_after_terminal"
                else:
                    cleanup_complete_outcome = "process_exited"
                if startup_gate is not None:
                    startup_gate.release("process_completed_before_session_gate_release")
                stdout = capture.stdout
                stderr = capture.stderr
                if capture.semantic_terminal_seen and capture.session_final_message:
                    stdout = self._append_session_terminal_result(stdout, capture.session_final_message)

                if self._job_is_cancelled(job_id):
                    self._write_process_artifact(stdout_log, stdout)
                    self._write_process_artifact(stderr_log, stderr)
                    raw_stdout = stdout.decode('utf-8', errors='replace')
                    session_id = capture.session_id or self._extract_session_id_from_json_events(raw_stdout)
                    if not session_id:
                        session_id = self._extract_session_id(stderr.decode('utf-8', errors='replace'))
                    cancelled_job = self.job_manager.get_job(job_id)
                    cancel_reason = (cancelled_job.error if cancelled_job else None) or "Cancelled by request"
                    partial_result = await self._parse_partial_result(
                        stdout,
                        result_file,
                        job.options,
                        reason=str(cancel_reason),
                    )
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.CANCELLED,
                        result=partial_result,
                        session_id=session_id,
                        exit_code=process.returncode,
                        error=str(cancel_reason),
                        last_heartbeat_at=time.time(),
                        last_event="process.cancelled",
                        progress=self._cancelled_progress_label(partial_result),
                        wrapper_cleanup_outcome="cleanup_pending",
                    )
                    logger.info(f"Job {job_id} process exited after cancellation")
                    return

                process_exit_job = self.job_manager.get_job(job_id)
                process_exit_state = (
                    process_exit_job.state
                    if process_exit_job is not None
                    and process_exit_job.state
                    in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                    else JobState.RUNNING
                )
                self.job_manager.update_job_state(
                    job_id,
                    process_exit_state,
                    exit_code=process.returncode,
                    last_heartbeat_at=time.time(),
                    last_event="process.exited",
                    progress="Codex process exited; PatchBay is parsing the result and writing artifacts.",
                )

                self._write_process_artifact(stdout_log, stdout)
                self._write_process_artifact(stderr_log, stderr)

                if capture.session_start_timed_out:
                    startup_result = await self._parse_partial_result(
                        stdout,
                        result_file,
                        job.options,
                        reason="Codex session startup timed out before PatchBay saw a JSON session.",
                        status="failed",
                    )
                    cleanup_complete_outcome = "process_not_live_after_terminal"
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.FAILED,
                        result=startup_result,
                        error=(
                            "Codex process started but did not create a JSON session before the startup "
                            "timeout. Inspect local job stdout/stderr logs for startup diagnostics."
                        ),
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                        wrapper_cleanup_outcome="cleanup_pending",
                    )
                    logger.error("Job %s failed: Codex session startup timeout", job_id)
                    return

                if capture.total_timed_out:
                    timeout_result = await self._parse_partial_result(
                        stdout,
                        result_file,
                        job.options,
                        reason=f"Job timed out after {timeout} seconds.",
                        status="failed",
                    )
                    cleanup_complete_outcome = "process_not_live_after_terminal"
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.FAILED,
                        result=timeout_result,
                        error=f"Job timed out after {timeout} seconds",
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                        wrapper_cleanup_outcome="cleanup_pending",
                    )
                    logger.error(f"Job {job_id} timed out")
                    return
                
                raw_stdout = stdout.decode('utf-8', errors='replace')
                
                completed_job = self.job_manager.get_job(job_id)
                if (
                    capture.semantic_terminal_seen
                    and completed_job is not None
                    and completed_job.state == JobState.COMPLETED
                    and isinstance(completed_job.result, dict)
                ):
                    result = dict(completed_job.result)
                else:
                    result = await self._parse_result(stdout, result_file, job.options)
                
                # Extract session ID from JSON events (stdout) first, then fall back to stderr
                session_id = capture.session_id or self._extract_session_id_from_json_events(raw_stdout)
                if not session_id:
                    session_id = self._extract_session_id(
                        stderr.decode("utf-8", errors="replace")
                    )
                
                result = redact_sensitive_output(result)
                
                if (
                    process.returncode == 0
                    or capture.semantic_terminal_seen
                    or capture.stdout_turn_completed_seen
                ):
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.COMPLETED,
                        result=result,
                        session_id=session_id,
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                        terminal_source=(
                            capture.terminal_source
                            or (
                                "stdout_turn_completed"
                                if capture.stdout_turn_completed_seen
                                else "process_exit"
                            )
                        ),
                        terminal_observed_at=(
                            capture.terminal_observed_at
                            or capture.stdout_turn_completed_at
                            or time.time()
                        ),
                        wrapper_cleanup_outcome="cleanup_pending",
                    )
                    logger.info(f"Job {job_id} completed successfully")
                else:
                    failure = self._classify_codex_failure(stdout, stderr, process.returncode)
                    if failure:
                        result = self._attach_failure_diagnostic(result, failure)
                        self._write_result_file(result_file, result)
                    error_message = (
                        failure.get("public_message")
                        if failure
                        else f"Codex process failed with exit code {process.returncode}. Inspect local job logs for details."
                    )
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.FAILED,
                        result=result,
                        error=error_message,
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                        wrapper_cleanup_outcome="cleanup_pending",
                    )
                    logger.error("Job %s failed: exit code %s%s", job_id, process.returncode, f" ({failure['category']})" if failure else "")
                    
            finally:
                if startup_gate is not None:
                    startup_gate.release("job_finally")
                self._retain_or_release_terminal_cleanup(
                    job_id,
                    process,
                    cleanup_reaped=cleanup_reaped,
                    cleanup_complete_outcome=cleanup_complete_outcome,
                )
                
        except asyncio.CancelledError:
            process = self.processes.get(job_id)
            if process is None:
                current = self.job_manager.get_job(job_id)
                if current is not None and current.state in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }:
                    self._complete_terminal_cleanup(
                        job_id,
                        "cancelled_before_process_launch",
                    )
                else:
                    self.repo_locks.release_job(job_id)
                return
            raise
        except Exception as e:
            process = self.processes.get(job_id)
            if self._job_is_cancelled(job_id):
                logger.info(f"Job {job_id} stopped after cancellation")
                if process is None:
                    current = self.job_manager.get_job(job_id)
                    if current is None:
                        self.repo_locks.release_job(job_id)
                    else:
                        self._reconcile_terminal_cleanup(current)
                else:
                    self._retain_or_release_terminal_cleanup(job_id, process)
                return
            current = self.job_manager.get_job(job_id)
            if current is not None and current.state == JobState.COMPLETED:
                logger.warning(
                    "Job %s raised after durable semantic completion; preserving "
                    "the completed result and reconciling wrapper cleanup: %s",
                    job_id,
                    internal_log_error(e),
                )
                if process is None:
                    self._reconcile_terminal_cleanup(current)
                else:
                    self._retain_or_release_terminal_cleanup(job_id, process)
                return
            logger.error("Job %s execution failed: %s", job_id, internal_log_error(e))
            current = self.job_manager.get_job(job_id)
            with self._terminal_cleanup_transition_lock:
                if (
                    current is not None
                    and job_id not in self._terminal_cleanup_completed
                ):
                    self._ensure_cleanup_repo_block(current)
                self._transition_job_terminal_with_cleanup(
                    job_id,
                    JobState.FAILED,
                    error=public_error_message(e, default="Job execution failed."),
                    last_heartbeat_at=time.time(),
                    wrapper_cleanup_outcome="cleanup_pending",
                )
            if process is None:
                current = self.job_manager.get_job(job_id)
                if current is None:
                    self.repo_locks.release_job(job_id)
                else:
                    self._reconcile_terminal_cleanup(current)
            else:
                self._retain_or_release_terminal_cleanup(job_id, process)
    
    def _build_codex_command(self, mode: str, prompt: str, cwd: str, options: Dict[str, Any] = None) -> list[str]:
        """
        Build the codex exec command.
        
        Args:
            mode: "plan" or "apply" (informational only)
            prompt: User prompt
            cwd: Working directory
            options: All options
            
        Returns:
            Command as list of strings
        """
        if options is None:
            options = {}
        if mode == "resume":
            return self._build_codex_resume_command(prompt, options)
        
        security = self.config.get('security', {})
        if mode == "plan":
            sandbox = options.get('sandbox') or security.get('default_sandbox', 'read-only')
        elif mode == "apply":
            sandbox = options.get('sandbox') or "workspace-write"
        else:
            sandbox = options.get('sandbox') or security.get('default_sandbox', 'read-only')

        cmd = ['codex', 'exec']
        
        if options.get('dangerously_bypass'):
            if not security.get('allow_dangerously_bypass', False):
                raise PermissionError("dangerously_bypass is disabled by config.yaml")
            cmd.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            # Only add sandbox if not bypassing
            cmd.extend(['--sandbox', sandbox])
            
            # Current Codex CLI versions no longer expose the historical
            # --full-auto flag. Keep accepting the option for older clients,
            # but do not emit a stale CLI argument.

        if options.get("ignore_user_config"):
            cmd.append("--ignore-user-config")

        exec_cwd = options.get("_codex_cwd")
        if exec_cwd:
            cmd.extend(['--cd', str(exec_cwd)])
        
        # Structured output
        if options.get('structured_output', True):
            cmd.extend(['--output-schema', str(self.schema_path)])
        
        # JSON events
        if options.get('json_events', True):
            cmd.append('--json')
        
        # Model
        if 'model' in options and options['model']:
            cmd.extend(['--model', options['model']])
        
        # Images
        if 'images' in options and options['images']:
            for image in options['images']:
                cmd.extend(['--image', image])
        
        # Feature flags. Do not inject defaults here: feature names change
        # across Codex CLI releases, and unknown flags are hard failures.
        features = options.get('features', {})
        disable = set(features.get('disable', []))
        enable = set(features.get('enable', []))

        # Apply feature flags
        for f in enable:
            cmd.extend(['--enable', f])
        for f in disable:
            cmd.extend(['--disable', f])
        
        # Config profile
        if 'profile' in options and options['profile']:
            cmd.extend(['--profile', options['profile']])
        
        # Additional directories
        if 'add_dirs' in options and options['add_dirs']:
            for add_dir in options['add_dirs']:
                allowed = self.config.get('repositories', {}).get('allowed') or []
                validated = validate_allowed_path(add_dir, allowed)
                cmd.extend(['--add-dir', str(validated)])
        
        # Config overrides via -c flag
        if 'config_overrides' in options:
            for override in options['config_overrides']:
                cmd.extend(['-c', override])

        if prompt:
            cmd.append("-")

        return cmd

    def _build_codex_resume_command(self, prompt: str, options: Dict[str, Any]) -> list[str]:
        """Build `codex exec resume` with options before SESSION_ID/PROMPT."""
        security = self.config.get('security', {})
        session_id = str(options.get("resume_session_id") or "").strip()
        if not session_id:
            raise ValueError("resume_session_id is required for resume jobs")

        cmd = ['codex', 'exec']
        sandbox = options.get('sandbox') or security.get('default_sandbox', 'read-only')

        if options.get('dangerously_bypass'):
            if not security.get('allow_dangerously_bypass', False):
                raise PermissionError("dangerously_bypass is disabled by config.yaml")
            cmd.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            cmd.extend(['--sandbox', sandbox])

        exec_cwd = options.get("_codex_cwd")
        if exec_cwd:
            cmd.extend(['--cd', str(exec_cwd)])

        if options.get('full_auto', False):
            # Current Codex CLI versions no longer expose the historical
            # --full-auto flag. Keep accepting the option for compatibility,
            # but do not emit a stale CLI argument.
            pass

        if options.get("ignore_user_config"):
            cmd.append("--ignore-user-config")

        if options.get('structured_output', True):
            cmd.extend(['--output-schema', str(self.schema_path)])

        if options.get('json_events', True):
            cmd.append('--json')

        if 'model' in options and options['model']:
            cmd.extend(['--model', options['model']])

        if 'images' in options and options['images']:
            for image in options['images']:
                cmd.extend(['--image', image])

        features = options.get('features', {})
        disable = set(features.get('disable', []))
        enable = set(features.get('enable', []))
        for feature in enable:
            cmd.extend(['--enable', feature])
        for feature in disable:
            cmd.extend(['--disable', feature])

        if 'config_overrides' in options:
            for override in options['config_overrides']:
                cmd.extend(['-c', override])

        cmd.append('resume')
        cmd.append(session_id)
        if prompt:
            cmd.append("-")

        return cmd

    def _stdin_for_command(self, prompt: str, cmd: list[str]) -> bytes | None:
        """Return prompt stdin when command uses Codex's '-' prompt sentinel."""
        if prompt and cmd and cmd[-1] == "-":
            return prompt.encode("utf-8")
        return None

    async def _communicate_with_progress(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        *,
        stdin_data: bytes | None,
        total_timeout: Optional[float],
        session_start_timeout: Optional[float],
        expect_session: bool,
        initial_session_id: str = "",
        initial_session_observation_offset: int = 0,
        startup_gate: StartupGateLease | None = None,
    ) -> ProcessCapture:
        """Read Codex JSON events incrementally so session/heartbeat state is live."""
        capture_limit = self._process_capture_max_bytes()
        event_line_limit = self._process_event_line_max_bytes()
        stdout_capture = _BoundedByteCapture(capture_limit)
        stderr_capture = _BoundedByteCapture(capture_limit)
        state: dict[str, Any] = {
            "session_id": str(initial_session_id or "").strip() or None,
            "session_start_timed_out": False,
            "total_timed_out": False,
            "semantic_terminal_seen": False,
            "stdout_turn_completed_seen": False,
            "terminal_source": "",
            "terminal_observed_at": None,
            "session_final_message": "",
            "wrapper_cleanup_outcome": "",
            "cleanup_reaped": False,
        }
        process_started_at = time.time()
        session_observer: CodexSessionTerminalObserver | None = None
        if state["session_id"] and startup_gate is not None:
            startup_gate.release("resume_session_known")

        async def feed_stdin() -> None:
            if stdin_data is None or process.stdin is None:
                return
            try:
                process.stdin.write(stdin_data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    process.stdin.close()
                    await process.stdin.wait_closed()
                except Exception:
                    pass

        def record_oversized_line(stream_name: str, byte_count: int) -> None:
            now = time.time()
            job = self.job_manager.get_job(job_id)
            if stream_name == "stdout":
                self.job_manager.update_job_state(
                    job_id,
                    JobState.RUNNING,
                    last_heartbeat_at=now,
                    last_event="stdout.oversized_line",
                    progress=(
                        "Codex emitted an oversized stdout event; PatchBay "
                        "drained it without blocking and retained bounded evidence."
                    ),
                    event_count=(int(job.event_count or 0) + 1) if job else 1,
                    stdout_bytes_seen=(
                        int(job.stdout_bytes_seen or 0) + byte_count
                        if job
                        else byte_count
                    ),
                    last_stdout_at=now,
                )
            else:
                self.job_manager.update_job_state(
                    job_id,
                    JobState.RUNNING,
                    last_heartbeat_at=now,
                    last_event="stderr.oversized_line",
                    progress=(
                        "Codex emitted an oversized stderr line; PatchBay "
                        "drained it without blocking and retained bounded evidence."
                    ),
                    event_count=(int(job.event_count or 0) + 1) if job else 1,
                    stderr_bytes_seen=(
                        int(job.stderr_bytes_seen or 0) + byte_count
                        if job
                        else byte_count
                    ),
                    last_stderr_at=now,
                )

        def process_line(stream_name: str, line: bytes) -> None:
            if stream_name == "stdout":
                self._capture_job_descendants(
                    job_id,
                    int(getattr(process, "pid", 0) or 0),
                    force_refresh=True,
                )
                session_started = self._observe_stdout_event(job_id, line, state)
                if session_started and startup_gate is not None:
                    startup_gate.release("session_created")
                return
            now = time.time()
            job = self.job_manager.get_job(job_id)
            self.job_manager.update_job_state(
                job_id,
                JobState.RUNNING,
                last_heartbeat_at=now,
                last_event="stderr",
                progress=(
                    "Codex emitted stderr output; inspect local stderr log if "
                    "the turn fails."
                ),
                event_count=(int(job.event_count or 0) + 1) if job else 1,
                stderr_bytes_seen=(
                    int(job.stderr_bytes_seen or 0) + len(line)
                    if job
                    else len(line)
                ),
                last_stderr_at=now,
            )

        async def read_stream(stream: asyncio.StreamReader | None, *, stream_name: str) -> None:
            if stream is None:
                return
            capture = stdout_capture if stream_name == "stdout" else stderr_capture
            line_buffer = bytearray()
            oversized_bytes = 0
            discarding_oversized_line = False
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    if discarding_oversized_line:
                        record_oversized_line(stream_name, oversized_bytes)
                    elif line_buffer:
                        process_line(stream_name, bytes(line_buffer))
                    return
                capture.append(chunk)
                pending = chunk
                while pending:
                    if discarding_oversized_line:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            oversized_bytes += len(pending)
                            pending = b""
                            continue
                        oversized_bytes += newline + 1
                        record_oversized_line(stream_name, oversized_bytes)
                        oversized_bytes = 0
                        discarding_oversized_line = False
                        pending = pending[newline + 1 :]
                        continue

                    newline = pending.find(b"\n")
                    if newline >= 0:
                        line_buffer.extend(pending[: newline + 1])
                        process_line(stream_name, bytes(line_buffer))
                        line_buffer.clear()
                        pending = pending[newline + 1 :]
                        continue
                    line_buffer.extend(pending)
                    pending = b""
                    if len(line_buffer) > event_line_limit:
                        oversized_bytes = len(line_buffer)
                        line_buffer.clear()
                        discarding_oversized_line = True

        stdin_task = asyncio.create_task(feed_stdin())
        stdout_task = asyncio.create_task(read_stream(process.stdout, stream_name="stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, stream_name="stderr"))
        wait_task = asyncio.create_task(process.wait())
        tasks = [stdin_task, stdout_task, stderr_task, wait_task]

        try:
            while not wait_task.done():
                await asyncio.sleep(0.5)
                for reader_task, stream_name in (
                    (stdout_task, "stdout"),
                    (stderr_task, "stderr"),
                ):
                    if not reader_task.done() or reader_task.cancelled():
                        continue
                    reader_error = reader_task.exception()
                    if reader_error is not None:
                        await self._terminate_process(job_id, process)
                        raise RuntimeError(
                            f"Codex {stream_name} reader failed while draining output"
                        ) from reader_error
                now = time.time()
                self._capture_job_descendants(
                    job_id,
                    int(getattr(process, "pid", 0) or 0),
                    force_refresh=True,
                )
                session_id = str(state.get("session_id") or "")
                if session_id and session_observer is None:
                    session_observer = self._session_terminal_observer(
                        session_id,
                        not_before=process_started_at,
                        initial_offset=(
                            initial_session_observation_offset
                            if session_id == str(initial_session_id or "").strip()
                            else 0
                        ),
                    )
                current_job = self.job_manager.get_job(job_id)
                durable_completion = bool(
                    current_job is not None
                    and current_job.state == JobState.COMPLETED
                    and isinstance(current_job.result, dict)
                )
                if session_observer is not None and not durable_completion:
                    terminal = session_observer.poll()
                    if terminal.completed:
                        state["semantic_terminal_seen"] = True
                        state["terminal_source"] = terminal.source
                        state["terminal_observed_at"] = terminal.observed_at or now
                        state["session_final_message"] = terminal.final_message
                        self._persist_semantic_completion(
                            job_id,
                            session_id=session_id,
                            source=terminal.source,
                            observed_at=float(state["terminal_observed_at"]),
                            final_message=terminal.final_message,
                            stdout=stdout_capture.value(),
                            stderr=stderr_capture.value(),
                        )
                if (
                    state.get("stdout_turn_completed_seen")
                    and not state.get("semantic_terminal_seen")
                    and now
                    - float(state.get("stdout_turn_completed_at") or now)
                    >= self._post_completion_exit_grace_seconds()
                ):
                    # The exact session observer was polled immediately above.
                    # If it still has no task_complete record, promote the
                    # already-durable stdout evidence instead of waiting for a
                    # quiet wrapper forever.
                    evidence_job = self.job_manager.get_job(job_id)
                    if evidence_job is not None and self._recover_completion_evidence(
                        evidence_job,
                        event_loop=asyncio.get_running_loop(),
                    ):
                        state["semantic_terminal_seen"] = True
                        state["terminal_source"] = "stdout_turn_completed"
                        state["terminal_observed_at"] = float(
                            getattr(
                                evidence_job,
                                "completion_evidence_observed_at",
                                None,
                            )
                            or now
                        )
                current_job = self.job_manager.get_job(job_id)
                durable_completion = bool(
                    state.get("semantic_terminal_seen")
                    and current_job is not None
                    and current_job.state == JobState.COMPLETED
                    and isinstance(current_job.result, dict)
                )
                if durable_completion:
                    observed_at = float(state.get("terminal_observed_at") or now)
                    if now - observed_at >= self._post_completion_exit_grace_seconds():
                        cleanup_timeout = self._post_completion_cleanup_timeout_seconds()
                        cleanup_job = self.job_manager.get_job(job_id)
                        cleanup_call_timeout = (
                            self._post_completion_cleanup_call_timeout_seconds(
                                cleanup_timeout,
                                supervisor_contract=self._supervisor_cleanup_contract_installed(
                                    cleanup_job
                                ),
                            )
                        )
                        try:
                            await asyncio.wait_for(
                                self._terminate_process(
                                    job_id,
                                    process,
                                    graceful_timeout=max(0.1, cleanup_timeout / 2),
                                    kill_timeout=max(0.1, cleanup_timeout / 2),
                                ),
                                timeout=cleanup_call_timeout,
                            )
                            cleanup_job = self.job_manager.get_job(job_id)
                            cleanup_pgid = int(
                                getattr(cleanup_job, "process_pgid", None)
                                or getattr(process, "pid", 0)
                                or 0
                            )
                            state["cleanup_reaped"] = (
                                await self._wait_for_process_group_exit(
                                    job_id,
                                    process,
                                    cleanup_pgid,
                                    timeout=max(0.5, cleanup_timeout),
                                )
                            )
                            state["wrapper_cleanup_outcome"] = (
                                "terminated_after_terminal"
                                if state["cleanup_reaped"]
                                else "cleanup_retry_pending_process_live"
                            )
                        except asyncio.TimeoutError:
                            state["wrapper_cleanup_outcome"] = "cleanup_timeout_after_terminal"
                        break
                if (
                    expect_session
                    and session_start_timeout is not None
                    and not state.get("session_id")
                    and now - process_started_at >= session_start_timeout
                ):
                    state["session_start_timed_out"] = True
                    await self._terminate_process(job_id, process)
                    break
                if total_timeout is not None and now - process_started_at >= total_timeout:
                    state["total_timed_out"] = True
                    await self._terminate_process(job_id, process)
                    break
            if state.get("semantic_terminal_seen"):
                settled = await self._settle_process_tasks_bounded(
                    tasks,
                    timeout_seconds=self._post_completion_cleanup_timeout_seconds(),
                )
                if not settled:
                    state["wrapper_cleanup_outcome"] = "cleanup_timeout_after_terminal"
            else:
                await wait_task
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                for reader_task, stream_name in (
                    (stdout_task, "stdout"),
                    (stderr_task, "stderr"),
                ):
                    if reader_task.cancelled():
                        raise RuntimeError(
                            f"Codex {stream_name} reader was cancelled before output drained"
                        )
                    reader_error = reader_task.exception()
                    if reader_error is not None:
                        raise RuntimeError(
                            f"Codex {stream_name} reader failed after process exit"
                        ) from reader_error
                await asyncio.gather(stdin_task, return_exceptions=True)
        finally:
            pending = {task for task in tasks if not task.done()}
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=0.1)

        return ProcessCapture(
            stdout=stdout_capture.value(),
            stderr=stderr_capture.value(),
            session_id=state.get("session_id"),
            session_start_timed_out=bool(state.get("session_start_timed_out")),
            total_timed_out=bool(state.get("total_timed_out")),
            semantic_terminal_seen=bool(state.get("semantic_terminal_seen")),
            stdout_turn_completed_seen=bool(
                state.get("stdout_turn_completed_seen")
            ),
            stdout_turn_completed_at=state.get("stdout_turn_completed_at"),
            terminal_source=str(state.get("terminal_source") or ""),
            terminal_observed_at=state.get("terminal_observed_at"),
            session_final_message=str(state.get("session_final_message") or ""),
            wrapper_cleanup_outcome=str(state.get("wrapper_cleanup_outcome") or ""),
            cleanup_reaped=bool(state.get("cleanup_reaped")),
        )

    def _schedule_terminal_cleanup(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        *,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if job_id in self._terminal_cleanup_completed:
            return
        if event_loop is not None:
            event_loop.call_soon_threadsafe(
                self._schedule_terminal_cleanup,
                job_id,
                process,
            )
            return
        existing = self.cleanup_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._terminal_cleanup_loop(job_id, process),
            name=f"patchbay-terminal-cleanup-{job_id}",
        )
        self.cleanup_tasks[job_id] = task
        task.add_done_callback(
            lambda done, cleanup_job_id=job_id: self._terminal_cleanup_task_done(
                cleanup_job_id, done
            )
        )

    def _terminal_cleanup_task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self.cleanup_tasks.get(job_id) is task:
            self.cleanup_tasks.pop(job_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Terminal cleanup task for job %s was cancelled", job_id)
        except Exception as error:
            logger.error(
                "Terminal cleanup task for job %s failed: %s",
                job_id,
                internal_log_error(error),
            )

    async def _terminal_cleanup_loop(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Retain process and repo-lock ownership until wrapper death is proven."""

        if self._supervisor_cleanup_uncertain(job_id, process=process):
            current = self.job_manager.get_job(job_id)
            if current is not None and current.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                self._transition_job_terminal_with_cleanup(
                    job_id,
                    current.state,
                    wrapper_cleanup_outcome=(
                        "cleanup_blocked_untrusted_process_identity"
                    ),
                    last_heartbeat_at=time.time(),
                )
            return
        while (
            job_id not in self._terminal_cleanup_completed
            and self._tracked_process_or_group_is_live(job_id, process)
        ):
            await self._terminate_process(
                job_id,
                process,
                graceful_timeout=0.5,
                kill_timeout=self._post_completion_cleanup_timeout_seconds(),
            )
            if self._supervisor_cleanup_uncertain(job_id, process=process):
                current = self.job_manager.get_job(job_id)
                if current is not None and current.state in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }:
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        current.state,
                        wrapper_cleanup_outcome=(
                            "cleanup_blocked_untrusted_process_identity"
                        ),
                        last_heartbeat_at=time.time(),
                    )
                return
            if (
                job_id not in self._terminal_cleanup_completed
                and self._tracked_process_or_group_is_live(job_id, process)
            ):
                current = self.job_manager.get_job(job_id)
                if current is not None and current.state in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }:
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        current.state,
                        wrapper_cleanup_outcome="cleanup_retry_pending_process_live",
                        last_heartbeat_at=time.time(),
                    )
                await asyncio.sleep(0.5)
        if job_id in self._terminal_cleanup_completed:
            return
        current = self.job_manager.get_job(job_id)
        if current is not None and current.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            self._complete_terminal_cleanup(
                job_id, "terminated_after_terminal_async"
            )
        else:
            self.processes.pop(job_id, None)
            self.repo_locks.release_job(job_id)

    def _observe_stdout_event(self, job_id: str, chunk: bytes, state: dict[str, Any]) -> bool:
        text = chunk.decode("utf-8", errors="replace").strip()
        session_id = None
        event_label = "stdout"
        progress = "Codex emitted stdout output."
        checkpoint = None
        now = time.time()
        item: Dict[str, Any] = {}
        stdout_completion_observed = False
        if text:
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                event_label = str(event.get("type") or "json_event")
                progress = self._event_progress_label(event)
                session_id = self._session_id_from_event(event)
                checkpoint = self._checkpoint_from_event(event)
                item = self._event_item_from_event(event)
                candidate = self._completion_candidate_from_event(event)
                if candidate is not None:
                    state["stdout_completion_candidate"] = candidate[0]
                    state["stdout_completion_candidate_status"] = candidate[1]
                if event_label == "turn.completed" and not state.get("semantic_terminal_seen"):
                    state["stdout_turn_completed_seen"] = True
                    state["stdout_turn_completed_at"] = now
                    stdout_completion_observed = True
        job = self.job_manager.get_job(job_id)
        if stdout_completion_observed and job is not None:
            candidate_result = state.get("stdout_completion_candidate")
            if not isinstance(candidate_result, dict):
                candidate_result = self._completion_evidence_result(job)
            candidate_status = str(
                state.get("stdout_completion_candidate_status")
                or ("checkpoint" if job.checkpoints else "missing")
            )
            self.job_manager.record_completion_evidence(
                job_id,
                source="stdout_turn_completed",
                observed_at=now,
                fallback_result=candidate_result,
                session_id=str(state.get("session_id") or job.session_id or ""),
                result_status=candidate_status,
            )
        updates = self._activity_updates_from_event(
            job,
            chunk=chunk,
            now=now,
            event_label=event_label,
            item=item,
        )
        updates.update(
            {
                "last_heartbeat_at": now,
                "last_event": event_label,
                "progress": progress,
            }
        )
        if checkpoint:
            updates["checkpoints"] = self._append_checkpoint(job_id, checkpoint)
            updates["progress"] = f"Worker checkpoint: {checkpoint['summary']}"
            updates["current_phase"] = "agent_message_emitted"
        if session_id and not state.get("session_id"):
            state["session_id"] = session_id
            updates["session_id"] = session_id
            updates["progress"] = "Codex session created; worker turn is now streaming events."
            updates["current_phase"] = "model_reasoning"
            session_started = True
        else:
            session_started = False
        self.job_manager.update_job_state(job_id, JobState.RUNNING, **updates)
        return session_started

    def _activity_updates_from_event(
        self,
        job: Any,
        *,
        chunk: bytes,
        now: float,
        event_label: str,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "event_count": (int(getattr(job, "event_count", 0) or 0) + 1) if job else 1,
            "stdout_bytes_seen": (int(getattr(job, "stdout_bytes_seen", 0) or 0) + len(chunk)) if job else len(chunk),
            "last_stdout_at": now,
        }
        item_type = str(item.get("type") or "").strip()
        item_status = str(item.get("status") or "").strip()
        if item_type:
            updates["current_item_type"] = item_type
        if item_status:
            updates["current_item_status"] = item_status

        command_preview = self._command_preview_from_item(item)
        if item_type == "command_execution":
            if event_label == "item.started":
                updates["current_phase"] = "command_running"
                updates["current_command_preview"] = command_preview
                updates["current_command_started_at"] = now
            elif event_label == "item.completed":
                updates["current_phase"] = "command_completed_waiting_for_model"
                updates["last_command_preview"] = command_preview or str(getattr(job, "current_command_preview", "") or "")
                updates["last_command_completed_at"] = now
                updates["current_command_preview"] = None
                updates["current_command_started_at"] = None
        elif item_type in {"agent_message", "message"}:
            updates["current_phase"] = "agent_message_emitted"
        elif event_label == "turn.completed":
            updates["current_phase"] = "finalizing_report"
        elif event_label == "thread.started":
            updates["current_phase"] = "model_reasoning"
        elif event_label == "turn.started":
            updates["current_phase"] = "model_reasoning"
        elif event_label.startswith("item."):
            updates["current_phase"] = item_type or "item_activity"
        elif event_label == "stdout":
            updates["current_phase"] = "stdout_output"
        return updates

    def _append_checkpoint(self, job_id: str, checkpoint: Dict[str, Any]) -> list[Dict[str, Any]]:
        job = self.job_manager.get_job(job_id)
        existing = list(job.checkpoints or []) if job else []
        existing.append(redact_sensitive_output(checkpoint))
        return existing[-MAX_JOB_CHECKPOINTS:]

    def _completion_candidate_from_event(
        self, event: Dict[str, Any]
    ) -> Optional[tuple[Dict[str, Any], str]]:
        item = self._agent_item_from_event(event)
        if not item:
            return None
        text = self._text_from_agent_item(item)
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
            status = (
                "malformed" if text.lstrip().startswith(("{", "[")) else "text"
            )
        else:
            status = "text"

        if isinstance(parsed, dict):
            result = dict(parsed)
            schema_valid = self._structured_result_is_valid(result)
            result.setdefault("files_changed", [])
            result.setdefault("result_source", "latest_agent_message_json")
            result.setdefault("codex_result_event_seen", False)
            result.setdefault("turn_completed_seen", True)
            result["parsed_output_schema_valid"] = schema_valid
            result["final_structured_result"] = False
            return result, "structured" if schema_valid else "malformed"
        if parsed is not None:
            text = str(parsed)
        return (
            {
                "summary": text,
                "files_changed": [],
                "result_source": "latest_agent_message_text",
                "codex_result_event_seen": False,
                "turn_completed_seen": True,
                "parsed_output_schema_valid": False,
                "final_structured_result": False,
            },
            status,
        )

    def _checkpoint_from_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item = self._agent_item_from_event(event)
        if not item:
            return None
        text = self._text_from_agent_item(item)
        if not text:
            return None

        summary = text
        details: Dict[str, Any] = {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            candidate = (
                parsed.get("summary")
                or parsed.get("detailed_report")
                or parsed.get("notes")
                or parsed.get("message")
            )
            if isinstance(candidate, str) and candidate.strip():
                summary = candidate.strip()
            for key in (
                "evidence",
                "files_changed",
                "commands_run",
                "tests_run",
                "risks",
                "open_questions",
                "next_steps",
            ):
                value = parsed.get(key)
                if isinstance(value, list):
                    details[f"{key}_count"] = len(value)
            if isinstance(parsed.get("notes"), str) and parsed["notes"].strip() and parsed["notes"].strip() != summary:
                details["notes"] = self._clip_checkpoint_text(parsed["notes"])

        return {
            "kind": "agent_message",
            "event": str(event.get("type") or "json_event"),
            "item_status": str(item.get("status") or ""),
            "at": time.time(),
            "summary": self._clip_checkpoint_text(summary),
            **details,
        }

    def _event_item_from_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item:
            return item
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        data_item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if data_item:
            return data_item
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        payload_item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if payload_item:
            return payload_item
        return {}

    def _agent_item_from_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        item = self._event_item_from_event(event)
        if item and str(item.get("type") or "") in {"agent_message", "message"}:
            return item
        return {}

    def _command_preview_from_item(self, item: Dict[str, Any]) -> str:
        if not item:
            return ""
        for key in ("command", "cmd", "text", "description", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return self._clip_status_text(value)
        for key in ("input", "arguments", "params"):
            value = item.get(key)
            if isinstance(value, dict):
                for nested_key in ("command", "cmd", "text", "description"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        return self._clip_status_text(nested)
                if value:
                    return self._clip_status_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
            if isinstance(value, str) and value.strip():
                return self._clip_status_text(value)
        return ""

    def _clip_status_text(self, value: str) -> str:
        text = redact_text(str(value or "").strip())
        text = " ".join(text.split())
        if len(text) <= 240:
            return text
        return text[:237].rstrip() + "..."

    def _text_from_agent_item(self, item: Dict[str, Any]) -> str:
        for key in ("text", "message", "content_text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for entry in content:
                if isinstance(entry, str) and entry.strip():
                    parts.append(entry.strip())
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in ("text", "content"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                        break
            if parts:
                return "\n".join(parts).strip()
        return ""

    def _clip_checkpoint_text(self, value: str) -> str:
        text = redact_text(str(value or "").strip())
        if len(text) <= MAX_CHECKPOINT_TEXT_CHARS:
            return text
        return text[:MAX_CHECKPOINT_TEXT_CHARS].rstrip() + "\n...[checkpoint truncated]"

    def _session_id_from_event(self, event: Dict[str, Any]) -> Optional[str]:
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id") or event.get("data", {}).get("thread_id")
            if thread_id:
                return str(thread_id)
        for container_name in ("thread", "data", "payload"):
            container = event.get(container_name)
            if isinstance(container, dict):
                thread_id = container.get("thread_id") or container.get("threadId") or container.get("id")
                if thread_id:
                    return str(thread_id)
        thread_id = event.get("thread_id")
        return str(thread_id) if thread_id else None

    def _event_progress_label(self, event: Dict[str, Any]) -> str:
        event_type = str(event.get("type") or "json_event")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item:
            item_type = str(item.get("type") or "item")
            status = str(item.get("status") or "").strip()
            return f"Codex event: {event_type} ({item_type}{', ' + status if status else ''})."
        return f"Codex event: {event_type}."
    
    def _build_env(self, *, job_id: str = "") -> Dict[str, str]:
        """Build a restricted environment for Codex execution."""
        allowed = self.config.get('security', {}).get('allowed_env_keys') or [
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "TMPDIR",
            "OPENAI_API_KEY",
        ]
        codex_home = str(resolve_codex_home(self.config, os.environ))
        allowed_set = set(allowed)
        if "*" in allowed_set:
            env = dict(os.environ)
        else:
            env = {k: v for k, v in os.environ.items() if k in allowed_set}
        env["CODEX_HOME"] = codex_home
        home = str(os.environ.get("HOME") or "").strip()
        if not home:
            codex_home_path = Path(codex_home).expanduser()
            home = str(codex_home_path.parent) if codex_home_path.name == ".codex" else str(Path.home())
        env["HOME"] = home
        env.setdefault("XDG_CONFIG_HOME", str(Path(home) / ".config"))
        git_config_global = str(os.environ.get("GIT_CONFIG_GLOBAL") or "").strip()
        if git_config_global:
            env["GIT_CONFIG_GLOBAL"] = git_config_global
        else:
            candidate = Path(home) / ".gitconfig"
            if candidate.exists():
                env["GIT_CONFIG_GLOBAL"] = str(candidate)
        if job_id:
            env[_JOB_PROCESS_MARKER_ENV] = self._job_process_marker(job_id)
        return env

    def _job_log_max_bytes(self) -> int:
        configured = int(self.config.get('logging', {}).get('job_log_max_bytes', 200_000))
        return max(1, configured)

    def _process_capture_max_bytes(self) -> int:
        configured = self.config.get("logging", {}).get(
            "process_capture_max_bytes",
            max(4_000_000, self._job_log_max_bytes()),
        )
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 4_000_000
        return max(self._job_log_max_bytes(), min(value, 64_000_000))

    def _process_event_line_max_bytes(self) -> int:
        configured = self.config.get("logging", {}).get(
            "process_event_line_max_bytes",
            8_000_000,
        )
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 8_000_000
        return max(64 * 1024, min(value, 64_000_000))

    def _write_process_artifact(self, path: Path, output: bytes) -> None:
        """Write bounded/redacted process artifacts unless raw logs are explicitly enabled."""
        if self.config.get('logging', {}).get('write_raw_job_logs', False):
            path.write_bytes(output)
            return

        text = output.decode('utf-8', errors='replace')
        safe_text = redact_text(text)
        encoded = safe_text.encode('utf-8')
        max_bytes = self._job_log_max_bytes()
        if len(encoded) > max_bytes:
            safe_text = encoded[:max_bytes].decode('utf-8', errors='replace')
            safe_text += f"\n...[log truncated to {max_bytes} bytes]"
        path.write_text(safe_text, encoding='utf-8')

    def _append_session_terminal_result(self, stdout: bytes, final_message: str) -> bytes:
        """Add a normalized final message when Codex only wrote it to session JSONL."""
        text = str(final_message or "").strip()
        if not text:
            return stdout
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        }
        terminal = {"type": "turn.completed", "source": "session_task_complete"}
        suffix = (json.dumps(event) + "\n" + json.dumps(terminal) + "\n").encode("utf-8")
        if stdout and not stdout.endswith(b"\n"):
            return stdout + b"\n" + suffix
        return stdout + suffix
    
    async def _parse_result(self, stdout: bytes, result_file: Path, options: Dict[str, Any] = None) -> Dict[str, Any]:
        """Parse result from Codex output."""
        if options is None:
            options = {}
            
        try:
            stdout_text = stdout.decode('utf-8').strip()
            
            # If structured output was disabled, return raw
            if not options.get('structured_output', True):
                return self._write_result_file(result_file, {
                    "summary": redact_text(stdout_text),
                    "raw_output": True,
                    "files_changed": [],
                    "parsed_output_schema_valid": False,
                    "final_structured_result": False,
                })
            
            # Parse JSONL - look for structured result
            lines = [line for line in stdout_text.split('\n') if line.strip()]
            turn_completed_seen = self._json_event_type_seen(lines, "turn.completed")
            
            if not lines:
                return self._write_result_file(result_file, {
                    "summary": "No output received",
                    "files_changed": [],
                    "result_source": "empty_stdout",
                    "codex_result_event_seen": False,
                    "turn_completed_seen": False,
                    "parsed_output_schema_valid": False,
                    "final_structured_result": False,
                })
            
            # Try to find the structured result in JSON events
            result = None
            for line in reversed(lines):
                try:
                    parsed = json.loads(line)
                    # Look for result event or last valid JSON
                    if isinstance(parsed, dict):
                        if parsed.get('type') == 'result' and 'data' in parsed:
                            result = parsed['data']
                            if isinstance(result, dict):
                                schema_valid = self._structured_result_is_valid(result)
                                result.setdefault("result_source", "codex_result_event")
                                result.setdefault("codex_result_event_seen", True)
                                result.setdefault("turn_completed_seen", turn_completed_seen)
                                result["parsed_output_schema_valid"] = schema_valid
                                result["final_structured_result"] = schema_valid
                            break
                        elif parsed.get('type') == 'item.completed':
                            item = self._agent_item_from_event(parsed)
                            text = self._text_from_agent_item(item) if item else ""
                            if text:
                                parsed_message_as_json = False
                                try:
                                    message_result = json.loads(text)
                                    if isinstance(message_result, dict):
                                        result = message_result
                                        parsed_message_as_json = self._structured_result_is_valid(
                                            message_result
                                        )
                                    else:
                                        result = {"summary": str(message_result), "files_changed": []}
                                except json.JSONDecodeError:
                                    result = {"summary": text, "files_changed": []}
                                if isinstance(result, dict):
                                    result.setdefault(
                                        "result_source",
                                        "latest_agent_message_json" if parsed_message_as_json else "latest_agent_message_text",
                                    )
                                    result.setdefault("codex_result_event_seen", False)
                                    result.setdefault("turn_completed_seen", turn_completed_seen)
                                    result["parsed_output_schema_valid"] = parsed_message_as_json
                                    result["final_structured_result"] = False
                                break
                        elif 'summary' in parsed:
                            result = parsed
                            schema_valid = self._structured_result_is_valid(result)
                            result.setdefault("result_source", "json_summary_event")
                            result.setdefault("codex_result_event_seen", False)
                            result.setdefault("turn_completed_seen", turn_completed_seen)
                            result["parsed_output_schema_valid"] = schema_valid
                            result["final_structured_result"] = schema_valid
                            break
                except json.JSONDecodeError:
                    continue
            
            if result:
                return self._write_result_file(result_file, result)
            
            return self._write_result_file(
                result_file,
                self._fallback_result_from_stdout(
                    stdout_text,
                    lines,
                    note="Could not extract a final structured Codex result event.",
                    turn_completed_seen=turn_completed_seen,
                ),
            )
            
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Codex result: %s", internal_log_error(e))
            stdout_text = stdout.decode('utf-8', errors='replace')
            return self._write_result_file(
                result_file,
                self._fallback_result_from_stdout(
                    stdout_text,
                    [line for line in stdout_text.split('\n') if line.strip()],
                    note="Could not parse Codex JSON output.",
                    turn_completed_seen=False,
                ),
            )

    def _json_event_type_seen(self, lines: list[str], event_type: str) -> bool:
        for line in lines:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("type") == event_type:
                return True
        return False

    def _write_result_file(self, result_file: Path, result: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a redacted result payload and return the same public payload."""
        safe_result = redact_sensitive_output(result)
        if isinstance(safe_result, dict):
            safe_result.setdefault("files_changed", [])
        result_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(safe_result, indent=2)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{result_file.name}.",
            suffix=".tmp",
            dir=result_file.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, result_file)
            if os.name != "nt":
                directory_fd = os.open(result_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            Path(temporary_path).unlink(missing_ok=True)
        return safe_result

    def _fallback_result_from_stdout(
        self,
        stdout_text: str,
        lines: list[str],
        *,
        note: str,
        turn_completed_seen: bool = False,
    ) -> Dict[str, Any]:
        """Build a manager-usable report when Codex did not emit the final schema."""
        latest_agent_result = self._latest_agent_message_result(lines)
        if latest_agent_result:
            latest_agent_result.setdefault("files_changed", [])
            latest_agent_result.setdefault("notes", note)
            latest_agent_result.setdefault("result_source", "latest_agent_message_text")
            latest_agent_result.setdefault("codex_result_event_seen", False)
            latest_agent_result.setdefault("turn_completed_seen", turn_completed_seen)
            latest_agent_result["parsed_output_schema_valid"] = False
            latest_agent_result["final_structured_result"] = False
            return latest_agent_result
        stdout_preview = self._fallback_stdout_preview(stdout_text, lines)
        return {
            "summary": (
                "No final structured worker report was captured, but PatchBay preserved bounded raw "
                "Codex output for manager inspection."
                if stdout_text
                else "No final structured worker report was captured."
            ),
            "files_changed": [],
            "notes": note,
            "result_source": "stdout_preview",
            "codex_result_event_seen": False,
            "turn_completed_seen": turn_completed_seen,
            "parsed_output_schema_valid": False,
            "final_structured_result": False,
            "raw_output_available": bool(stdout_text),
            "stdout_preview": stdout_preview,
        }

    def _fallback_stdout_preview(self, stdout_text: str, lines: list[str]) -> str:
        """Prefer useful tail/error material over the start of a JSON event stream."""
        if not stdout_text:
            return ""
        candidate_lines: list[str] = []
        for line in reversed(lines[-40:]):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                candidate_lines.append(stripped)
                continue
            if not isinstance(parsed, dict):
                candidate_lines.append(str(parsed))
                continue
            event_type = str(parsed.get("type") or "")
            if event_type in {"error", "turn.failed"}:
                candidate_lines.append(json.dumps(parsed, ensure_ascii=False))
                continue
            item = self._agent_item_from_event(parsed)
            text = self._text_from_agent_item(item) if item else ""
            if text:
                candidate_lines.append(text)
                continue
            raw_item = self._event_item_from_event(parsed)
            if str(raw_item.get("type") or "") == "error":
                candidate_lines.append(json.dumps(raw_item, ensure_ascii=False))
                continue
            if event_type in {"result", "turn.completed"}:
                candidate_lines.append(json.dumps(parsed, ensure_ascii=False))
        preview = "\n".join(reversed(candidate_lines[:6])).strip()
        if not preview:
            preview = stdout_text[-2000:]
        return redact_text(preview[-2000:])

    def _classify_codex_failure(self, stdout: bytes, stderr: bytes, exit_code: Optional[int]) -> Dict[str, Any] | None:
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        combined = f"{stderr_text}\n{stdout_text}"
        normalized = combined.lower()
        usage_limit_markers = (
            "usage limit",
            "quota has been reached",
            "quota exceeded",
            "rate limit exceeded for your account",
            "you have no weighted tokens left",
        )
        if any(marker in normalized for marker in usage_limit_markers):
            retry_match = re.search(
                r"(?:try again|retry|resets?)(?:\s+(?:at|after|in))?\s*[:=-]?\s*([^\n\r\"}]{1,80})",
                combined,
                flags=re.IGNORECASE,
            )
            retry_hint = retry_match.group(1).strip(" .") if retry_match else ""
            guidance = (
                "Codex rejected this turn because the selected account/model quota is temporarily exhausted. "
                "Preserve the worker and retry the same assignment after quota returns; this is not a PatchBay, repository, or brief failure."
            )
            if retry_hint:
                guidance += f" Reported retry guidance: {retry_hint}."
            return {
                "category": "codex_usage_limit",
                "exit_code": exit_code,
                "public_message": "Codex could not run this worker because its current usage quota is exhausted.",
                "manager_guidance": guidance,
                "operator_action": "Retry the same worker after quota becomes available, or explicitly choose another suitable available model.",
                "retry_without_operator_action": True,
                "retry_hint": retry_hint,
            }
        if (
            "refresh_token_reused" in normalized
            or "refresh token was already used" in normalized
            or "access token could not be refreshed" in normalized
            or ("token_expired" in normalized and "codex" in normalized)
        ):
            return {
                "category": "codex_auth_refresh_failed",
                "exit_code": exit_code,
                "public_message": (
                    "Codex authentication failed before the worker could run: the local Codex access token "
                    "could not be refreshed. Log in to Codex again on this host, then retry the worker."
                ),
                "manager_guidance": (
                    "Do not treat this as a repository or worker-brief failure. The host Codex login is invalid; "
                    "operator re-authentication is required before more workers can run."
                ),
                "operator_action": "Run `codex login` for the same user/CODEX_HOME used by PatchBay, then retry a small worker.",
                "retry_without_operator_action": False,
            }
        if "not inside a trusted directory" in normalized and "skip-git-repo-check" in normalized:
            return {
                "category": "codex_workspace_trust_failed",
                "exit_code": exit_code,
                "public_message": (
                    "Codex refused to run because the working directory is not trusted or not accepted by the current Codex CLI."
                ),
                "manager_guidance": "Check the repo path/trust setup or run from an allowed git workspace before retrying.",
                "operator_action": "Open or trust the workspace for Codex, or configure the job to run in a valid repository.",
                "retry_without_operator_action": False,
            }
        if "unknown model" in normalized or "model not found" in normalized or "invalid model" in normalized:
            return {
                "category": "codex_model_unavailable",
                "exit_code": exit_code,
                "public_message": "Codex rejected the selected model before the worker could run.",
                "manager_guidance": "Call codex_worker_options and retry with a model id returned by the current Codex runtime.",
                "operator_action": "Choose a currently available Codex model.",
                "retry_without_operator_action": True,
            }
        return None

    def _attach_failure_diagnostic(self, result: Dict[str, Any], failure: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(result) if isinstance(result, dict) else {"summary": str(result), "files_changed": []}
        summary = str(payload.get("summary") or "").strip()
        generic_missing_report = summary in {
            "",
            "No final structured worker report was captured.",
            "No final structured worker report was captured, but PatchBay preserved bounded raw Codex output for manager inspection.",
        }
        if generic_missing_report:
            payload["summary"] = failure["public_message"]
        payload.setdefault("files_changed", [])
        payload["failure_diagnostic"] = redact_sensitive_output(failure)
        notes = str(payload.get("notes") or "").strip()
        guidance = str(failure.get("manager_guidance") or "").strip()
        if guidance and guidance not in notes:
            payload["notes"] = f"{notes}\n\n{guidance}".strip() if notes else guidance
        return payload

    def _latest_agent_message_result(self, lines: list[str]) -> Optional[Dict[str, Any]]:
        """Return the latest agent message as a result-shaped payload."""
        for line in reversed(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            item = self._agent_item_from_event(parsed)
            text = self._text_from_agent_item(item) if item else ""
            if not text:
                continue
            try:
                message_result = json.loads(text)
            except json.JSONDecodeError:
                return {"summary": text, "files_changed": []}
            if isinstance(message_result, dict):
                return dict(message_result)
            return {"summary": str(message_result), "files_changed": []}
        return None

    def _cancelled_progress_label(self, result: Dict[str, Any]) -> str:
        """Describe exactly what was preserved when a running worker is stopped."""
        if result.get("summary") and result.get("summary") != "Worker turn was stopped before a final report. No partial worker message was captured.":
            return "Codex process was stopped; PatchBay preserved a partial worker report."
        if result.get("raw_output_available"):
            return "Codex process was stopped; PatchBay preserved bounded raw output but no partial worker report."
        return "Codex process was stopped before PatchBay captured a partial worker report."

    async def _parse_partial_result(
        self,
        stdout: bytes,
        result_file: Path,
        options: Dict[str, Any] = None,
        *,
        reason: str,
        status: str = "cancelled",
    ) -> Dict[str, Any]:
        """Parse and persist the latest useful report from a stopped worker turn."""
        result = await self._parse_result(stdout, result_file, options)
        if not isinstance(result, dict):
            result = {"summary": str(result), "files_changed": []}
        summary = str(result.get("summary") or "").strip()
        if not summary or summary == "No output received":
            result["summary"] = "Worker turn was stopped before a final report. No partial worker message was captured."
        result.setdefault("files_changed", [])
        result["partial"] = True
        result["partial_reason"] = redact_text(reason)
        result["status"] = status
        safe_result = redact_sensitive_output(result)
        return self._write_result_file(result_file, safe_result)
    
    def _extract_session_id_from_json_events(self, stdout: str) -> Optional[str]:
        """Extract thread_id/session_id from JSON events in stdout."""
        for line in stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    # Look for thread.started event
                    if event.get('type') == 'thread.started':
                        thread_id = event.get('thread_id') or event.get('data', {}).get('thread_id')
                        if thread_id:
                            return thread_id
                    # Also check for thread_id in any event
                    if 'thread_id' in event:
                        return event['thread_id']
            except json.JSONDecodeError:
                continue
        return None
    
    def _extract_session_id(self, stderr: str) -> Optional[str]:
        """Extract Codex session ID from stderr (fallback)."""
        for line in stderr.split('\n'):
            if 'session' in line.lower() or 'id' in line.lower():
                match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', line)
                if match:
                    return match.group(0)
        return None
    
    async def cancel_job(self, job_id: str, reason: str = "Cancelled by request") -> Dict[str, Any]:
        """Cancel a running job."""
        event_loop = asyncio.get_running_loop()
        job = self.job_manager.get_job(job_id)
        if not job:
            return {"cancelled": False, "reason": f"Unknown job: {job_id}"}
        if job.state not in (JobState.PENDING, JobState.RUNNING):
            logger.warning(f"Cannot cancel job {job_id}: state={job.state}")
            return {"cancelled": False, "job_id": job_id, "state": job.state.value, "reason": "Job is not running"}

        # Publish cancellation intent before any recovery probe yields control.
        # The POSIX launch gate and this write share the same transition lock,
        # so a cancellation that begins before launch commitment cannot let the
        # supervised target start while asyncio is still returning its handle.
        with self._terminal_cleanup_transition_lock:
            current = self.job_manager.get_job(job_id)
            if current is None or current.state not in {
                JobState.PENDING,
                JobState.RUNNING,
            }:
                return {
                    "cancelled": False,
                    "job_id": job_id,
                    "state": current.state.value if current else "unknown",
                    "reason": "Job reached a terminal state before cancellation began",
                }
            self._cancellation_intents.add(job_id)

        # A manager stop racing a completed Codex turn must preserve completion,
        # not overwrite the final report with cancellation metadata.
        if job.state == JobState.RUNNING:
            recovered_completion = await asyncio.to_thread(
                self._recover_completed_session,
                job,
                event_loop=event_loop,
            )
            if recovered_completion:
                with self._terminal_cleanup_transition_lock:
                    self._cancellation_intents.discard(job_id)
                return {
                    "cancelled": False,
                    "completed": True,
                    "job_id": job_id,
                    "state": JobState.COMPLETED.value,
                    "reason": "Codex had already completed; PatchBay recovered the final report.",
                }
            current = self.job_manager.get_job(job_id)
            if (
                current is not None
                and current.state == JobState.RUNNING
                and current.completion_evidence_source == "stdout_turn_completed"
            ):
                recovered_completion = await asyncio.to_thread(
                    self._recover_completion_evidence,
                    current,
                    event_loop=event_loop,
                )
                if recovered_completion:
                    with self._terminal_cleanup_transition_lock:
                        self._cancellation_intents.discard(job_id)
                    return {
                        "cancelled": False,
                        "completed": True,
                        "job_id": job_id,
                        "state": JobState.COMPLETED.value,
                        "reason": (
                            "Codex had already emitted durable turn completion; "
                            "PatchBay preserved the final available report."
                        ),
                    }

        process = self.processes.get(job_id)
        process_signalled = False
        minimal_result = self._minimal_cancelled_result(reason)
        with self._terminal_cleanup_transition_lock:
            # Install the turnstile before exposing terminal state. A live
            # in-process job already satisfies this through its bound lease.
            if job_id not in self._terminal_cleanup_completed:
                self._ensure_cleanup_repo_block(job)
            transitioned = self._transition_job_terminal_with_cleanup(
                job_id,
                JobState.CANCELLED,
                result=minimal_result,
                error=reason,
                terminal_source="manager_cancellation",
                terminal_observed_at=time.time(),
                wrapper_cleanup_outcome="cleanup_pending",
                last_heartbeat_at=time.time(),
                progress=self._cancelled_progress_label(minimal_result),
            )
        if not transitioned:
            with self._terminal_cleanup_transition_lock:
                self._cancellation_intents.discard(job_id)
            current = self.job_manager.get_job(job_id)
            return {
                "cancelled": False,
                "job_id": job_id,
                "state": current.state.value if current else "unknown",
                "reason": "Job reached a terminal state before cancellation was committed",
            }
        executor_task = self.tasks.get(job_id)
        if process is None and executor_task is not None and not executor_task.done():
            self._cancel_task_on_owner_loop(executor_task, event_loop)
        latest_after_cancel = self.job_manager.get_job(job_id)
        latest_options = dict(
            getattr(latest_after_cancel, "options", None) or {}
        )
        launch_cleanup_owned = bool(
            process is None
            and executor_task is not None
            and not executor_task.done()
            and latest_options.get(_JOB_PROCESS_SUPERVISOR_VERSION_OPTION)
            == _JOB_PROCESS_SUPERVISOR_VERSION
            and latest_options.get(_JOB_PROCESS_SUPERVISOR_LAUNCHING_OPTION)
            is True
            and not getattr(latest_after_cancel, "process_pid", None)
        )
        if launch_cleanup_owned:
            # The OS process exists, but asyncio has not returned its handle to
            # the executor yet. Marker-based recovery here could signal the
            # supervisor before its readiness handshake and bypass its durable
            # cleanup proof. The launch owner is cancellation-shielded; leave
            # the barrier pending and let that owner register and reap the exact
            # handle before it exits.
            partial_result = await self._cancelled_result_from_existing_artifacts(
                job, reason=reason
            )
            self.job_manager.update_job_state(
                job_id,
                JobState.CANCELLED,
                result=partial_result,
                error=reason,
                wrapper_cleanup_outcome="cleanup_pending",
                last_heartbeat_at=time.time(),
                progress=self._cancelled_progress_label(partial_result),
            )
            with self._terminal_cleanup_transition_lock:
                self._cancellation_intents.discard(job_id)
            logger.info(
                "Job %s cancellation cleanup delegated to its gated launch owner",
                job_id,
            )
            return {
                "cancelled": True,
                "job_id": job_id,
                "state": JobState.CANCELLED.value,
                "process_signalled": False,
                "cleanup_pending": True,
            }
        if process and self._tracked_process_or_group_is_live(job_id, process):
            process_signalled = await self._terminate_process(job_id, process)
        recorded_cleanup_outcome = ""
        recorded_cleanup_reaped = False
        marked_pids = self._job_marked_process_pids(
            job_id, force_refresh=True
        )
        if (
            not process_signalled
            and (
                (
                    isinstance(job.process_pid, int)
                    and
                    self._recorded_process_pid_is_trustworthy(job)
                    and self._process_pid_is_live(job.process_pid)
                )
                or (
                    isinstance(job.process_pgid, int)
                    and
                    self._recorded_process_group_is_trustworthy(job)
                    and self._process_group_has_live_members(int(job.process_pgid))
                )
                or marked_pids
            )
        ):
            recorded_cleanup_outcome = await asyncio.to_thread(
                self._terminate_recorded_process, job
            )
            recorded_cleanup_reaped = recorded_cleanup_outcome in {
                "process_not_live",
                "terminated_after_terminal_recovery",
            }
            process_signalled = recorded_cleanup_outcome.startswith("terminated")
        if not process_signalled:
            partial_result = await self._cancelled_result_from_existing_artifacts(job, reason=reason)
            self.job_manager.update_job_state(
                job_id,
                JobState.CANCELLED,
                result=partial_result,
                error=reason,
                last_heartbeat_at=time.time(),
                progress=self._cancelled_progress_label(partial_result),
            )

        self._finish_cancelled_cleanup(
            job,
            process,
            recorded_cleanup_outcome=recorded_cleanup_outcome,
            recorded_cleanup_reaped=recorded_cleanup_reaped,
        )
        with self._terminal_cleanup_transition_lock:
            self._cancellation_intents.discard(job_id)
        logger.info(f"Job {job_id} cancelled")
        return {
            "cancelled": True,
            "job_id": job_id,
            "state": JobState.CANCELLED.value,
            "process_signalled": process_signalled,
        }

    def _finish_cancelled_cleanup(
        self,
        job: Any,
        process: Optional[asyncio.subprocess.Process],
        *,
        recorded_cleanup_outcome: str,
        recorded_cleanup_reaped: bool,
    ) -> None:
        """Commit cancellation cleanup without reopening a completed barrier."""

        job_id = job.job_id
        with self._terminal_cleanup_transition_lock:
            if job_id in self._terminal_cleanup_completed:
                current = self.job_manager.get_job(job_id)
                current_outcome = str(
                    getattr(current, "wrapper_cleanup_outcome", "") or ""
                )
                self._complete_terminal_cleanup(
                    job_id,
                    current_outcome
                    if current_outcome
                    and not terminal_cleanup_pending(current_outcome)
                    else recorded_cleanup_outcome
                    or "process_not_live_after_terminal",
                )
                return

            tracked_cleanup_liveness = (
                self._tracked_process_or_group_liveness(job_id, process)
                if process is not None
                else False
            )
            live_process = tracked_cleanup_liveness is not False
            marked_pids = self._job_marked_process_pids(
                job_id, force_refresh=True
            )
            live_recorded_member = bool(
                (
                    (
                        isinstance(job.process_pid, int)
                        and
                        self._recorded_process_pid_is_trustworthy(job)
                        and self._process_pid_is_live(job.process_pid)
                    )
                    or (
                        isinstance(job.process_pgid, int)
                        and
                        self._recorded_process_group_is_trustworthy(job)
                        and self._process_group_has_live_members(
                            int(job.process_pgid)
                        )
                    )
                    or marked_pids
                )
            )
            executor_task = self.tasks.get(job_id)
            executor_task_alive = bool(
                executor_task is not None and not executor_task.done()
            )
            untrusted_live = self._recorded_cleanup_has_untrusted_live_members(job)
            if recorded_cleanup_reaped or (
                process is not None and tracked_cleanup_liveness is False
            ):
                self._complete_terminal_cleanup(
                    job_id,
                    recorded_cleanup_outcome
                    or "process_not_live_after_terminal",
                )
            elif live_process or live_recorded_member or executor_task_alive:
                current = self.job_manager.get_job(job_id)
                if current is not None:
                    self._ensure_cleanup_repo_block(current)
                    self._transition_job_terminal_with_cleanup(
                        job_id,
                        JobState.CANCELLED,
                        wrapper_cleanup_outcome=(
                            recorded_cleanup_outcome
                            if terminal_cleanup_pending(recorded_cleanup_outcome)
                            else "cleanup_retry_pending_process_live"
                        ),
                        last_heartbeat_at=time.time(),
                    )
                if process is not None:
                    self._schedule_terminal_cleanup(job_id, process)
                elif live_recorded_member:
                    self._schedule_recorded_terminal_cleanup(job_id)
            elif untrusted_live:
                self._transition_job_terminal_with_cleanup(
                    job_id,
                    JobState.CANCELLED,
                    wrapper_cleanup_outcome=(
                        "cleanup_blocked_untrusted_process_identity"
                    ),
                    last_heartbeat_at=time.time(),
                )
            else:
                self._complete_terminal_cleanup(
                    job_id,
                    recorded_cleanup_outcome
                    or "process_not_live_after_terminal",
                )

    def _minimal_cancelled_result(self, reason: str) -> Dict[str, Any]:
        """Return the report persisted atomically with cancellation state."""

        return {
            "summary": "Worker turn was stopped before PatchBay captured Codex output.",
            "files_changed": [],
            "partial": True,
            "partial_reason": redact_text(reason),
            "status": "cancelled",
            "parsed_output_schema_valid": False,
            "final_structured_result": False,
            "raw_output_available": False,
        }

    async def _cancelled_result_from_existing_artifacts(self, job: Any, *, reason: str) -> Dict[str, Any]:
        """Persist manager-readable evidence when cancellation happens outside the live process path."""
        result_file = self.job_logs_dir / f"{job.job_id}_result.json"
        stdout_log = self.job_logs_dir / f"{job.job_id}_stdout.log"
        if isinstance(getattr(job, "result", None), dict):
            result = dict(job.result)
            result.setdefault("files_changed", [])
            result["partial"] = True
            result["partial_reason"] = redact_text(reason)
            result["status"] = "cancelled"
            return self._write_result_file(result_file, result)
        if result_file.exists():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    payload.setdefault("files_changed", [])
                    payload["partial"] = True
                    payload["partial_reason"] = redact_text(reason)
                    payload["status"] = "cancelled"
                    return self._write_result_file(result_file, payload)
            except Exception as error:
                logger.warning("Failed to reuse result artifact for cancelled job %s: %s", job.job_id, internal_log_error(error))
        if stdout_log.exists():
            try:
                return await self._parse_partial_result(stdout_log.read_bytes(), result_file, job.options, reason=reason)
            except Exception as error:
                logger.warning("Failed to parse stdout artifact for cancelled job %s: %s", job.job_id, internal_log_error(error))
        return self._write_result_file(result_file, self._minimal_cancelled_result(reason))

    async def cancel_all_running(self, reason: str = "Server shutting down") -> None:
        """Cancel every tracked subprocess and mark queued/running jobs terminal."""
        for job_id, process in list(self.processes.items()):
            job = self.job_manager.get_job(job_id)
            if job is not None and job.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                if self._tracked_process_or_group_is_live(job_id, process):
                    await self._terminate_process(job_id, process)
                if not self._tracked_process_or_group_is_live(job_id, process):
                    self.processes.pop(job_id, None)
                    self.repo_locks.release_job(job_id)
                else:
                    self._schedule_terminal_cleanup(job_id, process)
                continue
            await self.cancel_job(job_id, reason=reason)
        for job_id, job in list(self.job_manager.jobs.items()):
            if job.state in {JobState.PENDING, JobState.RUNNING}:
                await self.cancel_job(job_id, reason=reason)
        pending_cleanup = [task for task in self.cleanup_tasks.values() if not task.done()]
        if pending_cleanup:
            _, still_pending = await asyncio.wait(
                pending_cleanup,
                timeout=self._post_completion_cleanup_timeout_seconds(),
            )
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.wait(still_pending, timeout=0.1)
        with self._cleanup_threads_lock:
            cleanup_threads = [
                thread for thread in self.cleanup_threads.values() if thread.is_alive()
            ]
        if cleanup_threads:
            timeout = self._post_completion_cleanup_timeout_seconds()
            await asyncio.gather(
                *(
                    asyncio.to_thread(thread.join, timeout)
                    for thread in cleanup_threads
                )
            )

    async def _terminate_process(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        *,
        graceful_timeout: float = 5.0,
        kill_timeout: Optional[float] = None,
    ) -> bool:
        if self._supervisor_cleanup_uncertain(job_id, process=process):
            logger.error(
                "Job %s supervisor reported uncertain descendant ownership; "
                "retaining its sentinel and inherited repository lock for "
                "explicit operator recovery",
                job_id,
            )
            return False
        pid = getattr(process, "pid", None)
        job = self.job_manager.get_job(job_id)
        pgid = int(
            (getattr(job, "process_pgid", None) if job is not None else None)
            or pid
            or 0
        )
        leader_live = process.returncode is None
        group_live = self._process_group_has_live_members(pgid)
        marked_live = bool(self._job_marked_process_pids(job_id))
        group_owned = bool(
            self.processes.get(job_id) is process
            or (
                job is not None
                and self._recorded_process_group_is_trustworthy(job)
            )
        )
        descendant_liveness = self._tracked_descendant_liveness(job_id)
        if (
            not leader_live
            and not (group_owned and group_live)
            and not marked_live
            and descendant_liveness is False
        ):
            return False
        group_signalled = False
        signalled = False
        effective_graceful_timeout = max(0.1, float(graceful_timeout))
        if self._supervisor_cleanup_contract_installed(job):
            # Let the job-private supervisor terminate descendants and publish
            # its durable cleanup proof before escalating against the
            # supervisor itself. This is especially important when many Darwin
            # workers complete together and process discovery is contended.
            effective_graceful_timeout = max(
                effective_graceful_timeout,
                _SUPERVISOR_CLEANUP_GRACE_FLOOR_SECONDS,
            )
        if isinstance(pid, int) and pid > 0 and leader_live:
            if await self._drain_current_job_descendants(
                job_id,
                pid,
                graceful_timeout=max(0.1, float(graceful_timeout) / 2),
            ):
                signalled = True
        if self._signal_exact_job_descendants(job_id, signal.SIGTERM):
            signalled = True
        if pgid > 0 and pgid != os.getpgrp() and group_owned and group_live:
            try:
                os.killpg(pgid, signal.SIGTERM)
                group_signalled = True
                signalled = True
            except (ProcessLookupError, PermissionError, OSError):
                group_signalled = False
        if not group_signalled and process.returncode is None:
            process.terminate()
            signalled = True
        if self._signal_job_marked_processes(job_id, signal.SIGTERM):
            signalled = True
        exited = await self._wait_for_process_group_exit(
            job_id,
            process,
            pgid,
            timeout=effective_graceful_timeout,
        )
        if not exited:
            if self._supervisor_cleanup_uncertain(job_id, process=process):
                logger.error(
                    "Job %s supervisor entered fail-closed cleanup sentinel; "
                    "SIGKILL escalation is intentionally withheld",
                    job_id,
                )
                return signalled
            logger.warning(f"Job {job_id} did not terminate gracefully; killing")
            group_live = self._process_group_has_live_members(pgid)
            job = self.job_manager.get_job(job_id)
            group_owned = bool(
                self.processes.get(job_id) is process
                or (
                    job is not None
                    and self._recorded_process_group_is_trustworthy(job)
                )
            )
            if pgid > 0 and pgid != os.getpgrp() and group_owned and group_live:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    signalled = True
                except (ProcessLookupError, PermissionError, OSError):
                    if process.returncode is None:
                        process.kill()
            elif process.returncode is None:
                process.kill()
                signalled = True
            if self._signal_job_marked_processes(job_id, signal.SIGKILL):
                signalled = True
            if self._signal_exact_job_descendants(job_id, signal.SIGKILL):
                signalled = True
            if self._signal_live_job_descendants(job_id, signal.SIGKILL):
                signalled = True
            final_wait = (
                self._post_completion_cleanup_timeout_seconds()
                if kill_timeout is None
                else max(0.1, float(kill_timeout))
            )
            if process.returncode is None:
                try:
                    # The subprocess transport can observe SIGKILL slightly
                    # after process-group discovery has already gone empty,
                    # especially on Darwin under concurrent test or worker
                    # load. Reap the exact leader first, then prove that its
                    # group, markers, descendants, and supervisor contract are
                    # all empty below. This never turns leader exit alone into
                    # permission to release the repository lock.
                    await asyncio.wait_for(
                        process.wait(), timeout=min(1.0, final_wait)
                    )
                except asyncio.TimeoutError:
                    pass
            exited = await self._wait_for_process_group_exit(
                job_id,
                process,
                pgid,
                timeout=final_wait,
            )
            if (
                not exited
                and process.returncode is not None
                and self._process_group_liveness(pgid) is False
                and self._tracked_descendant_liveness(job_id) is False
                and self._supervisor_cleanup_contract_installed(
                    self.job_manager.get_job(job_id)
                )
            ):
                # Under host load the gated supervisor can publish its exact
                # absence proof just after the ordinary kill window. Give the
                # proof a bounded final grace period before exposing a
                # recovery-pending outcome; do not infer safety without it.
                exited = await self._wait_for_process_group_exit(
                    job_id,
                    process,
                    pgid,
                    timeout=max(1.5, final_wait),
                )
            if not exited:
                final_job = self.job_manager.get_job(job_id)
                final_options = dict(
                    getattr(final_job, "options", None) or {}
                )
                logger.error(
                    "Job %s process group did not exit after kill within %.2fs; "
                    "cleanup will reconcile asynchronously "
                    "(returncode=%r group_liveness=%r marker_liveness=%r "
                    "descendant_liveness=%r descendant_flags=%r)",
                    job_id,
                    final_wait,
                    getattr(process, "returncode", None),
                    self._process_group_liveness(pgid),
                    self._job_marked_process_pids(job_id, force_refresh=True),
                    self._tracked_descendant_liveness(job_id),
                    {
                        key: final_options.get(key)
                        for key in (
                            _JOB_DESCENDANT_UNTRUSTED_OPTION,
                            _JOB_DESCENDANT_SCAN_UNCERTAIN_OPTION,
                            _JOB_DESCENDANT_IDENTITIES_OPTION,
                        )
                        if key in final_options
                    },
                )
        return signalled

    async def _wait_for_process_group_exit(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        pgid: int,
        *,
        timeout: float,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            if self._supervisor_cleanup_proven(job_id, process=process):
                return True
            group_liveness = self._process_group_liveness(pgid)
            marked_pids = self._job_marked_process_pids(job_id)
            descendant_liveness = self._tracked_descendant_liveness(job_id)
            if (
                process.returncode is not None
                and group_liveness is False
                and marked_pids == set()
                and descendant_liveness is False
                and not self._supervisor_cleanup_unproven(
                    job_id, process=process
                )
            ):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    def _job_is_cancelled(self, job_id: str) -> bool:
        job = self.job_manager.get_job(job_id)
        return bool(
            job_id in self._cancellation_intents
            or (job and job.state == JobState.CANCELLED)
        )

    def _job_launch_blocked(self, job_id: str) -> bool:
        job = self.job_manager.get_job(job_id)
        return bool(
            job_id in self._cancellation_intents
            or (
                job
                and job.state
                in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
            )
        )
    
    def get_diff(self, job_id: str, file_path: str) -> Optional[str]:
        """Get unified diff for a file in a job's worktree."""
        job = self.job_manager.get_job(job_id)
        if not job or not job.worktree_path:
            return None
        if job.mode != "apply" or job.state != JobState.COMPLETED:
            return None
        
        try:
            worktree_path = Path(job.worktree_path).resolve()
            file_full_path = validate_allowed_path(
                str(worktree_path / file_path),
                [str(worktree_path)],
            )
            rel_path = os.path.relpath(file_full_path, worktree_path).replace(os.sep, "/")
            if rel_path.startswith("../") or rel_path == "..":
                return None

            # First try git diff with proper -- separator
            result = subprocess.run(
                ['git', 'diff', 'HEAD', '--', rel_path],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return self._format_diff_output(result.stdout)

            status = subprocess.run(
                ['git', 'status', '--porcelain', '--', rel_path],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if status.returncode != 0 or not status.stdout.strip():
                return None
            if not any(line.startswith("?? ") for line in status.stdout.splitlines()):
                return None

            return self._diff_untracked_file(file_full_path, rel_path) or None
                
        except Exception as e:
            logger.debug(f"Failed to get diff for {file_path}: {e}")
            return None

    def _format_diff_output(self, diff: str) -> str:
        safe_diff = redact_text(diff)
        max_bytes = int(self.config.get('security', {}).get('max_diff_bytes', 200_000))
        encoded = safe_diff.encode('utf-8')
        if len(encoded) > max_bytes:
            safe_diff = encoded[:max_bytes].decode('utf-8', errors='replace')
            safe_diff += f"\n...[diff truncated to {max_bytes} bytes]"
        return safe_diff

    def _diff_untracked_file(self, path: Path, rel_path: str) -> str:
        if not path.exists() or not path.is_file():
            return ""
        sample = path.read_bytes()[:4096]
        if b"\0" in sample:
            return f"--- /dev/null\n+++ b/{rel_path}\nBinary file changed; diff omitted."
        content = path.read_text(encoding='utf-8', errors='replace')
        diff = f"--- /dev/null\n+++ b/{rel_path}\n" + "".join(
            f"+{line}\n" for line in content.splitlines()
        )
        return self._format_diff_output(diff)
