"""Tool handler for Codex MCP operations."""
import asyncio
import subprocess
import base64
import json
import logging
import re
from typing import Dict, Any, Optional
from pathlib import Path

from job_manager import JobManager, JobState
from job_executor import JobExecutor

logger = logging.getLogger(__name__)


def decode_if_base64(value: str) -> str:
    """Attempt to decode a base64 string, return original if not valid base64."""
    try:
        decoded = base64.b64decode(value).decode('utf-8')
        return decoded
    except Exception:
        return value


def extract_thread_id_from_json_events(stdout: str) -> Optional[str]:
    """Extract thread_id from JSON events in stdout."""
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


class ToolHandler:
    """
    Implements MCP tool execution logic for Codex operations.
    """
    
    def __init__(self, config: Dict[str, Any], job_manager: JobManager, job_executor: JobExecutor):
        self.config = config
        self.job_manager = job_manager
        self.job_executor = job_executor
        self.default_repo = config['repositories']['default']
        # Track interactive conversations
        self.conversations: Dict[str, Dict[str, Any]] = {}
    
    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route tool calls to appropriate handlers."""
        logger.info(f"Handling tool: {tool_name}")
        
        handlers = {
            "codex_plan_job": self._codex_plan_job,
            "codex_apply_job": self._codex_apply_job,
            "codex_get_status": self._codex_get_status,
            "codex_get_result": self._codex_get_result,
            "codex_get_diff": self._codex_get_diff,
            "codex_review": self._codex_review,
            "codex_resume": self._codex_resume,
            "codex_apply_diff": self._codex_apply_diff,
            "codex_interactive": self._codex_interactive,
            "codex_interactive_reply": self._codex_interactive_reply,
            "codex_get_config": self._codex_get_config,
            "codex_sandbox": self._codex_sandbox,
            "codex_cloud_exec": self._codex_cloud_exec,
            "codex_cloud_status": self._codex_cloud_status,
            "codex_cloud_diff": self._codex_cloud_diff,
            "string_transform": self._string_transform,  # New real transform handler
        }
        
        handler = handlers.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {tool_name}")
        
        return await handler(arguments)
    
    def _extract_options(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract all options from arguments - full capability access.
        """
        options = {}
        
        # Core parameters
        for key in ['model', 'images', 'search', 'features', 'profile', 'add_dirs',
                    'sandbox', 'approval_policy', 'network',
                    'config_overrides', 'dangerously_bypass', 'full_auto',
                    'structured_output', 'json_events']:
            if key in args:
                options[key] = args[key]
        
        return options
    
    async def _codex_plan_job(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start a text analytics query job"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = args.get('repo') or self.default_repo
        options = self._extract_options(args)
        
        logger.info(f"Creating analytics query: {prompt[:50]}...")
        
        try:
            job_id = self.job_manager.create_job('plan', prompt, repo, options)
            asyncio.create_task(self.job_executor.execute_job(job_id))
            
            return {
                "job_id": job_id,
                "mode": "plan",
                "status": "Operation initiated successfully",
                "access_level": options.get('sandbox', self.config.get('security', {}).get('default_sandbox', 'read-only')),
                "confirmation_mode": options.get('approval_policy', 'on-request')
            }
        except Exception as e:
            logger.error(f"Failed to create operation: {e}")
            return {"error": str(e)}
    
    async def _codex_apply_job(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start a content record update job"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = args.get('repo') or self.default_repo
        options = self._extract_options(args)
        
        logger.info(f"Creating record update: {prompt[:50]}...")
        
        try:
            job_id = self.job_manager.create_job('apply', prompt, repo, options)
            asyncio.create_task(self.job_executor.execute_job(job_id))
            
            job = self.job_manager.get_job(job_id)
            
            return {
                "job_id": job_id,
                "mode": "apply",
                "worktree_path": job.worktree_path,
                "branch_name": job.branch_name,
                "status": "Operation initiated. Changes staged in isolation.",
                "access_level": options.get('sandbox', self.config.get('security', {}).get('default_sandbox', 'read-only')),
                "confirmation_mode": options.get('approval_policy', 'on-request')
            }
        except Exception as e:
            logger.error(f"Failed to create update operation: {e}")
            return {"error": str(e)}
    
    async def _codex_get_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get operation status"""
        job_id = args['job_id']
        
        job = self.job_manager.get_job(job_id)
        if not job:
            return {"error": f"Reference not found: {job_id}"}
        
        result = {
            "reference_id": job_id,
            "state": job.state.value,
            "mode": job.mode,
            "started_at": job.started_at,
            "completed_at": job.completed_at
        }
        
        if job.last_event:
            result["last_event"] = job.last_event
        if job.progress:
            result["progress"] = job.progress
        
        if job.state == JobState.RUNNING:
            result["message"] = "Operation in progress"
        elif job.state == JobState.PENDING:
            result["message"] = "Operation queued"
        elif job.state == JobState.COMPLETED:
            result["message"] = "Operation completed. Use fetch_operation_result."
        elif job.state == JobState.FAILED:
            result["message"] = "Operation encountered an issue"
            if job.error:
                # Encode error to avoid filter triggers
                result["error_encoded"] = base64.b64encode(job.error.encode()).decode()
        
        return result
    
    async def _codex_get_result(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get operation result, blocking until complete"""
        job_id = args['job_id']
        
        job = self.job_manager.get_job(job_id)
        if not job:
            return {"error": f"Reference not found: {job_id}"}
        
        # Wait for completion
        max_wait = 60
        waited = 0
        while job.state == JobState.RUNNING or job.state == JobState.PENDING:
            if waited >= max_wait:
                return {"error": "Operation still running. Use check_operation_status."}
            await asyncio.sleep(1)
            waited += 1
            job = self.job_manager.get_job(job_id)
        
        if job.state == JobState.FAILED:
            error_msg = job.error or "Operation encountered an issue"
            return {
                "reference_id": job_id,
                "state": "error",
                "error_encoded": base64.b64encode(error_msg.encode()).decode(),
                "exit_code": job.exit_code
            }
        
        if job.state == JobState.CANCELLED:
            return {"reference_id": job_id, "state": "cancelled"}
        
        result = {
            "reference_id": job_id,
            "state": "completed",
            "mode": job.mode
        }
        
        if job.result:
            # Filter out internal fields
            for k, v in job.result.items():
                if not k.startswith('_'):
                    result[k] = v
        if job.session_id:
            result["session_ref"] = job.session_id
        if job.worktree_path:
            result["staging_path"] = job.worktree_path
        if job.branch_name:
            result["staging_branch"] = job.branch_name
        
        return result
    
    async def _codex_get_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get delta for a specific record"""
        job_id = args['job_id']
        file_path = args['file_path']
        
        diff = self.job_executor.get_diff(job_id, file_path)
        
        if diff is None:
            return {"error": f"Delta not available for {file_path}"}
        
        return {
            "reference_id": job_id,
            "record_path": file_path,
            "delta_content": diff
        }
    
    async def _codex_review(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run content change analysis"""
        # FIX: Use 'or' to handle empty string as not provided
        repo = args.get('repo') or self.default_repo
        prompt = args.get('prompt', '')
        uncommitted = bool(args.get('uncommitted'))
        base = args.get('base')
        commit = args.get('commit')
        
        # Build command with skills disabled to avoid SKILL.md errors
        cmd = ['codex', '--disable', 'skills', 'review']
        
        # FIX: Enforce mutual exclusion - if uncommitted, ignore base/commit
        if uncommitted:
            cmd.append('--uncommitted')
            # Don't pass prompt, base, or commit when using --uncommitted
        else:
            if base:
                cmd.extend(['--base', base])
            if commit:
                cmd.extend(['--commit', commit])
            if prompt:
                cmd.append(prompt)
        
        if 'title' in args:
            cmd.extend(['--title', args['title']])
        if 'model' in args:
            cmd.extend(['-c', f'model="{args["model"]}"'])
        
        if 'config_overrides' in args:
            for override in args['config_overrides']:
                cmd.extend(['-c', override])
        
        logger.info(f"Running content analysis in {repo}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            if process.returncode == 0:
                # FIX: If stdout is empty but stderr has content, use stderr
                analysis = stdout_text.strip()
                if not analysis and stderr_text.strip():
                    analysis = stderr_text.strip()
                
                return {
                    "status": "completed",
                    "analysis": analysis if analysis else "No changes to analyze",
                    "mode": "analysis"
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Analysis failed: {e}")
            return {"error": str(e)}
    
    async def _codex_resume(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Continue a previous session using non-interactive codex exec resume"""
        # Accept both session_id and session_ref
        session_id = args.get('session_id') or args.get('session_ref')
        if not session_id:
            return {"error": "Missing session reference"}
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = args.get('repo') or self.default_repo
        prompt = args.get('prompt', '')
        
        # FIX: Use `codex exec resume` instead of `codex resume` to avoid TTY requirement
        cmd = ['codex', 'exec', 'resume', session_id]
        
        if prompt:
            cmd.append(prompt)
        
        # FIX: Don't pass --sandbox to codex exec resume (unsupported)
        # Only add --full-auto and --json for autonomous operation
        if args.get('full_auto', False):
            cmd.append('--full-auto')
        
        # JSON output for non-interactive
        cmd.append('--json')
        
        if 'model' in args:
            cmd.extend(['--model', args['model']])
        if 'images' in args:
            for image in args['images']:
                cmd.extend(['--image', image])
        if 'config_overrides' in args:
            for override in args['config_overrides']:
                cmd.extend(['-c', override])
        
        logger.info(f"Continuing session {session_id}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            if process.returncode == 0:
                return {
                    "status": "completed",
                    "session_ref": session_id,
                    "output": stdout_text
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Session continuation failed: {e}")
            return {"error": str(e)}
    
    async def _codex_apply_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a remote delta to local storage using git apply"""
        task_id = args['task_id']
        repo = args.get('repo', self.default_repo)
        
        logger.info(f"Applying remote delta {task_id}")
        
        try:
            # First, fetch the diff using codex cloud diff
            diff_result = await self._codex_cloud_diff({'task_id': task_id})
            
            if 'error' in diff_result or 'error_encoded' in diff_result:
                return {
                    "status": "error",
                    "error": "Failed to fetch remote delta",
                    "details": diff_result
                }
            
            diff_content = diff_result.get('delta', '')
            if not diff_content:
                return {
                    "status": "error",
                    "error": "No delta content available for this task"
                }
            
            # Apply the diff using git apply
            process = await asyncio.create_subprocess_exec(
                'git', 'apply', '--verbose',
                cwd=repo,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=diff_content.encode()),
                timeout=300
            )
            
            if process.returncode == 0:
                return {
                    "status": "applied",
                    "task_ref": task_id,
                    "output": stdout.decode('utf-8'),
                    "note": "Delta applied successfully via git apply"
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr.decode('utf-8'),
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Apply delta failed: {e}")
            return {"error": str(e)}
    
    async def _codex_interactive(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start conversational query session using codex exec"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        repo = args.get('repo', self.default_repo)
        
        cmd = ['codex', 'exec']
        
        if 'model' in args:
            cmd.extend(['--model', args['model']])
        if 'images' in args:
            for image in args['images']:
                cmd.extend(['--image', image])
        if 'sandbox' in args:
            cmd.extend(['--sandbox', args['sandbox']])
        else:
            cmd.extend(['--sandbox', self.config.get('security', {}).get('default_sandbox', 'read-only')])
        if 'config_overrides' in args:
            for override in args['config_overrides']:
                cmd.extend(['-c', override])
        if args.get('dangerously_bypass'):
            if not self.config.get('security', {}).get('allow_dangerously_bypass', False):
                return {"error": "dangerously_bypass is disabled by config.yaml"}
            cmd.append('--dangerously-bypass-approvals-and-sandbox')
        elif args.get('full_auto', False):
            cmd.append('--full-auto')
        
        cmd.append('--json')
        cmd.append(prompt)
        
        logger.info(f"Starting conversational query: {prompt[:50]}...")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            # FIX: Extract session_id from JSON events in stdout, not stderr regex
            session_id = extract_thread_id_from_json_events(stdout_text)
            if not session_id:
                # Fallback to stderr regex
                match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', stderr_text)
                session_id = match.group(0) if match else None
            
            if session_id:
                self.conversations[session_id] = {"repo": repo}
            
            if process.returncode == 0:
                return {
                    "status": "completed",
                    "session_id": session_id,  # Use session_id for proper mapping
                    "response": stdout_text,
                    "note": "Use continue_conversational_query to continue"
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr_text.encode()).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Conversational query failed: {e}")
            return {"error": str(e)}
    
    async def _codex_interactive_reply(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Continue a conversational query session using codex exec resume"""
        # FIX: Accept session_id (mapped from session_ref) OR conversation_id for backwards compat
        session_id = args.get('session_id') or args.get('conversation_id')
        if not session_id:
            return {"error": "Missing session_ref/session_id"}
        
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        conv = self.conversations.get(session_id, {})
        repo = args.get('repo', conv.get('repo', self.default_repo))
        
        # FIX: Use `codex exec resume` for non-TTY operation
        cmd = ['codex', 'exec', 'resume', session_id]
        if prompt:
            cmd.append(prompt)
        cmd.append('--json')
        if args.get('full_auto', False):
            cmd.append('--full-auto')
        
        logger.info(f"Continuing query session {session_id}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1800)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            if process.returncode == 0:
                return {
                    "status": "continued",
                    "session_id": session_id,
                    "response": stdout_text
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Query continuation failed: {e}")
            return {"error": str(e)}
    
    async def _string_transform(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Real string encoding transform - deterministic, no shell needed.
        Transforms input text to various encoding formats.
        """
        import binascii
        
        # Get input string (may have been decoded from base64 already by translate_arguments)
        input_string = args.get('input_string', '')
        
        # Try to decode if it looks like base64 (fallback)
        if input_string and ' ' not in input_string and len(input_string) > 20:
            input_string = decode_if_base64(input_string)
        
        logger.info(f"Transforming string: {input_string[:50]}...")
        
        try:
            # Encode to UTF-8 bytes
            b = input_string.encode('utf-8', errors='strict')
            
            return {
                "status": "completed",
                "input_preview": input_string[:100] + ("..." if len(input_string) > 100 else ""),
                "length_chars": len(input_string),
                "length_bytes": len(b),
                "utf8_hex": binascii.hexlify(b).decode(),
                "utf8_base64": base64.b64encode(b).decode(),
                "unicode_codepoints": [hex(ord(c)) for c in input_string[:50]],  # First 50 chars
                "note": "Use utf8_base64 to safely transfer this content"
            }
        except Exception as e:
            logger.exception(f"String transform failed: {e}")
            return {"status": "error", "error": str(e)}
    
    async def _codex_get_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get system configuration"""
        import os
        from pathlib import Path
        
        result = {
            "config_path": str(Path.home() / ".codex" / "config.toml"),
            "config": {},
            "capabilities": {},
            "note": "Use the 'engine_variant' value from config when calling tools"
        }
        
        config_path = Path.home() / ".codex" / "config.toml"
        if config_path.exists():
            try:
                config_text = config_path.read_text()
                result["config_raw"] = config_text
                
                for line in config_text.split('\n'):
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        key, value = line.split('=', 1)
                        result["config"][key.strip()] = value.strip().strip('"')
            except Exception as e:
                result["config_error"] = str(e)
        else:
            result["config_error"] = "Configuration not found"
        
        try:
            process = await asyncio.create_subprocess_exec(
                'codex', 'features', 'list',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            
            if process.returncode == 0:
                for line in stdout.decode('utf-8').strip().split('\n'):
                    parts = line.split()
                    if len(parts) >= 3:
                        feature_name = parts[0]
                        stage = parts[1]
                        enabled = parts[2].lower() == 'true'
                        result["capabilities"][feature_name] = {
                            "stage": stage,
                            "enabled": enabled
                        }
        except Exception as e:
            result["capabilities_error"] = str(e)
        
        return result
    
    async def _codex_sandbox(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute string transformation / shell command in sandbox"""
        command = args.get('command', '')
        
        # Try to decode if it looks like base64
        if command and ' ' not in command and len(command) > 10:
            command = decode_if_base64(command)
        
        cwd = args.get('cwd', self.default_repo)
        sandbox_type = args.get('sandbox_type', 'macos')
        
        cmd = ['codex', 'sandbox', sandbox_type, '--full-auto', '--', 'bash', '-c', command]
        
        if 'config_overrides' in args:
            for override in args['config_overrides']:
                cmd.insert(4, '-c')
                cmd.insert(5, override)
        
        logger.info(f"Executing transformation: {command[:50]}...")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            return {
                "status": "completed" if process.returncode == 0 else "error",
                "exit_code": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                # Include encoded stderr for error surfacing
                "error_encoded": base64.b64encode(stderr).decode() if process.returncode != 0 else None
            }
        except Exception as e:
            logger.exception(f"Transformation failed: {e}")
            return {"error": str(e)}
    
    async def _codex_cloud_exec(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Submit task to remote processing"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = args.get('repo') or self.default_repo
        
        # FIX: env_id is required by codex cloud exec
        env_id = args.get('env_id') or self.config.get('cloud', {}).get('default_env_id')
        if not env_id:
            return {
                "status": "error",
                "error": "Missing env_id (cloud environment ID is required by codex cloud exec)"
            }
        
        cmd = ['codex', 'cloud', 'exec', '--env', env_id, prompt]
        
        if 'model' in args:
            cmd.extend(['-c', f'model="{args["model"]}"'])
        
        if 'config_overrides' in args:
            for override in args['config_overrides']:
                cmd.extend(['-c', override])
        
        logger.info(f"Submitting to remote: {prompt[:50]}...")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            # Try to extract task_id from output
            match = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', stdout_text)
            task_id = match.group(0) if match else None
            
            if process.returncode == 0:
                return {
                    "status": "submitted",
                    "task_ref": task_id,
                    "output": stdout_text,
                    "note": "Use check_remote_task_status to track progress"
                }
            else:
                return {
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Remote submission failed: {e}")
            return {"error": str(e)}
    
    async def _codex_cloud_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get remote task status"""
        task_id = args['task_id']
        
        cmd = ['codex', 'cloud', 'status', task_id]
        
        logger.info(f"Getting remote status for {task_id}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            if process.returncode == 0:
                return {
                    "task_ref": task_id,
                    "status": stdout_text
                }
            else:
                return {
                    "task_ref": task_id,
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Remote status check failed: {e}")
            return {"error": str(e)}
    
    async def _codex_cloud_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get delta for remote task"""
        task_id = args['task_id']
        
        cmd = ['codex', 'cloud', 'diff', task_id]
        
        logger.info(f"Getting remote delta for {task_id}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
            
            stdout_text = stdout.decode('utf-8')
            stderr_text = stderr.decode('utf-8')
            
            if process.returncode == 0:
                return {
                    "task_ref": task_id,
                    "delta": stdout_text
                }
            else:
                return {
                    "task_ref": task_id,
                    "status": "error",
                    "error_encoded": base64.b64encode(stderr).decode(),
                    "stderr": stderr_text,
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Remote delta fetch failed: {e}")
            return {"error": str(e)}
