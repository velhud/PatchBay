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
from typing import Dict, Optional, Any
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, asdict
import git

from patchbay.connector.profiles import normalize_logging_paths
from patchbay.security import internal_log_error, redact_sensitive_output, validate_allowed_path

logger = logging.getLogger(__name__)


class JobState(str, Enum):
    """Job execution states"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    last_heartbeat_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    
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
        self.max_concurrent = config['server']['max_concurrent_jobs']
        self.queue_enabled = bool(config.get("server", {}).get("queue_enabled", False))
        self.job_timeout = config['server']['job_timeout_seconds']
        self.cleanup_after_hours = config['server'].get('job_cleanup_after_hours', 24)
        logging_config = config.get('logging', {})
        self.worktrees_dir = Path(logging_config['worktrees_dir']).expanduser().resolve()
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.job_state_dir = Path(logging_config['job_state_dir']).expanduser().resolve()
        self.job_state_dir.mkdir(parents=True, exist_ok=True)
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
        if job_id not in self.jobs:
            raise ValueError(f"Unknown job: {job_id}")
        self.jobs[job_id].options = dict(options)
        self._persist_job(self.jobs[job_id])

    def _validate_worker_worktree_path(self, worktree_path: str) -> Path:
        path = Path(worktree_path).expanduser().resolve()
        root = self.worker_worktrees_dir()
        if path != root and root not in path.parents:
            raise ValueError(f"Worker worktree path is outside worker root: {worktree_path}")
        return path
    
    def update_job_state(self, job_id: str, state: JobState, **kwargs):
        """Update job state and metadata"""
        if job_id not in self.jobs:
            raise ValueError(f"Unknown job: {job_id}")
        
        job = self.jobs[job_id]
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
        
        logger.debug(f"Job {job_id} state updated: {state}")
        self._persist_job(job)
    
    def get_job(self, job_id: str) -> Optional[JobInfo]:
        """Get job info by ID"""
        return self.jobs.get(job_id)
    
    def cleanup_job(self, job_id: str):
        """
        Clean up job resources (worktree, logs).
        """
        job = self.get_job(job_id)
        if not job:
            return
        
        # Remove worktree if it was created
        if job.mode == "apply" and job.worktree_path:
            try:
                worktree_path = Path(job.worktree_path)
                if worktree_path.exists():
                    repo = git.Repo(job.repo_path)
                    repo.git.worktree('remove', str(worktree_path), '--force')
                    logger.info(f"Removed worktree for job {job_id}")
            except Exception as e:
                logger.warning("Failed to remove worktree for job %s: %s", job_id, internal_log_error(e))
        
        # Remove from tracking
        del self.jobs[job_id]
        self._delete_job_record(job_id)
        logger.info(f"Cleaned up job {job_id}")
    
    def cleanup_old_jobs(self):
        """Remove jobs older than cleanup_after_hours"""
        cleanup_threshold = time.time() - (self.cleanup_after_hours * 3600)
        
        to_remove = []
        for job_id, job in self.jobs.items():
            # Workers are derived from worker-tagged durable job records.
            # Ordinary age cleanup must not delete their identity/session index.
            if (job.options or {}).get("_worker_id"):
                continue
            if job.completed_at and job.completed_at < cleanup_threshold:
                to_remove.append(job_id)
        
        for job_id in to_remove:
            self.cleanup_job(job_id)
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old jobs")

    def mark_active_jobs_cancelled(self, reason: str):
        """Mark non-terminal jobs as cancelled without deleting their durable records."""
        for job_id, job in list(self.jobs.items()):
            if job.state in (JobState.PENDING, JobState.RUNNING):
                self.update_job_state(job_id, JobState.CANCELLED, error=reason)

    def _job_record_path(self, job_id: str) -> Path:
        return self.job_state_dir / f"{job_id}.json"

    def _persist_job(self, job: JobInfo) -> None:
        path = self._job_record_path(job.job_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(job.to_persisted_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _delete_job_record(self, job_id: str) -> None:
        try:
            self._job_record_path(job_id).unlink(missing_ok=True)
        except Exception as error:
            logger.warning("Failed to delete job record %s: %s", job_id, internal_log_error(error))

    def _load_jobs(self) -> None:
        loaded = 0
        for path in sorted(self.job_state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = JobInfo.from_persisted_dict(data)
                if job.state in (JobState.PENDING, JobState.RUNNING):
                    job.state = JobState.FAILED
                    job.completed_at = time.time()
                    job.error = "Job did not finish before the server stopped."
                elif job.state == JobState.COMPLETED:
                    job.error = None
                self.jobs[job.job_id] = job
                self._persist_job(job)
                loaded += 1
            except Exception as error:
                logger.warning("Failed to load job record %s: %s", path.name, internal_log_error(error))
        if loaded:
            logger.info("Loaded %d durable job record(s)", loaded)
