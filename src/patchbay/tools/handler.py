"""Tool handler for PatchBay operations."""
import asyncio
import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from typing import Dict, Any, Optional
from pathlib import Path

from patchbay.artifacts import ArtifactStore
from patchbay.auth import auth_public_metadata, build_auth_policy
from patchbay.jobs.sessions import CodexSessionReader
from patchbay.ownership import merge_owner_metadata
from patchbay.pro_requests import ProRequestStore
from patchbay.protocol.context import RequestContext
from patchbay.repo_locks import (
    RepoMutationBusy,
    RepoMutationLockManager,
    job_requires_repo_mutation_lock,
    mark_repo_lock_options,
)
from patchbay.workers.model_options import worker_option_menu
from patchbay.connector.status import connector_status
from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.executor import JobExecutor
from patchbay.tools.power import PowerToolRunner
from patchbay.security import (
    internal_log_error,
    public_error_message,
    redact_sensitive_output,
    validate_allowed_path,
)
from patchbay.workspace.context import WorkspaceContext
from patchbay.workers.runtime import WorkerRuntime

logger = logging.getLogger(__name__)
_CURRENT_REQUEST_CONTEXT: ContextVar[RequestContext] = ContextVar(
    "patchbay_request_context",
    default=RequestContext.anonymous(),
)


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
        self.repo_locks = RepoMutationLockManager(config)
        self.job_executor.repo_locks = self.repo_locks
        self.power_tools = PowerToolRunner(config, self.workspace_context, repo_locks=self.repo_locks)
        self.codex_sessions = CodexSessionReader(config)
        self.artifact_store = ArtifactStore(config)
        self.pro_request_store = ProRequestStore(config)
        self.worker_runtime = WorkerRuntime(config, job_manager, job_executor, repo_locks=self.repo_locks)
        # Track interactive conversations
        self.conversations: Dict[str, Dict[str, Any]] = {}
    
    def current_request_context(self) -> RequestContext:
        """Return the MCP request context for the current tool call."""
        return _CURRENT_REQUEST_CONTEXT.get()

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
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
            "codex_worker_inbox": self._codex_worker_inbox,
            "codex_worker_start": self._codex_worker_start,
            "codex_worker_message": self._codex_worker_message,
            "codex_worker_list": self._codex_worker_list,
            "codex_worker_inspect": self._codex_worker_inspect,
            "codex_worker_integrate": self._codex_worker_integrate,
            "codex_worker_stop": self._codex_worker_stop,
            "codex_pro_request_list": self._codex_pro_request_list,
            "codex_pro_request_read": self._codex_pro_request_read,
            "codex_pro_request_claim": self._codex_pro_request_claim,
            "codex_pro_request_respond": self._codex_pro_request_respond,
            "codex_pro_request_dispatch": self._codex_pro_request_dispatch,
            "codex_pro_request_close": self._codex_pro_request_close,
            "codex_self_test": self._codex_self_test,
            "codex_get_config": self._codex_get_config,
        }
        handler = handlers.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {tool_name}")

        token = _CURRENT_REQUEST_CONTEXT.set(context or RequestContext.anonymous())
        try:
            return await handler(arguments)
        finally:
            _CURRENT_REQUEST_CONTEXT.reset(token)

    def _reconcile_active_jobs(self) -> None:
        try:
            self.job_executor.reconcile_stale_running_jobs()
        except Exception as error:
            logger.warning("Failed to reconcile active jobs before tool call: %s", internal_log_error(error))

    def _active_job_count(self) -> int:
        counter = getattr(self.job_manager, "active_job_count", None)
        if callable(counter):
            return int(counter())
        jobs = getattr(self.job_manager, "jobs", {})
        return sum(1 for job in getattr(jobs, "values", lambda: [])() if getattr(job, "state", None) in {JobState.PENDING, JobState.RUNNING})

    def _public_error(
        self,
        error: Exception | str,
        *,
        default: str = "Operation could not be completed.",
        allow_details: bool = False,
    ) -> str:
        return public_error_message(error, default=default, allow_details=allow_details)

    async def _codex_self_test(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return connector readiness checks and ChatGPT connection metadata."""
        status = await asyncio.to_thread(
            connector_status,
            self.config,
            public_base_url=args.get("public_base_url"),
            reveal_token=False,
        )
        context = self.current_request_context()
        status["coordination"] = {
            "shared_server": True,
            "client": context.public_metadata(),
            "client_ref": context.client_ref,
            "owner_ref": context.owner_ref,
            "owner_scope": context.owner_scope,
            "active_mcp_sessions": context.active_mcp_sessions,
            "raw_session_ids_returned": False,
            "ownership_model": "coordination_not_authentication",
            "note": (
                "This server URL shares local worker, job, artifact, and repository state across connected "
                "ChatGPT conversations and MCP clients. Read/list/inspect may show shared state; cross-owner "
                "mutations require explicit takeover when ownership checks apply. active_mcp_sessions counts "
                "known transport sessions, and ChatGPT may create many short sessions; worker ownership is "
                "based on owner_scope/client owner metadata, not this count by itself."
            ),
        }
        status["jobs"] = {
            "active_jobs": self._active_job_count(),
            "max_concurrent_jobs": getattr(self.job_manager, "max_concurrent", self.config.get("server", {}).get("max_concurrent_jobs")),
            "active_job_definition": "pending_plus_running",
            "queue_enabled": bool(self.config.get("server", {}).get("queue_enabled", False)),
        }
        return status

    async def _codex_worker_options(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded menu of Codex worker model/reasoning choices."""
        return worker_option_menu(
            self.config,
            model=args.get("model"),
            max_models=args.get("max_models", 12),
            include_model_details=bool(args.get("include_model_details", False)),
        )

    async def _codex_worker_inbox(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Import, list, inspect, or clean up ChatGPT-supplied local artifacts."""
        action = str(args.get("action") or "list").strip().lower()
        repo = self._repo_from_args(args)
        if action == "import_file":
            return self.artifact_store.import_file(
                repo_path=repo,
                artifact_file=args.get("artifact_file") or {},
                label=args.get("label", ""),
                request_context=self.current_request_context(),
            )
        if action == "list":
            return self.artifact_store.list_artifacts(repo_path=repo, request_context=self.current_request_context())
        if action == "inspect":
            return self.artifact_store.inspect_artifact(
                repo_path=repo,
                artifact_id=args.get("artifact_id", ""),
                view=args.get("view", "summary"),
                file_path=args.get("file_path", ""),
                max_bytes=args.get("max_bytes"),
                max_entries=args.get("max_entries"),
                request_context=self.current_request_context(),
            )
        if action == "cleanup":
            return self.artifact_store.cleanup(
                repo_path=repo,
                artifact_id=args.get("artifact_id", ""),
                request_context=self.current_request_context(),
                takeover=bool(args.get("takeover", False)),
                takeover_reason=args.get("takeover_reason", ""),
            )
        raise ValueError("action must be one of: import_file, list, inspect, cleanup")

    async def _codex_worker_start(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start one durable named Codex colleague."""
        repo = self._repo_from_args(args)
        return await self.worker_runtime.start_worker(
            name=args["name"],
            brief=args["brief"],
            repo_path=repo,
            workspace_mode=args.get("workspace_mode", "isolated_write"),
            context_from_workers=args.get("context_from_workers"),
            context_from_artifacts=args.get("context_from_artifacts"),
            context_detail=args.get("context_detail", "report"),
            model=args.get("model"),
            reasoning_effort=args.get("reasoning_effort"),
            request_context=self.current_request_context(),
        )

    async def _codex_worker_message(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Continue or redirect an existing worker by human name or id."""
        return await self.worker_runtime.message_worker(
            worker=args["worker"],
            message=args["message"],
            repo_path=self._repo_from_args(args),
            context_from_workers=args.get("context_from_workers"),
            context_from_artifacts=args.get("context_from_artifacts"),
            context_detail=args.get("context_detail", "report"),
            model=args.get("model"),
            reasoning_effort=args.get("reasoning_effort"),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
            takeover_reason=args.get("takeover_reason", ""),
        )

    async def _codex_worker_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List durable workers without exposing backend ids or private paths."""
        repo = self._repo_from_args(args)
        return await self.worker_runtime.list_workers(
            repo_path=repo,
            active_only=bool(args.get("active_only", False)),
            include_stopped=bool(args.get("include_stopped", True)),
            owned_only=bool(args.get("owned_only", False)),
            created_after=args.get("created_after"),
            request_context=self.current_request_context(),
        )

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
            request_context=self.current_request_context(),
        )

    async def _codex_worker_integrate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an accepted isolated worker result to the base checkout."""
        return await self.worker_runtime.integrate_worker(
            worker=args["worker"],
            repo_path=self._repo_from_args(args),
            allow_dirty_base=bool(args.get("allow_dirty_base", False)),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
            takeover_reason=args.get("takeover_reason", ""),
        )

    async def _codex_worker_stop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Stop only the current turn while preserving conversation continuity."""
        return await self.worker_runtime.stop_worker(
            worker=args["worker"],
            repo_path=self._repo_from_args(args),
            cleanup_workspace=bool(args.get("cleanup_workspace", False)),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
            takeover_reason=args.get("takeover_reason", ""),
        )

    async def _codex_pro_request_list(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.pro_request_store.list_requests(
            repo_path=args.get("repo_path"),
            statuses=args.get("status") or [],
            include_closed=bool(args.get("include_closed", False)),
            limit=int(args.get("limit") or 10),
            request_context=self.current_request_context(),
        )

    async def _codex_pro_request_read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.pro_request_store.read_request(
            request_id=args["request_id"],
            include_report=args.get("include_report", True) is not False,
            include_response=args.get("include_response", True) is not False,
            include_events=bool(args.get("include_events", False)),
            max_report_bytes=args.get("max_report_bytes"),
            max_response_bytes=args.get("max_response_bytes"),
            request_context=self.current_request_context(),
        )

    async def _codex_pro_request_claim(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.pro_request_store.claim_request(
            request_id=args["request_id"],
            note=args.get("note", ""),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
        )

    async def _codex_pro_request_respond(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.pro_request_store.respond_request(
            request_id=args["request_id"],
            response_kind=args.get("response_kind", "analysis"),
            response_markdown=args["response_markdown"],
            recommended_next_action=args.get("recommended_next_action", ""),
            worker_message_markdown=args.get("worker_message_markdown", ""),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
        )

    async def _codex_pro_request_dispatch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = str(args.get("target") or "origin_worker")
        manifest, refusal = self.pro_request_store.mark_dispatch_requested(
            request_id=args["request_id"],
            target=target,
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
        )
        if refusal:
            return {"accepted": False, "request_id": manifest["id"], **refusal}
        read = self.pro_request_store.read_request(
            request_id=args["request_id"],
            include_report=True,
            include_response=True,
            request_context=self.current_request_context(),
        )
        response_text = read.get("response_markdown") or ""
        if not response_text:
            result = {"accepted": False, "note": "This Pro Request has no stored response to dispatch."}
            public = self.pro_request_store.finish_dispatch(
                request_id=args["request_id"],
                accepted=False,
                target=target,
                dispatch_result=result,
                request_context=self.current_request_context(),
            )
            return {**result, "request": public}
        message = response_text
        if str(args.get("message_source") or "worker_message_markdown") == "worker_message_markdown":
            worker_message = (((manifest.get("response") or {}).get("worker_message_markdown")) or "").strip()
            if worker_message:
                message = worker_message
        staleness = read.get("repo_state_check") or {}
        if staleness.get("warning"):
            message = f"Repository state warning from PatchBay: {staleness['warning']}\n\n{message}"
        if target == "new_worker":
            worker_result = await self.worker_runtime.start_worker(
                name=args.get("new_worker_name") or "Pro Solution Implementer",
                brief=message,
                repo_path=(manifest.get("workspace") or {}).get("repo_path_private") or self.default_repo,
                workspace_mode=args.get("workspace_mode", "isolated_write"),
                request_context=self.current_request_context(),
            )
        else:
            origin = manifest.get("origin") or {}
            worker_name = origin.get("worker_name")
            if not worker_name:
                worker_result = {"accepted": False, "note": "This Pro Request has no origin worker to dispatch to."}
            else:
                worker_result = await self.worker_runtime.message_worker(
                    worker=worker_name,
                    message=message,
                    repo_path=(manifest.get("workspace") or {}).get("repo_path_private") or self.default_repo,
                    request_context=self.current_request_context(),
                    takeover=bool(args.get("takeover", False)),
                    takeover_reason=args.get("takeover_reason", ""),
                )
        accepted = bool(worker_result.get("accepted"))
        public = self.pro_request_store.finish_dispatch(
            request_id=args["request_id"],
            accepted=accepted,
            target=target,
            dispatch_result=worker_result,
            request_context=self.current_request_context(),
        )
        return {
            "accepted": accepted,
            "dispatched": accepted,
            "request": public,
            "dispatch_result": worker_result,
            "repo_state_check": staleness,
            "note": "Dispatch never applies worker results to the base checkout and never commits.",
        }

    async def _codex_pro_request_close(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self.pro_request_store.close_request(
            request_id=args["request_id"],
            reason=args.get("reason", ""),
            status=args.get("status", "closed"),
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
        )

    async def _codex_open_workspace(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Open an allowed workspace and return bounded orientation."""
        return await asyncio.to_thread(self.workspace_context.open_summary, args)

    async def _codex_repo_tree(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded repository tree."""
        return await asyncio.to_thread(self.workspace_context.repo_tree, args)

    async def _codex_read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read a bounded workspace file slice."""
        try:
            return await asyncio.to_thread(self.workspace_context.read_file, args)
        except ValueError as error:
            hint = await asyncio.to_thread(self._worker_file_hint, args)
            if hint:
                raise ValueError(f"{error}. {hint}") from error
            raise

    async def _codex_search_repo(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search an allowed workspace."""
        return await asyncio.to_thread(self.workspace_context.search_repo, args)

    async def _codex_load_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Load Codex-ready workspace context."""
        return await asyncio.to_thread(self.workspace_context.load_context, args)

    async def _codex_export_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Export Codex-ready context under .ai-bridge."""
        return await asyncio.to_thread(self.workspace_context.export_context, args)

    async def _codex_list_skills(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List discovered workspace/user/plugin skills with sanitized paths."""
        return await asyncio.to_thread(self.workspace_context.list_skills, args)

    async def _codex_load_skill(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Load a bounded discovered SKILL.md body by name/source/path."""
        return await asyncio.to_thread(self.workspace_context.load_skill, args)

    async def _codex_write_handoff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Write a .ai-bridge handoff plan without executing local commands."""
        return await asyncio.to_thread(self.workspace_context.write_handoff, args)

    async def _codex_get_handoff_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read .ai-bridge handoff status files."""
        return await asyncio.to_thread(self.workspace_context.read_handoff_status, args)

    async def _codex_get_handoff_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read .ai-bridge implementation diff."""
        return await asyncio.to_thread(self.workspace_context.read_handoff_diff, args)

    async def _codex_list_workspaces(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List configured workspaces known to this connector."""
        return await asyncio.to_thread(self.workspace_context.list_workspaces, args)

    async def _codex_workspace_snapshot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a CodexPro-style workspace snapshot."""
        return await asyncio.to_thread(self.workspace_context.workspace_snapshot, args)

    async def _codex_inventory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return connector/workspace capability inventory."""
        return await asyncio.to_thread(self.workspace_context.inventory, args)

    async def _codex_git_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return git status without using bash."""
        return await asyncio.to_thread(self.workspace_context.git_status_text, args)

    async def _codex_git_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a bounded git diff without using bash."""
        return await asyncio.to_thread(self.workspace_context.git_diff_tool, args)

    async def _codex_show_changes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return review-oriented status, stats, and diff."""
        return await asyncio.to_thread(self.workspace_context.show_changes, args)

    async def _codex_write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create or overwrite a workspace file when direct writes are enabled."""
        if not self.power_tools.write_enabled():
            raise ValueError("codex_write_file is disabled. Set power_tools.direct_write to true.")
        repo = self._repo_from_args(args)
        try:
            async with self.repo_locks.hold(repo, operation="direct_write"):
                return self.workspace_context.write_file({**args, "repo": repo})
        except RepoMutationBusy as busy:
            return busy.public_payload()

    async def _codex_edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an exact text replacement when direct writes are enabled."""
        if not self.power_tools.write_enabled():
            raise ValueError("codex_edit_file is disabled. Set power_tools.direct_write to true.")
        repo = self._repo_from_args(args)
        try:
            async with self.repo_locks.hold(repo, operation="direct_edit"):
                return self.workspace_context.edit_file({**args, "repo": repo})
        except RepoMutationBusy as busy:
            return busy.public_payload()

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

    def _extract_owned_options(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Extract Codex options and add private MCP owner metadata when available."""
        return merge_owner_metadata(self._extract_options(args), self.current_request_context())

    async def _create_job_with_optional_repo_lock(
        self,
        mode: str,
        prompt: str,
        repo: str,
        options: Dict[str, Any],
        *,
        operation: str,
    ) -> str:
        lease = None
        if job_requires_repo_mutation_lock(
            mode,
            options,
            default_sandbox=self.config.get("security", {}).get("default_sandbox", "read-only"),
        ):
            lease = await self.repo_locks.acquire(repo, operation=operation)
            options = mark_repo_lock_options(options, operation=operation)
        try:
            job_id = self.job_manager.create_job(mode, prompt, repo, options)
        except Exception:
            if lease is not None:
                lease.release()
            raise
        if lease is not None:
            self.repo_locks.bind_to_job(job_id, lease)
        return job_id

    def _allowed_roots(self) -> list[str]:
        return self.config.get('repositories', {}).get('allowed') or []

    def _repo_from_args(self, args: Dict[str, Any]) -> str:
        repo = args.get('repo') or args.get('repo_path') or self.default_repo
        return str(self.workspace_context.open_workspace(str(repo)).root)

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
        options = self._extract_owned_options(args)
        options.setdefault(
            "sandbox",
            self.config.get("security", {}).get("default_sandbox", "read-only"),
        )
        
        logger.info("Creating analytics query job")
        
        try:
            job_id = await self._create_job_with_optional_repo_lock(
                "plan",
                prompt,
                repo,
                options,
                operation="codex_plan_job",
            )
            self.job_executor.schedule_job(job_id)
            
            return {
                "job_id": job_id,
                "mode": "plan",
                "status": "Operation initiated successfully",
                "access_level": options.get('sandbox', self.config.get('security', {}).get('default_sandbox', 'read-only')),
                "confirmation_mode": options.get('approval_policy', 'on-request')
            }
        except RepoMutationBusy as busy:
            return {"error": busy.public_payload()["note"], **busy.public_payload()}
        except Exception as e:
            logger.error("Failed to create operation: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Operation could not be started.", allow_details=True)}
    
    async def _codex_apply_job(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start a content record update job"""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        # FIX: Use 'or' to handle empty string as not provided
        repo = self._repo_from_args(args)
        options = self._extract_owned_options(args)
        options.setdefault("sandbox", "workspace-write")
        
        logger.info("Creating record update job")
        
        try:
            job_id = await self._create_job_with_optional_repo_lock(
                "apply",
                prompt,
                repo,
                options,
                operation="codex_apply_job",
            )
            self.job_executor.schedule_job(job_id)
            
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
        except RepoMutationBusy as busy:
            return {"error": busy.public_payload()["note"], **busy.public_payload()}
        except Exception as e:
            logger.error("Failed to create update operation: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Operation could not be started.", allow_details=True)}
    
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

        diagnostics = self._job_lifecycle_diagnostics(job)
        if diagnostics:
            result["diagnostics"] = diagnostics
        
        if job.state == JobState.RUNNING:
            result["message"] = "Operation in progress"
        elif job.state == JobState.PENDING:
            result["message"] = "Operation queued"
        elif job.state == JobState.COMPLETED:
            result["message"] = "Operation completed. Use codex_get_result."
        elif job.state == JobState.FAILED:
            result["message"] = "Operation encountered an issue"
            if job.error:
                result["error"] = self._public_error(job.error, default="Operation encountered an issue.", allow_details=True)
        
        return result

    def _job_lifecycle_diagnostics(self, job: Any) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {}
        if getattr(job, "launch_started_at", None) is not None:
            diagnostics["launch_started_at"] = float(job.launch_started_at)
        if getattr(job, "process_started_at", None) is not None:
            diagnostics["process_started_at"] = float(job.process_started_at)
            diagnostics["process_started"] = True
        else:
            diagnostics["process_started"] = False
        if getattr(job, "process_pid", None) is not None:
            diagnostics["process_pid"] = int(job.process_pid)
        if getattr(job, "last_heartbeat_at", None) is not None:
            diagnostics["last_heartbeat_at"] = float(job.last_heartbeat_at)
        if getattr(job, "exit_code", None) is not None:
            diagnostics["exit_code"] = job.exit_code
        diagnostics["session_created"] = bool(getattr(job, "session_id", None))
        return diagnostics
    
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
                "error": self._public_error(error_msg, default="Operation encountered an issue.", allow_details=True),
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
        
        logger.info("Running content analysis")
        
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
                    "error": self._public_error(stderr_text, default="Content analysis failed."),
                    "exit_code": process.returncode
                }
        except Exception as e:
            logger.error("Analysis failed: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Content analysis failed.")}

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
        """Return bounded metadata for resumable Codex sessions known to this PatchBay."""
        repo_filter = None
        if args.get("repo"):
            repo_filter = self._repo_from_args(args)
        max_sessions = max(1, min(int(args.get("max_sessions") or 20), 100))
        query = str(args.get("query") or "").strip().lower()

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
            title = result.get("title") if isinstance(result, dict) else None
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
                "source": "patchbay_job",
                "sources": ["patchbay_job"],
                "known_to_patchbay": True,
                "summary": redact_sensitive_output(summary) if summary else "",
                "files_changed": redact_sensitive_output(files_changed) if isinstance(files_changed, list) else [],
            }
            if title:
                by_session[job.session_id]["title"] = redact_sensitive_output(title)

        discovered = self.codex_sessions.list_sessions(
            {
                "repo": repo_filter,
                "max_sessions": max(200, max_sessions),
            }
        )
        for session in discovered["sessions"]:
            session_id = str(session.get("session_id") or "")
            if not session_id:
                continue
            existing = by_session.get(session_id)
            if existing:
                sources = list(dict.fromkeys([*(existing.get("sources") or [existing.get("source", "patchbay_job")]), "codex_home"]))
                existing["sources"] = sources
                existing["source"] = "+".join(sources)
                existing["known_to_patchbay"] = True
                existing["transcript_available"] = session.get("transcript_available", False)
                for field in ["title", "summary", "created_at", "last_active_at", "resume_command", "project"]:
                    if not existing.get(field) and session.get(field):
                        existing[field] = session[field]
                continue
            by_session[session_id] = dict(session)

        sessions = sorted(
            by_session.values(),
            key=lambda item: item.get("completed_at")
            or item.get("last_active_at")
            or item.get("started_at")
            or item.get("created_at")
            or 0,
            reverse=True,
        )
        if query:
            sessions = [session for session in sessions if self._session_matches_query(session, query)]
        patchbay_known = sum(1 for session in sessions if "patchbay_job" in (session.get("sources") or [session.get("source")]))
        codex_home_known = sum(1 for session in sessions if "codex_home" in (session.get("sources") or [session.get("source")]))
        return {
            "sessions": sessions[:max_sessions],
            "count": min(len(sessions), max_sessions),
            "total_known": len(sessions),
            "truncated": len(sessions) > max_sessions,
            "transcripts_returned": False,
            "repo_paths_returned": False,
            "paths_returned": False,
            "source_path_returned": False,
            "patchbay_known": patchbay_known,
            "codex_home_known": codex_home_known,
        }

    def _session_matches_query(self, session: Dict[str, Any], query: str) -> bool:
        haystack = "\n".join(
            str(value or "")
            for value in [
                session.get("session_id"),
                session.get("title"),
                session.get("summary"),
                session.get("project"),
                session.get("mode"),
            ]
        ).lower()
        return query in haystack

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
        options = self._extract_owned_options(args)
        options["resume_session_id"] = session_id

        try:
            job_id = await self._create_job_with_optional_repo_lock(
                "resume",
                prompt,
                repo,
                options,
                operation="codex_resume",
            )
            self.job_executor.schedule_job(job_id)
            return {
                "job_id": job_id,
                "mode": "resume",
                "session_id": session_id,
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result with job_id to inspect resumed output.",
            }
        except RepoMutationBusy as busy:
            return {"error": busy.public_payload()["note"], **busy.public_payload()}
        except Exception as e:
            logger.error("Session continuation failed: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Session continuation could not be started.")}
    
    async def _codex_interactive(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Start an async Codex exec session job."""
        prompt = args.get('prompt', '')
        
        # Try to decode if it looks like base64
        if prompt and ' ' not in prompt and len(prompt) > 20:
            prompt = decode_if_base64(prompt)
        
        repo = self._repo_from_args(args)
        options = self._extract_owned_options(args)

        try:
            job_id = await self._create_job_with_optional_repo_lock(
                "interactive",
                prompt,
                repo,
                options,
                operation="codex_interactive",
            )
            self.job_executor.schedule_job(job_id)
            return {
                "job_id": job_id,
                "mode": "interactive",
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result. The completed result includes session_ref when Codex returns one.",
            }
        except RepoMutationBusy as busy:
            return {"error": busy.public_payload()["note"], **busy.public_payload()}
        except Exception as e:
            logger.error("Conversational query failed: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Conversational query could not be started.")}
    
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
        options = self._extract_owned_options(args)
        options["resume_session_id"] = session_id

        try:
            job_id = await self._create_job_with_optional_repo_lock(
                "resume",
                prompt,
                repo,
                options,
                operation="codex_interactive_reply",
            )
            self.job_executor.schedule_job(job_id)
            return {
                "job_id": job_id,
                "mode": "resume",
                "session_id": session_id,
                "status": "Operation initiated successfully",
                "note": "Use codex_get_status and codex_get_result with job_id to inspect continued output.",
            }
        except RepoMutationBusy as busy:
            return {"error": busy.public_payload()["note"], **busy.public_payload()}
        except Exception as e:
            logger.error("Query continuation failed: %s", internal_log_error(e))
            return {"error": self._public_error(e, default="Query continuation could not be started.")}
    
    async def _codex_get_config(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return safe local Codex/PatchBay capability metadata without raw config values."""
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
            "patchbay_config": {
                "host": server_config.get("host", "127.0.0.1"),
                "port": server_config.get("port"),
                "tool_mode": app_config.get("tool_mode") or mcp_config.get("tool_mode") or server_config.get("tool_mode") or "worker",
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
                "max_concurrent_jobs": server_config.get("max_concurrent_jobs"),
                "active_jobs": self._active_job_count(),
                "active_job_definition": "pending_plus_running",
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
