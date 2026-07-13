"""
Job state management for PatchBay server.
Handles job lifecycle, worktree management, and state tracking.
"""
import uuid
import time
import json
import logging
import os
import threading
import errno
import tempfile
from typing import Any, Callable, Dict, Optional
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, asdict
import git

from patchbay.connector.profiles import normalize_logging_paths
from patchbay.evidence import EvidenceRecorder
from patchbay.security import internal_log_error, redact_sensitive_output, validate_allowed_path

logger = logging.getLogger(__name__)


class JobState(str, Enum):
    """Job execution states"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_CLEANUP_PENDING_OUTCOMES = frozenset(
    {
        "cleanup_pending",
        "cleanup_timeout_after_terminal",
        "cleanup_retry_pending_process_live",
        "cleanup_blocked_untrusted_process_identity",
        "cleanup_signal_failed",
        "cleanup_kill_failed",
    }
)

TERMINAL_CLEANUP_RECOVERY_REQUIRED_OUTCOMES = frozenset(
    {
        "cleanup_blocked_untrusted_process_identity",
        "cleanup_signal_failed",
        "cleanup_kill_failed",
    }
)


def terminal_cleanup_pending(outcome: Any) -> bool:
    """Return whether semantic completion still owns transport cleanup."""

    return str(outcome or "") in TERMINAL_CLEANUP_PENDING_OUTCOMES


def terminal_cleanup_recovery_required(outcome: Any) -> bool:
    """Return whether cleanup needs operator-visible recovery, not blind retry."""

    return str(outcome or "") in TERMINAL_CLEANUP_RECOVERY_REQUIRED_OUTCOMES


@dataclass
class JobInfo:
    """Job metadata and state"""
    job_id: str
    state: JobState
    mode: str  # "plan" or "apply"
    prompt: str
    repo_path: str
    options: Optional[Dict[str, Any]] = None
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    session_id: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    last_event: Optional[str] = None
    progress: Optional[str] = None
    launch_started_at: Optional[float] = None
    process_started_at: Optional[float] = None
    process_pid: Optional[int] = None
    process_pgid: Optional[int] = None
    process_identity: Optional[str] = None
    last_heartbeat_at: Optional[float] = None
    event_count: int = 0
    stdout_bytes_seen: int = 0
    stderr_bytes_seen: int = 0
    last_stdout_at: Optional[float] = None
    last_stderr_at: Optional[float] = None
    current_phase: Optional[str] = None
    current_item_type: Optional[str] = None
    current_item_status: Optional[str] = None
    current_command_preview: Optional[str] = None
    current_command_started_at: Optional[float] = None
    last_command_preview: Optional[str] = None
    last_command_completed_at: Optional[float] = None
    checkpoints: Optional[list[Dict[str, Any]]] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    prompt_artifact: Optional[str] = None
    prompt_sha256: Optional[str] = None
    prompt_bytes: Optional[int] = None
    prompt_recorded_at: Optional[str] = None
    terminal_source: Optional[str] = None
    terminal_observed_at: Optional[float] = None
    completion_evidence_source: Optional[str] = None
    completion_evidence_observed_at: Optional[float] = None
    completion_evidence_session_id: Optional[str] = None
    completion_evidence_result_status: Optional[str] = None
    completion_evidence_version: Optional[int] = None
    completion_evidence_result: Optional[Dict[str, Any]] = None
    wrapper_cleanup_outcome: Optional[str] = None
    late_terminal_source: Optional[str] = None
    late_terminal_observed_at: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, handling enum serialization"""
        data = asdict(self)
        data['state'] = self.state.value
        return data

    def to_persisted_dict(self) -> Dict[str, Any]:
        """Convert to a redacted durable job record."""
        data = self.to_dict()
        data.pop("prompt", None)
        if self.result:
            data["result"] = {
                key: redact_sensitive_output(value)
                for key, value in self.result.items()
                if not key.startswith("_")
            }
        if self.completion_evidence_result:
            data["completion_evidence_result"] = redact_sensitive_output(
                self.completion_evidence_result
            )
        if self.checkpoints:
            data["checkpoints"] = redact_sensitive_output(self.checkpoints)
        if self.error:
            data["error"] = redact_sensitive_output(self.error)
        return data

    @classmethod
    def from_persisted_dict(cls, data: Dict[str, Any]) -> "JobInfo":
        """Rehydrate a persisted redacted job record."""
        values = dict(data)
        values["state"] = JobState(values.get("state", JobState.FAILED.value))
        values["prompt"] = ""
        values.pop("prompt_preview", None)
        return cls(**{key: value for key, value in values.items() if key in cls.__dataclass_fields__})


