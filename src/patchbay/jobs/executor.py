"""Job execution engine for running Codex CLI commands."""
import asyncio
import hashlib
import json
import logging
import subprocess
import re
import os
import signal
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
from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver
from patchbay.connector.profiles import normalize_logging_paths, resolve_runtime_path
from patchbay.repo_locks import RepoMutationLockManager
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

_CODEX_STARTUP_LOCKS: Dict[tuple[int, str], asyncio.Lock] = {}


@dataclass
class ProcessCapture:
    stdout: bytes
    stderr: bytes
    session_id: Optional[str] = None
    session_start_timed_out: bool = False
    total_timed_out: bool = False
    semantic_terminal_seen: bool = False
    terminal_source: str = ""
    terminal_observed_at: Optional[float] = None
    session_final_message: str = ""
    wrapper_cleanup_outcome: str = ""


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

    def reconcile_stale_running_jobs(
        self,
        *,
        grace_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Fail durable running jobs that no longer have a tracked subprocess."""
        grace = self._stale_running_grace_seconds(grace_seconds)
        current_time = time.time() if now is None else float(now)
        checked = 0
        reconciled: list[str] = []
        recovered_completed: list[str] = []

        for job_id, job in list(self.job_manager.jobs.items()):
            if job.state != JobState.RUNNING:
                continue
            checked += 1
            task = self.tasks.get(job_id)
            process = self.processes.get(job_id)
            if not (task is not None and not task.done()) and process is None:
                if self._recover_completed_session(job):
                    recovered_completed.append(job_id)
                    continue
            if self._job_has_live_runtime(job_id):
                continue
            if job.last_heartbeat_at is not None and current_time - float(job.last_heartbeat_at) < max(grace, 10.0):
                continue
            if job.started_at is not None and current_time - float(job.started_at) < grace:
                continue

            self.job_manager.update_job_state(
                job_id,
                JobState.FAILED,
                error=STALE_RUNNING_JOB_ERROR,
                result=self._stale_running_result(job),
            )
            self.repo_locks.release_job(job_id)
            reconciled.append(job_id)
            logger.warning("Reconciled stale running job %s with no tracked process", job_id)

        return {
            "checked": checked,
            "reconciled": len(reconciled),
            "job_ids": reconciled,
            "recovered_completed": len(recovered_completed),
            "recovered_completed_job_ids": recovered_completed,
            "grace_seconds": grace,
        }

    def _recover_completed_session(self, job: Any) -> bool:
        """Recover a persisted running job whose exact Codex session is terminal."""
        session_id = str(getattr(job, "session_id", "") or "")
        if not session_id:
            return False
        not_before = float(getattr(job, "process_started_at", None) or getattr(job, "started_at", None) or 0)
        snapshot = CodexSessionTerminalObserver(
            self.config,
            session_id,
            not_before=not_before,
        ).poll()
        if not snapshot.completed:
            return False

        pid = getattr(job, "process_pid", None)
        cleanup_outcome = "process_not_live"
        if isinstance(pid, int) and self._process_pid_is_live(pid):
            if not getattr(job, "process_identity", None) or not self._recorded_process_pid_is_trustworthy(job):
                return False
            cleanup_outcome = self._terminate_recorded_process(job)
            if self._process_pid_is_live(pid):
                return False

        result_file = self.job_logs_dir / f"{job.job_id}_result.json"
        result = self._result_from_session_message(snapshot.final_message, result_file)
        transitioned = self.job_manager.transition_job_terminal(
            job.job_id,
            JobState.COMPLETED,
            result=result,
            terminal_source=snapshot.source,
            terminal_observed_at=snapshot.observed_at or time.time(),
            wrapper_cleanup_outcome=cleanup_outcome,
            last_heartbeat_at=time.time(),
            last_event="session_task_complete_recovered",
        )
        if transitioned:
            self.repo_locks.release_job(job.job_id)
            logger.info("Recovered completed Codex session for durable job %s", job.job_id)
        return transitioned

    def _terminate_recorded_process(self, job: Any) -> str:
        """Terminate a persisted process only after its exact start marker matches."""
        pid = int(job.process_pid)
        pgid = int(getattr(job, "process_pgid", None) or pid)
        try:
            if pgid == pid and os.getpgid(pid) == pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "process_not_live"
        except (PermissionError, OSError):
            return "cleanup_signal_failed"
        deadline = time.time() + self._post_completion_exit_grace_seconds()
        while self._process_pid_is_live(pid) and time.time() < deadline:
            time.sleep(0.05)
        if self._process_pid_is_live(pid):
            try:
                if pgid == pid and os.getpgid(pid) == pgid:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                return "cleanup_kill_failed"
        return "terminated_after_terminal_recovery"

    def _result_from_session_message(self, final_message: str, result_file: Path) -> Dict[str, Any]:
        text = str(final_message or "").strip()
        parsed: Any = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict):
            result = dict(parsed)
            result.setdefault("files_changed", [])
            result.setdefault("parsed_output_schema_valid", True)
            result.setdefault("final_structured_result", True)
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

    def _job_has_live_runtime(self, job_id: str) -> bool:
        task = self.tasks.get(job_id)
        if task is not None and not task.done():
            return True
        process = self.processes.get(job_id)
        if process is not None:
            return getattr(process, "returncode", None) is None
        job = self.job_manager.get_job(job_id)
        if job and job.process_pid and self._recorded_process_pid_is_trustworthy(job) and self._process_pid_is_live(int(job.process_pid)):
            return True
        return False

    def _recorded_process_pid_is_trustworthy(self, job: Any) -> bool:
        """Avoid trusting an old persisted pid forever after process tracking is lost."""
        recorded_identity = str(getattr(job, "process_identity", "") or "")
        if recorded_identity:
            return self._process_identity(getattr(job, "process_pid", None)) == recorded_identity
        timestamps = [
            value
            for value in (getattr(job, "last_heartbeat_at", None), getattr(job, "process_started_at", None), getattr(job, "started_at", None))
            if value is not None
        ]
        if not timestamps:
            return False
        try:
            newest = max(float(value) for value in timestamps)
            trust_seconds = float(self.config.get("server", {}).get("stale_running_pid_trust_seconds", 3600))
        except (TypeError, ValueError):
            return False
        return time.time() - newest <= max(0.0, trust_seconds)

    def _process_pid_is_live(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _process_identity(self, pid: Any) -> Optional[str]:
        """Return a stable Linux process start marker when available."""
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        _, separator, suffix = stat_text.rpartition(")")
        fields = suffix.strip().split() if separator else []
        if len(fields) <= 19:
            return None
        return f"linux-proc-start:{fields[19]}"

    def _stale_running_result(self, job: Any) -> Dict[str, Any]:
        """Return a diagnostic payload for a job PatchBay can no longer track."""
        return redact_sensitive_output(
            {
                "summary": STALE_RUNNING_JOB_ERROR,
                "files_changed": [],
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
            await asyncio.to_thread(fcntl.flock, file_handle.fileno(), fcntl.LOCK_EX)
        except Exception:
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
        except Exception:
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
        if job.state == JobState.CANCELLED:
            logger.info(f"Job {job_id} was cancelled before execution started")
            self.repo_locks.release_job(job_id)
            return
        
        try:
            self.job_manager.update_job_state(
                job_id,
                JobState.RUNNING,
                launch_started_at=time.time(),
                last_heartbeat_at=time.time(),
            )
            
            # Build command and keep prompt text off argv when the Codex CLI supports stdin.
            cmd = self._build_codex_command(job.mode, job.prompt, job.worktree_path, job.options)
            stdin_data = self._stdin_for_command(job.prompt, cmd)
            if self._job_is_cancelled(job_id):
                logger.info(f"Job {job_id} was cancelled before process launch")
                self.repo_locks.release_job(job_id)
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
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=job.worktree_path,
                    stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._build_env(),
                    start_new_session=True,
                )
                self.processes[job_id] = process
                process_pid = getattr(process, "pid", None)
                self.job_manager.update_job_state(
                    job_id,
                    JobState.RUNNING,
                    process_started_at=time.time(),
                    process_pid=process_pid,
                    process_pgid=process_pid,
                    process_identity=self._process_identity(process_pid),
                    last_heartbeat_at=time.time(),
                    current_phase="codex_process_started_waiting_for_session",
                    progress="Codex process started; waiting for session creation.",
                )
                logger.info("Job %s Codex process started: pid=%s", job_id, getattr(process, "pid", None))
            except Exception:
                if startup_gate is not None:
                    startup_gate.release("process_launch_failed")
                raise
            
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
                    startup_gate=startup_gate,
                )
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
                    self.job_manager.transition_job_terminal(
                        job_id,
                        JobState.CANCELLED,
                        result=partial_result,
                        session_id=session_id,
                        exit_code=process.returncode,
                        error=str(cancel_reason),
                        last_heartbeat_at=time.time(),
                        last_event="process.cancelled",
                        progress=self._cancelled_progress_label(partial_result),
                    )
                    logger.info(f"Job {job_id} process exited after cancellation")
                    return

                self.job_manager.update_job_state(
                    job_id,
                    JobState.RUNNING,
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
                    self.job_manager.transition_job_terminal(
                        job_id,
                        JobState.FAILED,
                        result=startup_result,
                        error=(
                            "Codex process started but did not create a JSON session before the startup "
                            "timeout. Inspect local job stdout/stderr logs for startup diagnostics."
                        ),
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
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
                    self.job_manager.transition_job_terminal(
                        job_id,
                        JobState.FAILED,
                        result=timeout_result,
                        error=f"Job timed out after {timeout} seconds",
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                    )
                    logger.error(f"Job {job_id} timed out")
                    return
                
                raw_stdout = stdout.decode('utf-8', errors='replace')
                
                result = await self._parse_result(stdout, result_file, job.options)
                
                # Extract session ID from JSON events (stdout) first, then fall back to stderr
                session_id = capture.session_id or self._extract_session_id_from_json_events(raw_stdout)
                if not session_id:
                    session_id = self._extract_session_id(stderr.decode('utf-8'))
                
                result = redact_sensitive_output(result)
                
                if process.returncode == 0 or capture.semantic_terminal_seen:
                    self.job_manager.transition_job_terminal(
                        job_id,
                        JobState.COMPLETED,
                        result=result,
                        session_id=session_id,
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                        terminal_source=capture.terminal_source or "process_exit",
                        terminal_observed_at=capture.terminal_observed_at or time.time(),
                        wrapper_cleanup_outcome=capture.wrapper_cleanup_outcome or "process_exited",
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
                    self.job_manager.transition_job_terminal(
                        job_id,
                        JobState.FAILED,
                        result=result,
                        error=error_message,
                        exit_code=process.returncode,
                        last_heartbeat_at=time.time(),
                    )
                    logger.error("Job %s failed: exit code %s%s", job_id, process.returncode, f" ({failure['category']})" if failure else "")
                    
            finally:
                if startup_gate is not None:
                    startup_gate.release("job_finally")
                self.processes.pop(job_id, None)
                self.repo_locks.release_job(job_id)
                
        except Exception as e:
            if self._job_is_cancelled(job_id):
                logger.info(f"Job {job_id} stopped after cancellation")
                self.repo_locks.release_job(job_id)
                return
            logger.error("Job %s execution failed: %s", job_id, internal_log_error(e))
            self.job_manager.transition_job_terminal(
                job_id,
                JobState.FAILED,
                error=public_error_message(e, default="Job execution failed."),
                last_heartbeat_at=time.time(),
            )
            self.repo_locks.release_job(job_id)
    
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
        startup_gate: StartupGateLease | None = None,
    ) -> ProcessCapture:
        """Read Codex JSON events incrementally so session/heartbeat state is live."""
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        state: dict[str, Any] = {
            "session_id": None,
            "session_start_timed_out": False,
            "total_timed_out": False,
            "semantic_terminal_seen": False,
            "terminal_source": "",
            "terminal_observed_at": None,
            "session_final_message": "",
            "wrapper_cleanup_outcome": "",
        }
        process_started_at = time.time()
        session_observer: CodexSessionTerminalObserver | None = None

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

        async def read_stream(stream: asyncio.StreamReader | None, *, stream_name: str) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                if stream_name == "stdout":
                    stdout_chunks.append(chunk)
                    session_started = self._observe_stdout_event(job_id, chunk, state)
                    if session_started and startup_gate is not None:
                        startup_gate.release("session_created")
                else:
                    stderr_chunks.append(chunk)
                    now = time.time()
                    job = self.job_manager.get_job(job_id)
                    self.job_manager.update_job_state(
                        job_id,
                        JobState.RUNNING,
                        last_heartbeat_at=now,
                        last_event="stderr",
                        progress="Codex emitted stderr output; inspect local stderr log if the turn fails.",
                        event_count=(int(job.event_count or 0) + 1) if job else 1,
                        stderr_bytes_seen=(int(job.stderr_bytes_seen or 0) + len(chunk)) if job else len(chunk),
                        last_stderr_at=now,
                    )

        stdin_task = asyncio.create_task(feed_stdin())
        stdout_task = asyncio.create_task(read_stream(process.stdout, stream_name="stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, stream_name="stderr"))
        wait_task = asyncio.create_task(process.wait())
        tasks = [stdin_task, stdout_task, stderr_task, wait_task]

        try:
            while not wait_task.done():
                await asyncio.sleep(0.5)
                now = time.time()
                session_id = str(state.get("session_id") or "")
                if session_id and session_observer is None:
                    session_observer = CodexSessionTerminalObserver(
                        self.config,
                        session_id,
                        not_before=process_started_at,
                    )
                if session_observer is not None and not state.get("semantic_terminal_seen"):
                    terminal = session_observer.poll()
                    if terminal.completed:
                        state["semantic_terminal_seen"] = True
                        state["terminal_source"] = terminal.source
                        state["terminal_observed_at"] = terminal.observed_at or now
                        state["session_final_message"] = terminal.final_message
                        self.job_manager.claim_job_semantic_completion(
                            job_id,
                            source=terminal.source,
                            observed_at=float(state["terminal_observed_at"]),
                        )
                        self.job_manager.update_job_state(
                            job_id,
                            JobState.RUNNING,
                            last_heartbeat_at=now,
                            last_event=terminal.source,
                            current_phase="codex_complete_cleaning_up_wrapper",
                            progress=(
                                "Codex completed the turn; PatchBay is preserving the report and "
                                "cleaning up the CLI wrapper."
                            ),
                        )
                if state.get("semantic_terminal_seen"):
                    observed_at = float(state.get("terminal_observed_at") or now)
                    if now - observed_at >= self._post_completion_exit_grace_seconds():
                        terminated = await self._terminate_process(job_id, process)
                        state["wrapper_cleanup_outcome"] = (
                            "terminated_after_terminal" if terminated else "already_exited"
                        )
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
            await wait_task
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            await asyncio.gather(stdin_task, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        return ProcessCapture(
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            session_id=state.get("session_id"),
            session_start_timed_out=bool(state.get("session_start_timed_out")),
            total_timed_out=bool(state.get("total_timed_out")),
            semantic_terminal_seen=bool(state.get("semantic_terminal_seen")),
            terminal_source=str(state.get("terminal_source") or ""),
            terminal_observed_at=state.get("terminal_observed_at"),
            session_final_message=str(state.get("session_final_message") or ""),
            wrapper_cleanup_outcome=str(state.get("wrapper_cleanup_outcome") or ""),
        )

    def _observe_stdout_event(self, job_id: str, chunk: bytes, state: dict[str, Any]) -> bool:
        text = chunk.decode("utf-8", errors="replace").strip()
        session_id = None
        event_label = "stdout"
        progress = "Codex emitted stdout output."
        checkpoint = None
        now = time.time()
        item: Dict[str, Any] = {}
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
                if event_label == "turn.completed" and not state.get("semantic_terminal_seen"):
                    state["semantic_terminal_seen"] = True
                    state["terminal_source"] = "stdout_turn_completed"
                    state["terminal_observed_at"] = now
                    self.job_manager.claim_job_semantic_completion(
                        job_id,
                        source="stdout_turn_completed",
                        observed_at=now,
                    )
        job = self.job_manager.get_job(job_id)
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
    
    def _build_env(self) -> Dict[str, str]:
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
        return env

    def _job_log_max_bytes(self) -> int:
        configured = int(self.config.get('logging', {}).get('job_log_max_bytes', 200_000))
        return max(1, configured)

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
                    "files_changed": []
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
                                result.setdefault("result_source", "codex_result_event")
                                result.setdefault("codex_result_event_seen", True)
                                result.setdefault("turn_completed_seen", turn_completed_seen)
                                result.setdefault("parsed_output_schema_valid", True)
                                result.setdefault("final_structured_result", True)
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
                                        parsed_message_as_json = True
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
                                    result.setdefault("parsed_output_schema_valid", parsed_message_as_json)
                                    result.setdefault("final_structured_result", False)
                                break
                        elif 'summary' in parsed:
                            result = parsed
                            result.setdefault("result_source", "json_summary_event")
                            result.setdefault("codex_result_event_seen", False)
                            result.setdefault("turn_completed_seen", turn_completed_seen)
                            result.setdefault("parsed_output_schema_valid", True)
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
        result_file.write_text(json.dumps(safe_result, indent=2), encoding="utf-8")
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
            latest_agent_result.setdefault("parsed_output_schema_valid", False)
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
        result_file.write_text(json.dumps(safe_result, indent=2))
        return safe_result
    
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
        job = self.job_manager.get_job(job_id)
        if not job:
            return {"cancelled": False, "reason": f"Unknown job: {job_id}"}
        if job.state not in (JobState.PENDING, JobState.RUNNING):
            logger.warning(f"Cannot cancel job {job_id}: state={job.state}")
            return {"cancelled": False, "job_id": job_id, "state": job.state.value, "reason": "Job is not running"}

        process = self.processes.get(job_id)
        process_signalled = False
        transitioned = self.job_manager.transition_job_terminal(
            job_id,
            JobState.CANCELLED,
            error=reason,
            terminal_source="manager_cancellation",
            terminal_observed_at=time.time(),
        )
        if not transitioned:
            current = self.job_manager.get_job(job_id)
            return {
                "cancelled": False,
                "job_id": job_id,
                "state": current.state.value if current else "unknown",
                "reason": "Job reached a terminal state before cancellation was committed",
            }
        if process and process.returncode is None:
            process_signalled = await self._terminate_process(job_id, process)
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

        self.repo_locks.release_job(job_id)
        logger.info(f"Job {job_id} cancelled")
        return {
            "cancelled": True,
            "job_id": job_id,
            "state": JobState.CANCELLED.value,
            "process_signalled": process_signalled,
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
        return self._write_result_file(
            result_file,
            {
                "summary": "Worker turn was stopped before PatchBay captured Codex output.",
                "files_changed": [],
                "partial": True,
                "partial_reason": redact_text(reason),
                "status": "cancelled",
                "final_structured_result": False,
                "raw_output_available": False,
            },
        )

    async def cancel_all_running(self, reason: str = "Server shutting down") -> None:
        """Cancel every tracked subprocess and mark queued/running jobs terminal."""
        for job_id in list(self.processes.keys()):
            await self.cancel_job(job_id, reason=reason)
        self.job_manager.mark_active_jobs_cancelled(reason)

    async def _terminate_process(self, job_id: str, process: asyncio.subprocess.Process) -> bool:
        if process.returncode is not None:
            return False
        pid = getattr(process, "pid", None)
        group_signalled = False
        if isinstance(pid, int) and pid > 0:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGTERM)
                    group_signalled = True
            except (ProcessLookupError, PermissionError, OSError):
                group_signalled = False
        if not group_signalled:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(f"Job {job_id} did not terminate gracefully; killing")
            if group_signalled and isinstance(pid, int):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
            else:
                process.kill()
            await process.wait()
        return True

    def _job_is_cancelled(self, job_id: str) -> bool:
        job = self.job_manager.get_job(job_id)
        return bool(job and job.state == JobState.CANCELLED)
    
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
