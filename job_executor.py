"""Job execution engine for running Codex CLI commands."""
import asyncio
import json
import logging
import subprocess
import re
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional

from job_manager import JobManager, JobState
from security import redact_sensitive_output, redact_text, validate_allowed_path

logger = logging.getLogger(__name__)

STALE_RUNNING_JOB_ERROR = (
    "Job was marked running, but no live Codex process is tracked. "
    "The wrapper marked it failed so it can be inspected or restarted."
)


class JobExecutor:
    """
    Executes Codex jobs with conservative defaults.
    """
    
    def __init__(self, config: Dict[str, Any], job_manager: JobManager):
        self.config = config
        self.job_manager = job_manager
        self.schema_path = Path(__file__).parent / 'codex_output_schema.json'
        self.job_logs_dir = Path(config['logging']['job_logs_dir'])
        self.job_logs_dir.mkdir(parents=True, exist_ok=True)
        self.processes: Dict[str, asyncio.subprocess.Process] = {}

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

        for job_id, job in list(self.job_manager.jobs.items()):
            if job.state != JobState.RUNNING:
                continue
            checked += 1
            if job_id in self.processes:
                continue
            if job.started_at is not None and current_time - float(job.started_at) < grace:
                continue

            self.job_manager.update_job_state(
                job_id,
                JobState.FAILED,
                error=STALE_RUNNING_JOB_ERROR,
            )
            reconciled.append(job_id)
            logger.warning("Reconciled stale running job %s with no tracked process", job_id)

        return {
            "checked": checked,
            "reconciled": len(reconciled),
            "job_ids": reconciled,
            "grace_seconds": grace,
        }

    def _stale_running_grace_seconds(self, override: Optional[float] = None) -> float:
        if override is not None:
            return max(0.0, float(override))
        try:
            configured = float(self.config.get("server", {}).get("stale_running_job_grace_seconds", 5))
        except (TypeError, ValueError):
            configured = 5.0
        return max(0.0, configured)
        
    async def execute_job(self, job_id: str):
        """Execute a Codex job asynchronously."""
        job = self.job_manager.get_job(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return
        if job.state == JobState.CANCELLED:
            logger.info(f"Job {job_id} was cancelled before execution started")
            return
        
        try:
            self.job_manager.update_job_state(job_id, JobState.RUNNING)
            
            # Build command and keep prompt text off argv when the Codex CLI supports stdin.
            cmd = self._build_codex_command(job.mode, job.prompt, job.worktree_path, job.options)
            stdin_data = self._stdin_for_command(job.prompt, cmd)
            if self._job_is_cancelled(job_id):
                logger.info(f"Job {job_id} was cancelled before process launch")
                return
            
            logger.info(f"Executing job {job_id}: {' '.join(cmd[:5])}...")
            
            # Log files
            stdout_log = self.job_logs_dir / f"{job_id}_stdout.log"
            stderr_log = self.job_logs_dir / f"{job_id}_stderr.log"
            result_file = self.job_logs_dir / f"{job_id}_result.json"
            
            timeout = self.config['server']['job_timeout_seconds']
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=job.worktree_path,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env()
            )
            self.processes[job_id] = process
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=stdin_data),
                    timeout=timeout
                )
                
                self._write_process_artifact(stdout_log, stdout)
                self._write_process_artifact(stderr_log, stderr)

                if self._job_is_cancelled(job_id):
                    logger.info(f"Job {job_id} process exited after cancellation")
                    return
                
                raw_stdout = stdout.decode('utf-8', errors='replace')
                
                result = await self._parse_result(stdout, result_file, job.options)
                
                # Extract session ID from JSON events (stdout) first, then fall back to stderr
                session_id = self._extract_session_id_from_json_events(raw_stdout)
                if not session_id:
                    session_id = self._extract_session_id(stderr.decode('utf-8'))
                
                result = redact_sensitive_output(result)
                
                if process.returncode == 0:
                    self.job_manager.update_job_state(
                        job_id,
                        JobState.COMPLETED,
                        result=result,
                        session_id=session_id,
                        exit_code=0
                    )
                    logger.info(f"Job {job_id} completed successfully")
                else:
                    error_msg = redact_text(stderr.decode('utf-8', errors='replace')[-2000:])
                    self.job_manager.update_job_state(
                        job_id,
                        JobState.FAILED,
                        error=error_msg,
                        exit_code=process.returncode
                    )
                    logger.error(f"Job {job_id} failed: exit code {process.returncode}")
                    
            except asyncio.TimeoutError:
                await self._terminate_process(job_id, process)
                self.job_manager.update_job_state(
                    job_id,
                    JobState.FAILED,
                    error=f"Job timed out after {timeout} seconds"
                )
                logger.error(f"Job {job_id} timed out")
            finally:
                self.processes.pop(job_id, None)
                
        except Exception as e:
            if self._job_is_cancelled(job_id):
                logger.info(f"Job {job_id} stopped after cancellation")
                return
            logger.exception(f"Job {job_id} execution failed: {e}")
            self.job_manager.update_job_state(
                job_id,
                JobState.FAILED,
                error=str(e)
            )
    
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
        allowed_set = set(allowed)
        if "*" in allowed_set:
            return dict(os.environ)
        return {k: v for k, v in os.environ.items() if k in allowed_set}

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
    
    async def _parse_result(self, stdout: bytes, result_file: Path, options: Dict[str, Any] = None) -> Dict[str, Any]:
        """Parse result from Codex output."""
        if options is None:
            options = {}
            
        try:
            stdout_text = stdout.decode('utf-8').strip()
            
            # If structured output was disabled, return raw
            if not options.get('structured_output', True):
                return {
                    "summary": redact_text(stdout_text),
                    "raw_output": True,
                    "files_changed": []
                }
            
            # Parse JSONL - look for structured result
            lines = [line for line in stdout_text.split('\n') if line.strip()]
            
            if not lines:
                return {
                    "summary": "No output received",
                    "files_changed": []
                }
            
            # Try to find the structured result in JSON events
            result = None
            for line in reversed(lines):
                try:
                    parsed = json.loads(line)
                    # Look for result event or last valid JSON
                    if isinstance(parsed, dict):
                        if parsed.get('type') == 'result' and 'data' in parsed:
                            result = parsed['data']
                            break
                        elif parsed.get('type') == 'item.completed':
                            item = parsed.get('item') or {}
                            if isinstance(item, dict) and item.get('type') == 'agent_message':
                                text = item.get('text')
                                if isinstance(text, str) and text.strip():
                                    try:
                                        message_result = json.loads(text)
                                        if isinstance(message_result, dict):
                                            result = message_result
                                        else:
                                            result = {"summary": str(message_result), "files_changed": []}
                                    except json.JSONDecodeError:
                                        result = {"summary": text, "files_changed": []}
                                    break
                        elif 'summary' in parsed:
                            result = parsed
                            break
                except json.JSONDecodeError:
                    continue
            
            if result:
                safe_result = redact_sensitive_output(result)
                result_file.write_text(json.dumps(safe_result, indent=2))
                return safe_result
            
            # Fallback: return raw summary
            return {
                "summary": redact_text(stdout_text[:2000]),
                "files_changed": [],
                "notes": "Could not extract structured result"
            }
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Codex result: {e}")
            return {
                "summary": redact_text(stdout.decode('utf-8', errors='replace')[:2000]),
                "files_changed": [],
                "notes": f"Could not parse as JSON: {str(e)}"
            }
    
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
        if process and process.returncode is None:
            process_signalled = await self._terminate_process(job_id, process)

        self.job_manager.update_job_state(job_id, JobState.CANCELLED, error=reason)
        logger.info(f"Job {job_id} cancelled")
        return {
            "cancelled": True,
            "job_id": job_id,
            "state": JobState.CANCELLED.value,
            "process_signalled": process_signalled,
        }

    async def cancel_all_running(self, reason: str = "Server shutting down") -> None:
        """Cancel every tracked subprocess and mark queued/running jobs terminal."""
        for job_id in list(self.processes.keys()):
            await self.cancel_job(job_id, reason=reason)
        self.job_manager.mark_active_jobs_cancelled(reason)

    async def _terminate_process(self, job_id: str, process: asyncio.subprocess.Process) -> bool:
        if process.returncode is not None:
            return False
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(f"Job {job_id} did not terminate gracefully; killing")
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