class JobManager:
    """
    Manages Codex job lifecycle and worktree isolation.
    """
    
    def __init__(self, config: Dict[str, Any]):
        normalize_logging_paths(config)
        self.config = config
        self.jobs: Dict[str, JobInfo] = {}
        self._admission_lock = threading.Lock()
        # Job mutations, their durable snapshots, and record deletion share one
        # re-entrant boundary. create_job takes _admission_lock before this lock;
        # no other path takes _admission_lock, so the ordering cannot invert.
        self._state_lock = threading.RLock()
        self.max_concurrent = config['server']['max_concurrent_jobs']
        self.queue_enabled = bool(config.get("server", {}).get("queue_enabled", False))
        self.job_timeout = config['server']['job_timeout_seconds']
        self.cleanup_after_hours = config['server'].get('job_cleanup_after_hours', 24)
        logging_config = config.get('logging', {})
        self.job_logs_dir = Path(logging_config['job_logs_dir']).expanduser().resolve()
        self.job_logs_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir = Path(logging_config['worktrees_dir']).expanduser().resolve()
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.job_state_dir = Path(logging_config['job_state_dir']).expanduser().resolve()
        self.job_state_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_recorder = EvidenceRecorder(config)
        self._load_jobs()
        
        timeout_label = "disabled" if str(self.job_timeout).strip().lower() in {"", "0", "none", "never", "unlimited", "disabled", "false"} else f"{self.job_timeout}s"
        logger.info(
            "JobManager initialized: max_concurrent=%s, queue_enabled=%s, timeout=%s",
            self.max_concurrent,
            self.queue_enabled,
            timeout_label,
        )
    
    def create_job(self, mode: str, prompt: str, repo_path: str, options: Optional[Dict] = None) -> str:
        """Create a new job under the active-job admission lock."""
        with self._admission_lock:
            return self._create_job_unlocked(mode, prompt, repo_path, options)

    def active_job_count(self) -> int:
        """Return PENDING + RUNNING jobs that count against concurrency."""
        with self._state_lock:
            return sum(1 for job in self.jobs.values() if job.state in (JobState.PENDING, JobState.RUNNING))

    def _create_job_unlocked(self, mode: str, prompt: str, repo_path: str, options: Optional[Dict] = None) -> str:
        """
        Create a new job with unique worktree.
        
        Args:
            mode: "plan" or "apply"
            prompt: User's prompt for Codex
            repo_path: Repository path to operate on
            options: Additional job options
            
        Returns:
            job_id: Unique job identifier
        """
        # Check concurrent limit (0 = unlimited)
        if not self.queue_enabled and self.max_concurrent > 0:
            active_count = self.active_job_count()
            if active_count >= self.max_concurrent:
                raise RuntimeError(
                    f"Maximum active jobs ({self.max_concurrent}) reached; "
                    "active includes pending and running jobs. Inspect or wait before starting another worker."
                )
        
        
        repo_path = str(Path(repo_path).expanduser().resolve())
        self._validate_repo_allowed(repo_path)

        if self.config.get('security', {}).get('require_git_repo', False):
            try:
                git.Repo(repo_path)
            except git.InvalidGitRepositoryError:
                raise ValueError(f"Not a git repository: {repo_path}")
        
        # Generate unique job ID
        job_id = str(uuid.uuid4())
        
        # Create job info
        job = JobInfo(
            job_id=job_id,
            state=JobState.PENDING,
            mode=mode,
            prompt=prompt,
            repo_path=repo_path,
            options=options or {}
        )
        
        # Create or assign worktree for this job
        if mode == "apply":  # Only apply mode needs writable worktree
            try:
                worktree_path, branch_name = self._create_worktree(job_id, repo_path)
                job.worktree_path = str(worktree_path)
                job.branch_name = branch_name
                logger.info("Created isolated worktree for job %s", job_id)
            except Exception as e:
                logger.error("Failed to create isolated worktree for job %s: %s", job_id, internal_log_error(e))
                raise
        else:
            options = job.options or {}
            worker_worktree = options.get("_worker_worktree_path")
            if worker_worktree:
                job.worktree_path = str(self._validate_worker_worktree_path(str(worker_worktree)))
                if options.get("_worker_branch_name"):
                    job.branch_name = str(options["_worker_branch_name"])
            else:
                # Plan/read-only/shared modes can use the main repo.
                job.worktree_path = repo_path

        prompt_record = self.evidence_recorder.record_job_brief(
            job_id=job_id,
            mode=mode,
            prompt=prompt,
            repo_path=repo_path,
            options=job.options or {},
            worktree_path=job.worktree_path,
            branch_name=job.branch_name,
        )
        if prompt_record:
            job.prompt_artifact = prompt_record["prompt_artifact"]
            job.prompt_sha256 = prompt_record["prompt_sha256"]
            job.prompt_bytes = prompt_record["prompt_bytes"]
            job.prompt_recorded_at = prompt_record["prompt_recorded_at"]
        
        with self._state_lock:
            self.jobs[job_id] = job
            self._persist_job(job)
        logger.info("Created job %s: mode=%s", job_id, mode)
        
        return job_id

    def _validate_repo_allowed(self, repo_path: str) -> None:
        """Require repo_path to sit under one of the configured allowed roots."""
        allowed = self.config.get('repositories', {}).get('allowed') or []
        validate_allowed_path(repo_path, allowed)
    
    def _create_worktree(self, job_id: str, repo_path: str) -> tuple[Path, str]:
        """
        Create a git worktree for isolated execution.
        
        Args:
            job_id: Unique job identifier
            repo_path: Base repository path
            
        Returns:
            (worktree_path, branch_name)
        """
        try:
            repo = git.Repo(repo_path)
        except git.InvalidGitRepositoryError as error:
            raise ValueError("Job worktree could not be created: base repository is not a git repository") from error
        
        # Generate unique branch name
        branch_name = f"codex/job-{job_id[:8]}"
        
        # Create worktree directory
        worktree_path = self.worktrees_dir / f"job-{job_id[:8]}"
        
        # Add worktree
        try:
            repo.git.worktree('add', str(worktree_path), '-b', branch_name)
        except Exception as error:
            raise self._worktree_creation_error("Job worktree could not be created", error) from error

        return worktree_path, branch_name

    def worker_worktrees_dir(self) -> Path:
        """Return the external root for durable named worker worktrees."""
        workers_config = self.config.get("workers", {})
        configured = workers_config.get("worktree_root") or workers_config.get("worktrees_dir")
        if configured:
            root = Path(configured).expanduser()
        elif os.environ.get("PATCHBAY_HOME"):
            root = Path(os.environ["PATCHBAY_HOME"]).expanduser() / "worktrees"
        else:
            root = Path.home() / ".patchbay" / "worktrees"
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def create_worker_worktree(self, worker_id: str, repo_path: str) -> tuple[Path, str, str]:
        """Create one external durable worktree for a named worker."""
        repo_path = str(Path(repo_path).expanduser().resolve())
        self._validate_repo_allowed(repo_path)
        try:
            repo = git.Repo(repo_path)
        except git.InvalidGitRepositoryError as error:
            raise ValueError("Worker worktree could not be created: base repository is not a git repository") from error
        base_revision = repo.head.commit.hexsha
        suffix = "".join(ch if ch.isalnum() else "-" for ch in worker_id.lower()).strip("-")[:32]
        branch_name = f"codex/worker-{suffix}"
        worktree_path = self.worker_worktrees_dir() / f"worker-{suffix}"
        if worktree_path.exists():
            raise ValueError(f"Worker worktree already exists: {worktree_path}")
        try:
            repo.git.worktree("add", str(worktree_path), "-b", branch_name)
        except Exception as error:
            raise self._worktree_creation_error("Worker worktree could not be created", error) from error
        logger.info("Created worker worktree for worker %s", worker_id)
        return worktree_path.resolve(), branch_name, base_revision

    def _worktree_creation_error(self, prefix: str, error: Exception) -> ValueError:
        text = str(error).lower()
        if isinstance(error, OSError) and getattr(error, "errno", None) == errno.ENOSPC:
            return ValueError(f"{prefix}: host filesystem is full")
        if "no space left on device" in text:
            return ValueError(f"{prefix}: host filesystem is full")
        if "not a git repository" in text:
            return ValueError(f"{prefix}: base repository is not a git repository")
        if "already exists" in text:
            return ValueError(f"{prefix}: target worktree already exists")
        return ValueError(f"{prefix}: git worktree command failed; inspect server logs for details")

    def remove_worker_worktree(self, repo_path: str, worktree_path: str, branch_name: Optional[str] = None) -> None:
        """Remove an external durable worker worktree and its private branch."""
        repo_path = str(Path(repo_path).expanduser().resolve())
        self._validate_repo_allowed(repo_path)
        path = self._validate_worker_worktree_path(worktree_path)
        repo = git.Repo(repo_path)
        if path.exists():
            repo.git.worktree("remove", str(path), "--force")
            logger.info("Removed worker worktree")
        if branch_name:
            try:
                repo.git.branch("-D", branch_name)
                logger.info("Deleted worker branch")
            except Exception as error:
                logger.warning("Failed to delete worker branch: %s", internal_log_error(error))

    def update_job_options(self, job_id: str, options: Dict[str, Any]) -> None:
        """Replace a job's private options and persist the durable record."""
        with self._state_lock:
            if job_id not in self.jobs:
                raise ValueError(f"Unknown job: {job_id}")
            job = self.jobs[job_id]
            job.options = dict(options)
            self._persist_job(job)

    def mutate_job_options(
        self,
        job_id: str,
        mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Atomically read, patch, and persist private job options."""

        with self._state_lock:
            if job_id not in self.jobs:
                raise ValueError(f"Unknown job: {job_id}")
            job = self.jobs[job_id]
            options = dict(job.options or {})
            replacement = mutator(options)
            if replacement is not None:
                options = dict(replacement)
            job.options = options
            self._persist_job(job)
            return dict(options)

    def _validate_worker_worktree_path(self, worktree_path: str) -> Path:
        path = Path(worktree_path).expanduser().resolve()
        root = self.worker_worktrees_dir()
        if path != root and root not in path.parents:
            raise ValueError(f"Worker worktree path is outside worker root: {worktree_path}")
        return path
    
    def update_job_state(self, job_id: str, state: JobState, **kwargs):
        """Update job state and metadata"""
        with self._state_lock:
            if job_id not in self.jobs:
                raise ValueError(f"Unknown job: {job_id}")

            job = self.jobs[job_id]
            terminal_states = {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }
            authoritative_sources = {
                "session_task_complete",
                "stdout_turn_completed",
            }
            if job.state in terminal_states and state != job.state:
                logger.debug(
                    "Ignored state change for terminal job %s: %s -> %s",
                    job_id,
                    job.state,
                    state,
                )
                return
            if (
                job.state in {JobState.PENDING, JobState.RUNNING}
                and state in {JobState.FAILED, JobState.CANCELLED}
                and job.terminal_source in authoritative_sources
            ):
                logger.debug(
                    "Ignored non-completion terminal change after semantic completion claim for job %s",
                    job_id,
                )
                return
            if (
                job.state in terminal_states
                and job.terminal_source in authoritative_sources
                and isinstance(job.result, dict)
            ):
                kwargs.pop("result", None)
                kwargs.pop("error", None)
                for protected in (
                    "terminal_source",
                    "terminal_observed_at",
                    "session_id",
                    "exit_code",
                    "completed_at",
                ):
                    if getattr(job, protected, None) is not None:
                        kwargs.pop(protected, None)
            job.state = state

            # Update timestamps
            if state == JobState.RUNNING and job.started_at is None:
                job.started_at = time.time()
            elif state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                job.completed_at = time.time()

            if state in (JobState.RUNNING, JobState.COMPLETED) and "error" not in kwargs:
                job.error = None

            # Update other fields
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)

            if state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                self._normalize_terminal_job(job)

            logger.debug(f"Job {job_id} state updated: {state}")
            self._persist_job(job)

    def transition_job_terminal(self, job_id: str, state: JobState, **kwargs) -> bool:
        """Commit the first terminal decision and make later decisions no-ops."""
        if state not in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
            raise ValueError(f"Not a terminal job state: {state}")
        with self._state_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"Unknown job: {job_id}")
            if (
                state in {JobState.FAILED, JobState.CANCELLED}
                and job.state in (JobState.PENDING, JobState.RUNNING)
                and job.terminal_source in {"session_task_complete", "stdout_turn_completed"}
            ):
                job.late_terminal_source = str(kwargs.get("terminal_source") or "manager_cancellation")
                job.late_terminal_observed_at = float(kwargs.get("terminal_observed_at") or time.time())
                self._persist_job(job)
                return False
            if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                terminal_source = str(kwargs.get("terminal_source") or "")
                if job.state == state:
                    if (
                        job.terminal_source
                        in {"session_task_complete", "stdout_turn_completed"}
                        and isinstance(job.result, dict)
                    ):
                        for protected in (
                            "result",
                            "error",
                            "terminal_source",
                            "terminal_observed_at",
                            "session_id",
                            "exit_code",
                            "completed_at",
                        ):
                            kwargs.pop(protected, None)
                    for key, value in kwargs.items():
                        if hasattr(job, key):
                            setattr(job, key, value)
                    self._normalize_terminal_job(job)
                    self._persist_job(job)
                    return True
                terminal_source = kwargs.get("terminal_source")
                terminal_observed_at = kwargs.get("terminal_observed_at")
                if terminal_source and not job.late_terminal_source:
                    job.late_terminal_source = str(terminal_source)
                    job.late_terminal_observed_at = (
                        float(terminal_observed_at) if terminal_observed_at is not None else time.time()
                    )
                    self._persist_job(job)
                return False
            self.update_job_state(job_id, state, **kwargs)
            return True

    def record_completion_evidence(
        self,
        job_id: str,
        *,
        source: str,
        observed_at: float,
        fallback_result: Dict[str, Any],
        session_id: str = "",
        result_status: str = "missing",
    ) -> bool:
        """Persist pre-terminal completion evidence without choosing terminal state.

        A stdout ``turn.completed`` event proves the Codex turn finished, but
        the exact session report may still arrive and has higher authority.
        Keeping this separate from ``terminal_source`` preserves that ordering
        while making a crash between event observation and wrapper exit
        recoverable.
        """

        if source != "stdout_turn_completed":
            raise ValueError("Unsupported completion evidence source")
        allowed_statuses = {
            "structured",
            "text",
            "checkpoint",
            "missing",
            "malformed",
            "truncated",
        }
        if result_status not in allowed_statuses:
            raise ValueError("Unsupported completion evidence result status")
        with self._state_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"Unknown job: {job_id}")
            if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                return False
            if job.completion_evidence_source:
                return job.completion_evidence_source == source
            bounded_result, bounded_status = self._bounded_completion_evidence_result(
                fallback_result,
                result_status=result_status,
            )
            job.completion_evidence_source = str(source)
            job.completion_evidence_observed_at = float(observed_at)
            job.completion_evidence_session_id = str(session_id or "") or None
            job.completion_evidence_result_status = bounded_status
            job.completion_evidence_version = 1
            job.completion_evidence_result = bounded_result
            self._persist_job(job)
            return True

    def _bounded_completion_evidence_result(
        self,
        value: Dict[str, Any],
        *,
        result_status: str,
    ) -> tuple[Dict[str, Any], str]:
        """Redact and structurally compact one pre-terminal report envelope."""

        redacted = redact_sensitive_output(dict(value))
        safe = (
            {key: item for key, item in redacted.items() if not str(key).startswith("_")}
            if isinstance(redacted, dict)
            else {}
        )
        configured = self.config.get("logging", {}).get(
            "job_log_max_bytes", 200_000
        )
        try:
            limit = max(2, min(int(configured), 200_000))
        except (TypeError, ValueError):
            limit = 200_000

        def encoded_size(payload: Dict[str, Any]) -> int:
            return len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

        if encoded_size(safe) <= limit:
            return safe, result_status

        summary = str(safe.get("summary") or "Completion report exceeded the durable evidence limit.")
        compact: Dict[str, Any] = {
            "summary": summary,
            "report_completeness": "truncated",
        }
        while summary and encoded_size(compact) > limit:
            summary = summary[: max(0, len(summary) // 2)]
            compact["summary"] = summary
        if encoded_size(compact) > limit:
            compact = {"summary": "[truncated]"}
        if encoded_size(compact) > limit:
            compact = {}

        for key in (
            "detailed_report",
            "files_changed",
            "commands_run",
            "tests_run",
            "notes",
            "risks",
            "open_questions",
            "next_steps",
            "parsed_output_schema_valid",
            "final_structured_result",
            "result_source",
        ):
            if key not in safe:
                continue
            candidate = dict(compact)
            candidate[key] = safe[key]
            if encoded_size(candidate) <= limit:
                compact = candidate
        return compact, "truncated"

    def _normalize_terminal_job(self, job: JobInfo) -> None:
        """Clear live-only activity fields once a job reaches a terminal state."""
        if job.current_command_preview:
            job.last_command_preview = job.current_command_preview
        job.current_phase = None
        job.current_item_type = None
        job.current_item_status = None
        job.current_command_preview = None
        job.current_command_started_at = None
    
    def get_job(self, job_id: str) -> Optional[JobInfo]:
        """Get job info by ID"""
        with self._state_lock:
            return self.jobs.get(job_id)
    
    def cleanup_job(self, job_id: str) -> bool:
        """
        Clean up job resources (worktree, logs).
        """
        with self._state_lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
                logger.warning(
                    "Refused cleanup of job %s while process ownership is unresolved",
                    job_id,
                )
                return False
            mode = job.mode
            worktree_path = job.worktree_path
            repo_path = job.repo_path
        
        # Remove worktree if it was created
        if mode == "apply" and worktree_path:
            try:
                worktree = Path(worktree_path)
                if worktree.exists():
                    repo = git.Repo(repo_path)
                    repo.git.worktree('remove', str(worktree), '--force')
                    logger.info(f"Removed worktree for job {job_id}")
            except Exception as e:
                logger.warning("Failed to remove worktree for job %s: %s", job_id, internal_log_error(e))
        
        # Remove from tracking and disk as one serialized state transition. A
        # concurrent update either persists before this deletion or observes an
        # unknown job; it cannot recreate the record after deletion.
        with self._state_lock:
            if self.jobs.get(job_id) is not job:
                return False
            del self.jobs[job_id]
            self._delete_job_record(job_id)
        logger.info(f"Cleaned up job {job_id}")
        return True
    
    def cleanup_old_jobs(self):
        """Remove jobs older than cleanup_after_hours"""
        cleanup_threshold = time.time() - (self.cleanup_after_hours * 3600)

        with self._state_lock:
            to_remove = []
            for job_id, job in self.jobs.items():
                # Workers are derived from worker-tagged durable job records.
                # Ordinary age cleanup must not delete their identity/session index.
                if (job.options or {}).get("_worker_id"):
                    continue
                if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
                    continue
                if job.completed_at and job.completed_at < cleanup_threshold:
                    to_remove.append(job_id)
        
        for job_id in to_remove:
            self.cleanup_job(job_id)
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old jobs")

    def mark_active_jobs_cancelled(self, reason: str):
        """Mark non-terminal jobs as cancelled without deleting their durable records."""
        with self._state_lock:
            for job_id, job in list(self.jobs.items()):
                if job.state in (JobState.PENDING, JobState.RUNNING):
                    self.update_job_state(job_id, JobState.CANCELLED, error=reason)

    def _job_record_path(self, job_id: str) -> Path:
        return self.job_state_dir / f"{job_id}.json"

    def _persist_job(self, job: JobInfo) -> None:
        """Atomically persist one state-locked JobInfo snapshot."""
        with self._state_lock:
            path = self._job_record_path(job.job_id)
            payload = json.dumps(job.to_persisted_dict(), indent=2, sort_keys=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=self.job_state_dir,
                text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
                self._fsync_job_state_dir()
            finally:
                Path(temporary_path).unlink(missing_ok=True)

    def _fsync_job_state_dir(self) -> None:
        """Best-effort directory sync after replacing a durable job record."""
        if os.name == "nt":
            return
        try:
            descriptor = os.open(self.job_state_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            logger.debug("Failed to sync job state directory: %s", internal_log_error(error))

    def _delete_job_record(self, job_id: str) -> None:
        with self._state_lock:
            try:
                self._job_record_path(job_id).unlink(missing_ok=True)
                self._fsync_job_state_dir()
            except Exception as error:
                logger.warning("Failed to delete job record %s: %s", job_id, internal_log_error(error))

    def _load_jobs(self) -> None:
        loaded = 0
        for path in sorted(self.job_state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = JobInfo.from_persisted_dict(data)
                self._recover_result_artifact(job)
                if job.state == JobState.PENDING:
                    job.state = JobState.FAILED
                    job.completed_at = time.time()
                    job.error = "Job did not finish before the server stopped."
                elif job.state == JobState.RUNNING:
                    job.progress = job.progress or "Recovered running job record after PatchBay restart; executor reconciliation will verify whether a live Codex runtime still exists."
                elif job.state == JobState.COMPLETED:
                    job.error = None
                if job.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
                    self._normalize_terminal_job(job)
                with self._state_lock:
                    self.jobs[job.job_id] = job
                    self._persist_job(job)
                loaded += 1
            except Exception as error:
                logger.warning("Failed to load job record %s: %s", path.name, internal_log_error(error))
        if loaded:
            logger.info("Loaded %d durable job record(s)", loaded)

    def _recover_result_artifact(self, job: JobInfo) -> None:
        """Recover a terminal result payload when the state file lost it."""
        if job.state not in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return
        if isinstance(job.result, dict):
            return
        result_path = self.job_logs_dir / f"{job.job_id}_result.json"
        if not result_path.exists():
            return
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as error:
            logger.warning("Failed to recover result artifact for job %s: %s", job.job_id, internal_log_error(error))
            return
        if isinstance(payload, dict):
            job.result = redact_sensitive_output(payload)
