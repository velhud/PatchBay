"""Tool handler for Codex MCP operations."""
import asyncio
import base64
import hashlib
import json
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from auth import auth_public_metadata, build_auth_policy
from codex_sessions import CodexSessionReader
from codex_model_options import worker_option_menu
from connector import connector_status
from job_manager import JobManager, JobState
from job_executor import JobExecutor
from power_tools import PowerToolRunner
from security import redact_sensitive_output, validate_allowed_path
from workspace_context import WorkspaceContext
from worker_runtime import WorkerRuntime

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
        self.workspace_context = WorkspaceContext(config)
        self.power_tools = PowerToolRunner(config, self.workspace_context)
        self.codex_sessions = CodexSessionReader(config)
        self.worker_runtime = WorkerRuntime(config, job_manager, job_executor)
        # Track interactive conversations
        self.conversations: Dict[str, Dict[str, Any]] = {}
    
    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route tool calls to appropriate handlers."""
        logger.info(f"Handling tool: {tool_name}")
        self._reconcile_active_jobs()
        
        handlers = {
            "codex_open_workspace": self._codex_open_workspace,
            "codex_repo_tree": self._codex_repo_tree,
            "codex_read_file": self._codex_read_file,
            "codex_search_repo": self._codex_search_repo,
            "codex_load_context": self._codex_load_context,
            "codex_export_context": self._codex_export_context,
            "codex_list_skills": self._codex_list_skills,
            "codex_load_skill": self._codex_load_skill,
            "codex_write_handoff": self._codex_write_handoff,
            "codex_get_handoff_status": self._codex_get_handoff_status,
            "codex_get_handoff_diff": self._codex_get_handoff_diff,
            "codex_list_workspaces": self._codex_list_workspaces,
            "codex_workspace_snapshot": self._codex_workspace_snapshot,
            "codex_inventory": self._codex_inventory,
            "codex_git_status": self._codex_git_status,
            "codex_git_diff": self._codex_git_diff,
            "codex_show_changes": self._codex_show_changes,
            "codex_write_file": self._codex_write_file,
            "codex_edit_file": self._codex_edit_file,
            "codex_run_command": self._codex_run_command,
            "codex_plan_job": self._codex_plan_job,
            "codex_apply_job": self._codex_apply_job,
            "codex_get_status": self._codex_get_status,
            "codex_get_result": self._codex_get_result,
            "codex_get_diff": self._codex_get_diff,
            "codex_cancel_job": self._codex_cancel_job,
            "codex_review": self._codex_review,
            "codex_list_sessions": self._codex_list_sessions,
            "codex_read_session": self._codex_read_session,
            "codex_resume": self._codex_resume,
            "codex_interactive": self._codex_interactive,
            "codex_interactive_reply": self._codex_interactive_reply,
            "codex_worker_options": self._codex_worker_options,
            "codex_worker_start": self._codex_worker_start,
            "codex_worker_message": self._codex_worker_message,
            "codex_worker_list": self._codex_worker_list,
            "codex_worker_inspect": self._codex_worker_inspect,
            "codex_worker_integrate": self._codex_worker_integrate,
            "codex_worker_stop": self._codex_worker_stop,
            "codex_self_test": self._codex_self_test,
            "codex_get_config": self._codex_get_config,
        }
        handler = handlers.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {tool_name}")
        
        return await handler(arguments)

    def _reconcile_active_jobs(self) -> None:
        try:
            self.job_executor.reconcile_stale_running_jobs()
        except Exception as error:
            logger.warning("Failed to reconcile active jobs before tool call: %s", error)

    async def _codex_self_test(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return connector readiness checks and ChatGPT connection metadata."""
        return connector_status(
            self.config,
            public_base_url=args.get("public_base_url"),
            reveal_token=False,
        )

    async def _codex_worker_options(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded menu of Codex worker model/reasoning choices."""
        return worker_option_menu(
            self.config,
            model=args.get("model"),
            max_models=args.get("max_models", 12),
            include_model_details=bool(args.get("include_model_details", False)),
        )

    async def _codex_worker_start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start one durable named Codex colleague."""
        repo = self._repo_from_args(args)
        return await self.worker_runtime.start_worker(
            name=args["name"],
            brief=args["brief"],
            repo_path=repo,
            workspace_mode=args.get("workspace_mode", "isolated_write"),
            context_from_workers=args.get("context_from_workers"),
            context_detail=args.get("context_detail", "report"),
            model=args.get("model"),
            reasoning_effort=args.get("reasoning_effort"),
        )

    async def _codex_worker_message(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Continue or redirect an existing worker by human name or id."""
        return await self.worker_runtime.message_worker(
            worker=args["worker"],
            message=args["message"],
            repo_path=self._repo_from_args(args),
            context_from_workers=args.get("context_from_workers"),
            context_detail=args.get("context_detail", "report"),
            model=args.get("model"),
            reasoning_effort=args.get("reasoning_effort"),
        )

    async def _codex_worker_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List durable workers without exposing backend ids or private paths."""
        repo = self._repo_from_args(args)
        return await self.worker_runtime.list_workers(repo_path=repo)

    async def _codex_worker_inspect(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read one worker's current human-oriented report."""
        return await self.worker_runtime.inspect_worker(
            worker=args["worker"],
            wait_seconds=args.get("wait_seconds", 0),
            view=args.get("view", "report"),
            file_path=args.get("file_path"),
            repo_path=self._repo_from_args(args),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            max_bytes=args.get("max_bytes"),
        )

    async def _codex_worker_integrate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an accepted isolated worker result to the base checkout."""
        return await self.worker_runtime.integrate_worker(
            worker=args["worker"],
            repo_path=self._repo_from_args(args),
            allow_dirty_base=bool(args.get("allow_dirty_base", False)),
        )

    async def _codex_worker_stop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Stop only the current turn while preserving conversation continuity."""
        return await self.worker_runtime.stop_worker(
            worker=args["worker"],
            repo_path=self._repo_from_args(args),
            cleanup_workspace=bool(args.get("cleanup_workspace", False)),
        )

    async def _codex_open_workspace(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Open an allowed workspace and return bounded orientation."""
        return self.workspace_context.open_summary(args)

    async def _codex_repo_tree(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded repository tree."""
        return self.workspace_context.repo_tree(args)

    async def _codex_read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read a bounded workspace file slice."""
        try:
            return self.workspace_context.read_file(args)
        except ValueError as error:
            hint = self._worker_file_hint(args)
            if hint:
                raise ValueError(f"{error}. {hint}") from error
            raise

    async def _codex_search_repo(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search an allowed workspace."""
        return self.workspace_context.search_repo(args)

    async def _codex_load_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Load Codex-ready workspace context."""
        return self.workspace_context.load_context(args)

    async def _codex_export_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Export Codex-ready context under .ai-bridge."""
        return self.workspace_context.export_context(args)

    async def _codex_list_skills(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List discovered workspace/user/plugin skills with sanitized paths."""
        return self.workspace_context.list_skills(args)

    async def _codex_load_skill(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Load a bounded discovered SKILL.md body by name/source/path."""
        return self.workspace_context.load_skill(args)

    async def _codex_write_handoff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Write a .ai-bridge handoff plan without executing local commands."""
        return self.workspace_context.write_handoff(args)

    async def _codex_get_handoff_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read .ai-bridge handoff status files."""
        return self.workspace_context.read_handoff_status(args)

    async def _codex_get_handoff_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read .ai-bridge implementation diff."""
        return self.workspace_context.read_handoff_diff(args)

    async def _codex_list_workspaces(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List configured workspaces known to this connector."""
        return self.workspace_context.list_workspaces(args)

    async def _codex_workspace_snapshot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a CodexPro-style workspace snapshot."""
        return self.workspace_context.workspace_snapshot(args)

    async def _codex_inventory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return connector/workspace capability inventory."""
        return self.workspace_context.inventory(args)

    async def _codex_git_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return git status without using bash."""
        return self.workspace_context.git_status_text(args)

    async def _codex_git_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded git diff without using bash."""
        return self.workspace_context.git_diff_tool(args)

    async def _codex_show_changes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return review-oriented status, stats, and diff."""
        return self.workspace_context.show_changes(args)

    async def _codex_write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create or overwrite a workspace file when direct writes are enabled."""
        if not self.power_tools.write_enabled():
            raise ValueError("codex_write_file is disabled. Set power_tools.direct_write to true.")
        return self.workspace_context.write_file(args)

    async def _codex_edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an exact text replacement when direct writes are enabled."""
        if not self.power_tools.write_enabled():
            raise ValueError("codex_edit_file is disabled. Set power_tools.direct_write to true.")
        return self.workspace_context.edit_file(args)

    async def _codex_run_command(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run an optional safe/full command in the workspace."""
        return await self.power_tools.run_command(args)
    
    def _extract_options(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Extract allowed Codex options from arguments."""
        options = {}
        
        # Core parameters
        for key in ['model', 'images', 'search', 'features', 'profile', 'add_dirs',
                    'sandbox', 'approval_policy', 'network',
                    'config_overrides', 'full_auto', 'dangerously_bypass',
                    'structured_output', 'json_events']:
            if key in args:
                if key == 'config_overrides':
                    options[key] = self._safe_config_overrides(args[key])
                else:
                    options[key] = args[key]
        
        return options

    def _allowed_roots(self) -> list[str]:
        return self.config.get('repositories', {}).get('allowed') or []

    def _repo_from_args(self, args: Dict[str, Any]) -> str:
        repo = args.get('repo') or args.get('repo_path') or self.default_repo
        return str(validate_allowed_path(repo, self._allowed_roots()))

    def _worker_file_hint(self, args: Dict[str, Any]) -> str:
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return ""
        try:
            repo = self._repo_from_args(args)
            locations = self.worker_runtime.worker_file_locations(repo_path=repo, file_path=file_path)
        except Exception:
            return ""
        if not locations:
            return ""
        names = ", ".join(location["worker"] for location in locations[:5])
        return (
            "This path exists in isolated worker output for "
            f"{names}. Before integration, read it with codex_worker_inspect using "
            f'view="file" and file_path="{locations[0]["file_path"]}". codex_read_file reads only the base checkout.'
        )

    def _safe_config_overrides(self, overrides: Any) -> list[str]:
        if not overrides:
            return []
        allowed_prefixes = self.config.get('security', {}).get('allowed_config_override_prefixes') or []
        if not allowed_prefixes:
            raise ValueError("config_overrides are disabled by default")
        safe = []
        for override in overrides:
            if not any(str(override).startswith(prefix) for prefix in allowed_prefixes):
                raise ValueError(f"Config override is not allowed: {override}")
            safe.append(str(override))
        return safe

    def _repo_for_session(self, session_id: str, fallback_repo: Optional[str] = None) -> str:
        if fallback_repo:
            return self._repo_from_args({"repo": fallback_repo})
        jobs = sorted(
            self.job_manager.jobs.values(),
            key=lambda job: job.completed_at or job.started_at or 0,
            reverse=True,
        )
        for job in jobs:
            if job.session_id == session_id and job.repo_path:
                try:
                    return self._repo_from_args({"repo": job.repo_path})
                except ValueError:
                    logger.warning("Ignoring out-of-scope repo path for session %s", session_id)
                    continue
        conv = self.conversations.get(session_id, {})
        if conv.get("repo"):
            try:
                return self._repo_from_args({"repo": conv["repo"]})
            except ValueError:
                logger.warning("Ignoring out-of-scope conversation repo for session %s", session_id)
        return self._repo_from_args({})
    
    async def _codex_plan_job(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start a text analytics query job"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = self._repo_from_args(args)
        options = self._extract_options(args)
        options.setdefault(
            "sandbox",
            self.config.get("security", {}).get("default_sandbox", "read-only"),
        )
        
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
        repo = self._repo_from_args(args)
        options = self._extract_options(args)
        options.setdefault("sandbox", "workspace-write")
        
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
            result["message"] = "Operation completed. Use codex_get_result."
        elif job.state == JobState.FAILED:
            result["message"] = "Operation encountered an issue"
            if job.error:
                result["error"] = redact_sensitive_output(job.error)
        
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
                return {"error": "Operation still running. Use codex_get_status."}
            await asyncio.sleep(1)
            waited += 1
            job = self.job_manager.get_job(job_id)
        
        if job.state == JobState.FAILED:
            error_msg = job.error or "Operation encountered an issue"
            return {
                "reference_id": job_id,
                "state": "error",
                "error": redact_sensitive_output(error_msg),
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

    async def _codex_cancel_job(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Cancel a pending or running Codex job."""
        return await self.job_executor.cancel_job(args["job_id"])
    
    async def _codex_review(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run content change analysis"""
        repo = self._repo_from_args(args)
        cmd, stdin_data = self._build_review_command(args)
        
        logger.info(f"Running content analysis in {repo}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=repo,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.job_executor._build_env(),
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(input=stdin_data), timeout=600)
            
            stdout_text = stdout.decode('utf-8', errors='replace')
            stderr_text = stderr.decode('utf-8', errors='replace')
            
            if process.returncode == 0:
                analysis = stdout_text.strip()
                if not analysis and stderr_text.strip():
                    analysis = stderr_text.strip()
                
                return {
                    "status": "completed",
                    "analysis": redact_sensitive_output(analysis if analysis else "No changes to analyze"),
                    "mode": "analysis"
                }
            else:
                return {
                    "status": "error",
                    "error": redact_sensitive_output(stderr_text),
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.exception(f"Analysis failed: {e}")
            return {"error": str(e)}

    def _build_review_command(self, args: Dict[str, Any]) -> tuple[list[str], bytes | None]:
        """Build `codex review` with options before the stdin prompt sentinel."""
        prompt = str(args.get('prompt') or '')
        uncommitted = bool(args.get('uncommitted'))
        base = args.get('base')
        commit = args.get('commit')
        if uncommitted and (base or commit):
            raise ValueError("codex_review accepts either uncommitted=true or base/commit, not both")

        cmd = ['codex', 'review']
        if uncommitted:
            cmd.append('--uncommitted')
        else:
            if base:
                cmd.extend(['--base', str(base)])
            if commit:
                cmd.extend(['--commit', str(commit)])

        if args.get('title'):
            cmd.extend(['--title', str(args['title'])])
        if args.get('model'):
            cmd.extend(['-c', f'model="{args["model"]}"'])

        for override in self._safe_config_overrides(args.get('config_overrides')):
            cmd.extend(['-c', override])

        if prompt:
            cmd.append('-')
            return cmd, prompt.encode('utf-8')
        return cmd, None

    async def _codex_list_sessions(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return bounded metadata for resumable Codex sessions known to this wrapper."""
        repo_filter = None
        if args.get("repo"):
            repo_filter = self._repo_from_args(args)
        max_sessions = max(1, min(int(args.get("max_sessions") or 20), 100))

        by_session: Dict[str, Dict[str, Any]] = {}
        sorted_jobs = sorted(
            self.job_manager.jobs.values(),
            key=lambda job: job.completed_at or job.started_at or 0,
            reverse=True,
        )
        for job in sorted_jobs:
            if not job.session_id:
                continue
            if repo_filter and str(Path(job.repo_path).resolve()) != str(Path(repo_filter).resolve()):
                continue
            if job.session_id in by_session:
                continue

            result = job.result or {}
            summary = result.get("summary") if isinstance(result, dict) else None
            files_changed = result.get("files_changed") if isinstance(result, dict) else None
            workspace_id = "ws_" + hashlib.sha256(str(Path(job.repo_path).resolve()).encode("utf-8")).hexdigest()[:24]
            by_session[job.session_id] = {
                "session_id": job.session_id,
                "last_job_id": job.job_id,
                "mode": job.mode,
                "state": job.state.value,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "workspace_id": workspace_id,
                "summary": redact_sensitive_output(summary) if summary else "",
                "files_changed": redact_sensitive_output(files_changed) if isinstance(files_changed, list) else [],
            }

        sessions = list(by_session.values())
        return {
            "sessions": sessions[:max_sessions],
            "count": min(len(sessions), max_sessions),
            "total_known": len(sessions),
            "truncated": len(sessions) > max_sessions,
            "transcripts_returned": False,
            "repo_paths_returned": False,
        }

    async def _codex_read_session(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read a bounded Codex transcript only when session-read power mode is enabled."""
        return self.codex_sessions.read_session(args)
    
    async def _codex_resume(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start an async Codex resume job."""
        # Accept both session_id and session_ref
        session_id = args.get('session_id') or args.get('session_ref')
        if not session_id:
            return {"error": "Missing session reference"}

        repo = self._repo_for_session(session_id, args.get("repo"))
        prompt = args.get('prompt', '')
        options = self._extract_options(args)
        options["resume_session_id"] = session_id

        try:
            job_id = self.job_manager.create_job('resume', prompt, repo, options)
            asyncio.create_task(self.job_executor.execute_job(job_id))
            return {
                "job_id": job_id,
                "mode": "resume",
                "session_id": session_id,
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result with job_id to inspect resumed output.",
            }
        except Exception as e:
            logger.exception(f"Session continuation failed: {e}")
            return {"error": str(e)}
    
    async def _codex_interactive(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start an async Codex exec session job."""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        repo = self._repo_from_args(args)
        options = self._extract_options(args)

        try:
            job_id = self.job_manager.create_job('interactive', prompt, repo, options)
            asyncio.create_task(self.job_executor.execute_job(job_id))
            return {
                "job_id": job_id,
                "mode": "interactive",
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result. The completed result includes session_ref when Codex returns one.",
            }
        except Exception as e:
            logger.exception(f"Conversational query failed: {e}")
            return {"error": str(e)}
    
    async def _codex_interactive_reply(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start an async continuation job for a Codex session."""
        # FIX: Accept session_id (mapped from session_ref) OR conversation_id for backwards compat
        session_id = args.get('session_id') or args.get('conversation_id')
        if not session_id:
            return {"error": "Missing session_ref/session_id"}
        
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        repo = self._repo_for_session(session_id, args.get("repo"))
        options = self._extract_options(args)
        options["resume_session_id"] = session_id

        try:
            job_id = self.job_manager.create_job('resume', prompt, repo, options)
            asyncio.create_task(self.job_executor.execute_job(job_id))
            return {
                "job_id": job_id,
                "mode": "resume",
                "session_id": session_id,
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result with job_id to inspect continued output.",
            }
        except Exception as e:
            logger.exception(f"Query continuation failed: {e}")
            return {"error": str(e)}
    
    async def _codex_get_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return safe local Codex/wrapper capability metadata without raw config values."""
        import shutil
        from pathlib import Path

        security_config = self.config.get("security", {})
        app_config = self.config.get("app", {})
        mcp_config = self.config.get("mcp", {})
        server_config = self.config.get("server", {})
        logging_config = self.config.get("logging", {})
        repo_config = self.config.get("repositories", {})
        power_config = self.config.get("power_tools", {})
        auth_policy = build_auth_policy(self.config)
        config_path = Path.home() / ".codex" / "config.toml"

        result = {
            "codex_cli": {
                "available": shutil.which("codex") is not None,
            },
            "codex_config": {
                "present": config_path.exists(),
                "path_hint": "~/.codex/config.toml",
                "raw_values_returned": False,
            },
            "wrapper_config": {
                "host": server_config.get("host", "127.0.0.1"),
                "port": server_config.get("port"),
                "tool_mode": app_config.get("tool_mode") or mcp_config.get("tool_mode") or server_config.get("tool_mode") or "full",
                "cors_enabled": bool(server_config.get("enable_cors", False)),
                "access_log_enabled": bool(logging_config.get("access_log", False)),
                "durable_job_state_enabled": bool(logging_config.get("job_state_dir")),
                "power_tools": {
                    "direct_write": bool(power_config.get("direct_write", False)),
                    "bash_mode": power_config.get("bash_mode", "off"),
                    "bash_transcript": power_config.get("bash_transcript", "compact"),
                    "bash_session_configured": bool(power_config.get("bash_session_id")),
                    "require_bash_session": bool(power_config.get("require_bash_session", False)),
                    "codex_session_read": bool(power_config.get("codex_session_read", False)),
                    "codex_home_configured": bool(power_config.get("codex_home")),
                },
                "allowed_roots_count": len(repo_config.get("allowed") or []),
                "default_repository_configured": bool(repo_config.get("default")),
                "default_sandbox": security_config.get("default_sandbox", "read-only"),
                "dangerous_bypass_enabled": bool(security_config.get("allow_dangerously_bypass", False)),
                "config_overrides_enabled": bool(security_config.get("allowed_config_override_prefixes") or []),
                "allowed_env_keys_count": len(security_config.get("allowed_env_keys") or []),
                "http_auth": auth_public_metadata(auth_policy),
            },
            "capabilities": {},
            "note": "Raw local Codex config values, local paths, prompts, and secrets are never returned."
        }

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
            else:
                result["capabilities_error"] = {
                    "message": "Unable to list Codex features.",
                    "exit_code": process.returncode,
                }
        except Exception as e:
            result["capabilities_error"] = {
                "message": "Unable to list Codex features.",
                "error_type": type(e).__name__,
            }

        return result
