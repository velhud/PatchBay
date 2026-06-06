"""Job execution engine for running Codex CLI commands."""
import asyncio
import json
import logging
import subprocess
import re
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

from job_manager import JobManager, JobState

logger = logging.getLogger(__name__)


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
        
    async def execute_job(self, job_id: str):
        """Execute a Codex job asynchronously."""
        job = self.job_manager.get_job(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return
        
        try:
            self.job_manager.update_job_state(job_id, JobState.RUNNING)
            
            # Build command
            cmd = self._build_codex_command(job.mode, job.prompt, job.worktree_path, job.options)
            
            logger.info(f"Executing job {job_id}: {' '.join(cmd[:5])}...")
            
            # Log files
            stdout_log = self.job_logs_dir / f"{job_id}_stdout.log"
            stderr_log = self.job_logs_dir / f"{job_id}_stderr.log"
            result_file = self.job_logs_dir / f"{job_id}_result.json"
            
            timeout = self.config['server']['job_timeout_seconds']
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=job.worktree_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env()
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
                
                stdout_log.write_bytes(stdout)
                stderr_log.write_bytes(stderr)
                
                # Store raw stdout for fallback parsing
                raw_stdout = stdout.decode('utf-8')
                
                result = await self._parse_result(stdout, result_file, job.options)
                
                # Extract session ID from JSON events (stdout) first, then fall back to stderr
                session_id = self._extract_session_id_from_json_events(raw_stdout)
                if not session_id:
                    session_id = self._extract_session_id(stderr.decode('utf-8'))
                
                # Store raw output in result for fallback access
                result['_raw_stdout'] = raw_stdout
                
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
                    error_msg = stderr.decode('utf-8')[-2000:]
                    self.job_manager.update_job_state(
                        job_id,
                        JobState.FAILED,
                        error=error_msg,
                        exit_code=process.returncode
                    )
                    logger.error(f"Job {job_id} failed: exit code {process.returncode}")
                    
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                self.job_manager.update_job_state(
                    job_id,
                    JobState.FAILED,
                    error=f"Job timed out after {timeout} seconds"
                )
                logger.error(f"Job {job_id} timed out")
                
        except Exception as e:
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
        
        security = self.config.get('security', {})
        sandbox = options.get('sandbox') or security.get('default_sandbox', 'read-only')
        
        cmd = ['codex', 'exec', prompt]
        
        if options.get('dangerously_bypass'):
            if not security.get('allow_dangerously_bypass', False):
                raise PermissionError("dangerously_bypass is disabled by config.yaml")
            cmd.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            # Only add sandbox if not bypassing
            cmd.extend(['--sandbox', sandbox])
            
            if options.get('full_auto', False):
                cmd.append('--full-auto')
        
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
        
        # Feature flags - default disable skills to avoid malformed SKILL.md issues
        features = options.get('features', {})
        disable = set(features.get('disable', []))
        enable = set(features.get('enable', []))
        
        # Default: disable skills unless explicitly enabled (avoids SKILL.md parsing errors)
        if 'skills' not in enable:
            disable.add('skills')
        
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
                cmd.extend(['--add-dir', add_dir])
        
        # Config overrides via -c flag
        if 'config_overrides' in options:
            for override in options['config_overrides']:
                cmd.extend(['-c', override])
        
        return cmd
    
    def _build_env(self) -> Dict[str, str]:
        """Build environment variables for Codex execution"""
        import os
        return os.environ.copy()
    
    async def _parse_result(self, stdout: bytes, result_file: Path, options: Dict[str, Any] = None) -> Dict[str, Any]:
        """Parse result from Codex output."""
        if options is None:
            options = {}
            
        try:
            stdout_text = stdout.decode('utf-8').strip()
            
            # If structured output was disabled, return raw
            if not options.get('structured_output', True):
                return {
                    "summary": stdout_text,
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
                        elif 'summary' in parsed:
                            result = parsed
                            break
                except json.JSONDecodeError:
                    continue
            
            if result:
                result_file.write_text(json.dumps(result, indent=2))
                return result
            
            # Fallback: return raw summary
            return {
                "summary": stdout_text[:2000],
                "files_changed": [],
                "notes": "Could not extract structured result"
            }
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Codex result: {e}")
            return {
                "summary": stdout.decode('utf-8')[:2000],
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
    
    async def cancel_job(self, job_id: str):
        """Cancel a running job."""
        job = self.job_manager.get_job(job_id)
        if not job or job.state != JobState.RUNNING:
            logger.warning(f"Cannot cancel job {job_id}: not running")
            return
        
        self.job_manager.update_job_state(job_id, JobState.CANCELLED)
        logger.info(f"Job {job_id} marked as cancelled")
    
    def get_diff(self, job_id: str, file_path: str) -> Optional[str]:
        """Get unified diff for a file in a job's worktree."""
        job = self.job_manager.get_job(job_id)
        if not job or not job.worktree_path:
            return None
        
        try:
            # First try git diff with proper -- separator
            result = subprocess.run(
                ['git', 'diff', 'HEAD', '--', file_path],
                cwd=job.worktree_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
            
            # Fallback: try to extract diff from stored job output
            if job.result and '_raw_stdout' in job.result:
                raw = job.result['_raw_stdout']
                # Look for diff-like content mentioning this file
                if file_path in raw:
                    # Try to extract a diff block
                    lines = raw.split('\n')
                    diff_lines = []
                    in_diff = False
                    for line in lines:
                        if line.startswith('diff --git') and file_path in line:
                            in_diff = True
                        if in_diff:
                            diff_lines.append(line)
                            if line.startswith('diff --git') and file_path not in line:
                                break
                    if diff_lines:
                        return '\n'.join(diff_lines)
            
            # If file is new/untracked, show its content as a diff
            file_full_path = Path(job.worktree_path) / file_path
            if file_full_path.exists():
                content = file_full_path.read_text()
                return f"--- /dev/null\n+++ b/{file_path}\n" + '\n'.join(
                    f"+{line}" for line in content.split('\n')
                )
            
            # FIX: Only log if stderr has actual content (not just empty diff for nonexistent file)
            if result.stderr and result.stderr.strip():
                logger.debug(f"git diff returned no output for {file_path}: {result.stderr}")
            return None
                
        except Exception as e:
            logger.debug(f"Failed to get diff for {file_path}: {e}")
            return None
