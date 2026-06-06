"""
Job state management for Codex MCP server.
Handles job lifecycle, worktree management, and state tracking.
"""
import uuid
import time
import json
import logging
from typing import Dict, Optional, Any
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, asdict
from datetime import datetime
import git

from security import validate_allowed_path

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
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, handling enum serialization"""
        data = asdict(self)
        data['state'] = self.state.value
        return data


class JobManager:
    """
    Manages Codex job lifecycle and worktree isolation.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.jobs: Dict[str, JobInfo] = {}
        self.max_concurrent = config['server']['max_concurrent_jobs']
        self.job_timeout = config['server']['job_timeout_seconds']
        self.cleanup_after_hours = config['server'].get('job_cleanup_after_hours', 24)
        self.worktrees_dir = Path(config['repositories']['default']).resolve() / 'worktrees'
        self.worktrees_dir.mkdir(exist_ok=True)
        
        logger.info(f"JobManager initialized: max_concurrent={self.max_concurrent}, "
                   f"timeout={self.job_timeout}s, worktrees_dir={self.worktrees_dir}")
    
    def create_job(self, mode: str, prompt: str, repo_path: str, options: Optional[Dict] = None) -> str:
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
        if self.max_concurrent > 0:
            running_count = sum(1 for job in self.jobs.values() 
                              if job.state == JobState.RUNNING)
            if running_count >= self.max_concurrent:
                raise RuntimeError(f"Maximum concurrent jobs ({self.max_concurrent}) reached")
        
        
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
        
        # Create worktree for this job
        if mode == "apply":  # Only apply mode needs writable worktree
            try:
                worktree_path, branch_name = self._create_worktree(job_id, repo_path)
                job.worktree_path = str(worktree_path)
                job.branch_name = branch_name
                logger.info(f"Created worktree for job {job_id}: {worktree_path} (branch: {branch_name})")
            except Exception as e:
                logger.error(f"Failed to create worktree for job {job_id}: {e}")
                raise
        else:
            # Plan mode can use the main repo (read-only sandbox)
            job.worktree_path = repo_path
        
        self.jobs[job_id] = job
        logger.info(f"Created job {job_id}: mode={mode}, repo={repo_path}")
        
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
        repo = git.Repo(repo_path)
        
        # Generate unique branch name
        branch_name = f"codex/job-{job_id[:8]}"
        
        # Create worktree directory
        worktree_path = self.worktrees_dir / f"job-{job_id[:8]}"
        
        # Add worktree
        repo.git.worktree('add', str(worktree_path), '-b', branch_name)
        
        return worktree_path, branch_name
    
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
        
        # Update other fields
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        
        logger.debug(f"Job {job_id} state updated: {state}")
    
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
                logger.warning(f"Failed to remove worktree for job {job_id}: {e}")
        
        # Remove from tracking
        del self.jobs[job_id]
        logger.info(f"Cleaned up job {job_id}")
    
    def cleanup_old_jobs(self):
        """Remove jobs older than cleanup_after_hours"""
        cleanup_threshold = time.time() - (self.cleanup_after_hours * 3600)
        
        to_remove = []
        for job_id, job in self.jobs.items():
            if job.completed_at and job.completed_at < cleanup_threshold:
                to_remove.append(job_id)
        
        for job_id in to_remove:
            self.cleanup_job(job_id)
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old jobs")
