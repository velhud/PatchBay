"""Consequential local live evaluator for the production-shaped Hub V2 stack.

The evaluator is intentionally library-only.  It composes the opt-in Hub V2
ASGI server with two in-process Edge V2 runners, while every Edge result reaches
the Hub through the real durable result outbox and pull-transport endpoints.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from patchbay.hub.app_v2 import HubAppV2
from patchbay.hub.edge import build_capabilities, build_workspaces, edge_preflight
from patchbay.hub.edge_client_v2 import (
    DEFAULT_ENDPOINTS,
    EdgeV2HttpError,
    EdgeV2Profile,
    EdgeV2Runner,
)
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.edge_v2 import EdgeExecutionService
from patchbay.hub.server_v2 import create_hub_v2_server
from patchbay.hub.transport_v2 import HubPullTransportBridgeV2
from patchbay.hub.tool_surface import (
    HUB_V2_EXPECTED_TOOL_COUNT,
    HUB_V2_TOOL_NAMES,
)
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler


_TERMINAL_OPERATIONS = frozenset({"succeeded", "blocked", "failed", "cancelled"})
_EDGE_DISPATCH_ENTITY = "hub.edge_dispatch"
_WORKER_FILE = "live-v2-worker.txt"
_MCP_METADATA = {
    "openai/session": "patchbay-live-v2-eval-conversation",
    "openai/subject": "patchbay-live-v2-eval-subject",
}


class LiveHubV2EvalError(RuntimeError):
    """Raised when a required live-evaluation checkpoint is not satisfied."""


class _NullProRequestStore:
    """Canonical store stub for a live run that does not exercise Pro Requests."""

    def list_requests(self, **_: Any) -> dict[str, Any]:
        return {"requests": [], "count": 0, "total_known": 0}

    def read_request(self, **_: Any) -> dict[str, Any]:
        raise ValueError("Pro Request not found")

    claim_request = read_request
    respond_request = read_request
    close_request = read_request


class _DeterministicCodexExecutor(JobExecutor):
    """Deterministic executor at the Codex process boundary.

    Worker creation, continuation, worktree preparation, job persistence,
    projection, inspection, and integration all remain real PatchBay behavior.
    """

    def __init__(self, config: dict[str, Any], manager: JobManager):
        super().__init__(config, manager)
        self.effects: list[dict[str, Any]] = []

    async def execute_job(self, job_id: str) -> None:
        job = self.job_manager.get_job(job_id)
        if job is None:
            raise ValueError(f"Unknown deterministic eval job: {job_id}")
        self.job_manager.update_job_state(job_id, JobState.RUNNING)
        await asyncio.sleep(0)

        options = dict(job.options or {})
        worker_name = str(options.get("_worker_name") or "Worker")
        worker_id = str(options.get("_worker_id") or "")
        is_resume = str(job.mode) == "resume"
        changed_files: list[str] = []
        if worker_name == "Writer" and not is_resume:
            target = Path(str(job.worktree_path)) / _WORKER_FILE
            target.write_text("written by the deterministic live V2 worker\n", encoding="utf-8")
            changed_files.append(_WORKER_FILE)

        turn = "follow-up" if is_resume else "initial"
        summary = f"{worker_name} completed the deterministic {turn} turn."
        session_id = str(options.get("resume_session_id") or f"eval-session-{worker_id}")
        self.effects.append(
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "worker_name": worker_name,
                "mode": str(job.mode),
                "changed_files": changed_files,
            }
        )
        self.job_manager.update_job_state(
            job_id,
            JobState.COMPLETED,
            result={"summary": summary, "files_changed": changed_files},
            session_id=session_id,
            exit_code=0,
        )


class _LiveEdgeHandler:
    """EdgeRunner-compatible projection over the real local ToolHandler."""

    def __init__(self, config: dict[str, Any], tool_handler: ToolHandler):
        self.config = config
        self.tool_handler = tool_handler
        self.worker_runtime = tool_handler.worker_runtime
        self.calls: list[str] = []

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "patchbay_edge_preflight":
            projection = self.worker_runtime.projection_snapshot()
            status = {
                "workers": projection["workers"],
                "active": sum(
                    str(worker.get("turn_state") or "") in {"queued", "starting", "working"}
                    for worker in projection["workers"]
                ),
            }
            return edge_preflight(self.config, arguments, status)
        if tool_name == "codex_worker_integrate":
            # Hub V2 has token/idempotency fields that the mature ToolHandler
            # adapter has not yet adopted. Keep the real WorkerRuntime as the
            # authority while preserving the same ToolHandler-owned instance.
            token = str(arguments.get("preview_token") or "")
            return await self.worker_runtime.integrate_worker(
                worker=str(arguments.get("worker") or ""),
                repo_path=str(arguments.get("repo_path") or ""),
                allow_dirty_base=bool(arguments.get("allow_dirty_base", False)),
                accepted_dirty_base=arguments.get("accepted_dirty_base"),
                preview_token=token,
                idempotency_key="live-v2-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:24],
                request_context=context,
                takeover=bool(arguments.get("takeover", False)),
                takeover_reason=str(arguments.get("takeover_reason") or ""),
            )
        return await self.tool_handler.handle_tool_call(
            tool_name,
            deepcopy(arguments),
            context=context,
        )


class _FakeNetworkTransport:
    """In-process HTTP-shaped network used by one real Edge V2 runner."""

    def __init__(self, server: Any, *, machine_id: str):
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - test extra supplies it.
            raise RuntimeError("The Hub V2 live evaluator requires the test extra (httpx)") from error
        self.machine_id = machine_id
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server),
            base_url="http://patchbay-live-v2.local",
        )
        self.calls: dict[str, int] = defaultdict(int)
        self.claimed_attempt_ids: list[str] = []
        self.lose_and_hold_next_result = False
        self.result_delivery_held = False
        self.lost_result_responses = 0
        self.blocked_result_retries = 0

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        del timeout_seconds
        self.calls[path] += 1
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if self.result_delivery_held:
            self.blocked_result_retries += 1
            raise OSError("simulated Edge network outage after Hub accepted the result")

        response = await self._client.post(path, json=dict(payload), headers=headers)
        if response.status_code >= 400:
            raise EdgeV2HttpError(
                f"Hub V2 fake-network request failed: {response.status_code} {response.text}",
                status_code=response.status_code,
            )
        value = response.json()
        if not isinstance(value, Mapping):
            raise EdgeV2HttpError("Hub V2 fake-network response must be an object")

        if path == DEFAULT_ENDPOINTS.claim:
            attempts = value.get("attempts")
            if isinstance(attempts, list):
                self.claimed_attempt_ids.extend(
                    str(attempt["attempt_id"])
                    for attempt in attempts
                    if isinstance(attempt, Mapping) and attempt.get("attempt_id")
                )
        if path == DEFAULT_ENDPOINTS.result and self.lose_and_hold_next_result:
            self.lose_and_hold_next_result = False
            self.result_delivery_held = True
            self.lost_result_responses += 1
            raise OSError("simulated lost result response after Hub acceptance")
        return dict(value)

    async def close(self) -> None:
        await self._client.aclose()


class _McpClient:
    """Small JSON-RPC client for the evaluator's real ASGI MCP route."""

    def __init__(self, server: Any):
        import httpx

        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server),
            base_url="http://patchbay-live-v2.local",
        )
        self.session_id = ""
        self.request_id = 0

    async def initialize(self) -> dict[str, Any]:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "patchbay-live-hub-v2-eval", "version": "1"},
                "_meta": dict(_MCP_METADATA),
            },
        )
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        return list((await self._rpc("tools/list", {}))["tools"])

    async def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = await self._rpc(
            "tools/call",
            {"name": name, "arguments": deepcopy(dict(arguments)), "_meta": dict(_MCP_METADATA)},
        )
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise LiveHubV2EvalError(f"{name} returned no structuredContent")
        return deepcopy(dict(structured))

    async def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
        response = await self._client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": dict(params),
            },
        )
        if not self.session_id:
            self.session_id = str(response.headers.get("Mcp-Session-Id") or "")
        if response.status_code >= 400:
            raise LiveHubV2EvalError(f"MCP HTTP {response.status_code}: {response.text}")
        body = response.json()
        if "error" in body:
            raise LiveHubV2EvalError(f"MCP {method} failed: {body['error']}")
        result = body.get("result")
        if not isinstance(result, Mapping):
            raise LiveHubV2EvalError(f"MCP {method} returned no result")
        return dict(result)

    async def close(self) -> None:
        await self._client.aclose()


class _EdgeStack:
    def __init__(
        self,
        *,
        name: str,
        machine_id: str,
        edge_generation: str,
        config: dict[str, Any],
        profile: EdgeV2Profile,
        journal_path: Path,
        server: Any,
    ):
        self.name = name
        self.machine_id = machine_id
        self.edge_generation = edge_generation
        self.config = config
        self.manager = JobManager(config)
        self.executor = _DeterministicCodexExecutor(config, self.manager)
        self.tool_handler = ToolHandler(config, self.manager, self.executor)
        self.handler = _LiveEdgeHandler(config, self.tool_handler)
        self.journal = EdgeJournal(journal_path, edge_generation=edge_generation)
        self.execution = EdgeExecutionService(
            self.handler,
            self.journal,
            machine_id=machine_id,
            capabilities=build_capabilities(config),
        )
        self.transport = _FakeNetworkTransport(server, machine_id=machine_id)
        self.profile = profile
        self.runner = EdgeV2Runner(
            self.execution,
            config=config,
            profile=profile,
            transport=self.transport,
            heartbeat_interval_seconds=0.05,
            claim_interval_seconds=0.01,
            result_retry_seconds=0.01,
            reconciliation_interval_seconds=0.05,
            lease_renewal_seconds=0.05,
            shutdown_timeout_seconds=2.0,
            request_timeout_seconds=2.0,
            max_concurrent_tasks=4,
            outbox_batch_size=32,
        )
        self.run_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.runner.projection_once()
        self.run_task = self.runner.start()

    async def stop(self) -> None:
        await self.runner.shutdown(timeout_seconds=2.0)
        if self.run_task is not None:
            await asyncio.gather(self.run_task, return_exceptions=True)
        if not self.journal.closed:
            self.journal.close()
        await self.transport.close()


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _create_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# PatchBay Hub V2 live evaluator\n", encoding="utf-8")
    _git(path, "init")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=PatchBay Live Eval",
        "-c",
        "user.email=live-v2@example.invalid",
        "commit",
        "-m",
        "initial fixture",
    )
    return path


def _edge_config(root: Path, repo: Path, name: str) -> dict[str, Any]:
    state_root = root / name
    return {
        "server": {
            "max_concurrent_jobs": 4,
            "queue_enabled": False,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "workers": {
            "worktree_root": str(state_root / "worker-worktrees"),
            "minimum_poll_seconds": 0,
            "recommended_poll_seconds": 1,
        },
        "hub": {"integration_preview_token_ttl_seconds": 120},
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [
                ".env",
                ".env.*",
                "**/.env",
                "**/.env.*",
                ".git",
                ".git/**",
                "**/.git/**",
                "**/*secret*",
            ],
            "max_diff_bytes": 200_000,
        },
        "power_tools": {"direct_write": False, "bash_mode": "off"},
        "logging": {
            "job_logs_dir": str(state_root / "jobs"),
            "job_state_dir": str(state_root / "jobs" / "state"),
        },
        "locks": {"root": str(state_root / "locks")},
        "pro_requests": {"root": str(state_root / "pro-requests")},
    }


def _create_hub(state_path: Path) -> tuple[HubAppV2, HubPullTransportBridgeV2, Any]:
    delivery = HubPullTransportBridgeV2(semantic_wait_seconds=10.0)
    app = HubAppV2(
        state_path,
        edge_delivery=delivery,
        canonical_pro_store=_NullProRequestStore(),
        pro_request_route={
            "machine_id": "machine_alpha",
            "edge_generation": "edgegen_alpha_live",
            "workspace_ref": "workspace_live_eval",
        },
    )
    delivery.bind(app)
    server = create_hub_v2_server(
        {"auth": {"enabled": False}, "server": {"max_request_bytes": 2_000_000}},
        hub_app=delivery,
    )
    return app, delivery, server


async def _enroll_edge(
    app: HubAppV2,
    server: Any,
    *,
    config: dict[str, Any],
    machine_id: str,
    edge_generation: str,
    display_name: str,
) -> EdgeV2Profile:
    transport = _FakeNetworkTransport(server, machine_id=machine_id)
    try:
        code = app.runtime.create_enrollment_code(name=display_name, tags=["live-v2"])["code"]
        enrolled = await transport.post_json(
            DEFAULT_ENDPOINTS.enroll,
            {
                "code": code,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "display_name": display_name,
                "tags": ["live-v2"],
                "capabilities": build_capabilities(config),
                "workspaces": build_workspaces(config),
            },
        )
    finally:
        await transport.close()
    return EdgeV2Profile(
        hub_url="http://patchbay-live-v2.local",
        machine_id=machine_id,
        node_token=str(enrolled["node_token"]),
        edge_generation=str(enrolled["edge_generation"]),
        display_name=display_name,
        tags=("live-v2",),
    )


async def _wait_until(predicate: Any, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("Timed out waiting for live Hub V2 state")


def _check(
    report: dict[str, Any],
    name: str,
    passed: bool,
    details: Mapping[str, Any] | None = None,
) -> None:
    check = {"name": name, "passed": bool(passed)}
    if details:
        check["details"] = deepcopy(dict(details))
    report["checks"].append(check)
    if not passed:
        raise LiveHubV2EvalError(f"Live Hub V2 check failed: {name}")


def _workers(app: HubAppV2, group_id: str) -> list[dict[str, Any]]:
    return app.runtime._workers_for_group(group_id)


def _dispositions(workers: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for worker in workers:
        integration = str(worker.get("integration_state") or "")
        result.append(
            {
                "fleet_worker_ref": str(worker["fleet_worker_ref"]),
                "disposition": "integrated" if integration == "applied_to_checkout" else "no_changes",
            }
        )
    return result


async def run_live_hub_v2_eval(
    root: str | Path | None = None,
    *,
    keep_temp: bool = False,
) -> dict[str, Any]:
    """Run the full local Hub V2 evaluator and return its structured report."""

    owns_root = root is None
    work_root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="patchbay-live-v2."))
    work_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "name": "patchbay-live-hub-v2-eval",
        "status": "failed",
        "checks": [],
        "tool_count": 0,
        "result_posts_are_edge_generated": True,
    }
    app: HubAppV2 | None = None
    mcp: _McpClient | None = None
    edges: list[_EdgeStack] = []
    restarted_edges: list[_EdgeStack] = []
    restarted_app: HubAppV2 | None = None
    restarted_mcp: _McpClient | None = None

    try:
        alpha_repo = _create_repo(work_root / "repos-alpha" / "live-repo")
        beta_repo = _create_repo(work_root / "repos-beta" / "live-repo")
        alpha_config = _edge_config(work_root, alpha_repo, "alpha-edge")
        beta_config = _edge_config(work_root, beta_repo, "beta-edge")
        state_path = work_root / "hub-v2.sqlite3"
        alpha_journal = work_root / "alpha-edge" / "edge-journal.sqlite3"
        beta_journal = work_root / "beta-edge" / "edge-journal.sqlite3"

        app, delivery, server = _create_hub(state_path)
        _check(
            report,
            "server_started",
            server.state.hub_v2_app is delivery and delivery.app is app,
        )

        alpha_profile = await _enroll_edge(
            app,
            server,
            config=alpha_config,
            machine_id="machine_alpha",
            edge_generation="edgegen_alpha_live",
            display_name="Alpha Live Edge",
        )
        beta_profile = await _enroll_edge(
            app,
            server,
            config=beta_config,
            machine_id="machine_beta",
            edge_generation="edgegen_beta_live",
            display_name="Beta Live Edge",
        )
        edges = [
            _EdgeStack(
                name="alpha",
                machine_id=alpha_profile.machine_id,
                edge_generation=alpha_profile.edge_generation,
                config=alpha_config,
                profile=alpha_profile,
                journal_path=alpha_journal,
                server=server,
            ),
            _EdgeStack(
                name="beta",
                machine_id=beta_profile.machine_id,
                edge_generation=beta_profile.edge_generation,
                config=beta_config,
                profile=beta_profile,
                journal_path=beta_journal,
                server=server,
            ),
        ]
        for edge in edges:
            await edge.start()
        await _wait_until(
            lambda: app.runtime.fleet_status(include_workspaces=True)["result"]["counts"]["online"] == 2
        )
        _check(
            report,
            "two_real_edges_online",
            all(isinstance(edge.execution, EdgeExecutionService) for edge in edges)
            and all(isinstance(edge.tool_handler, ToolHandler) for edge in edges),
            {"machines": [edge.machine_id for edge in edges]},
        )

        mcp = _McpClient(server)
        initialized = await mcp.initialize()
        _check(
            report,
            "mcp_initialize",
            initialized["serverInfo"]["name"] == "patchbay-hub"
            and bool(initialized.get("instructions")),
        )
        tools = await mcp.list_tools()
        tool_names = tuple(tool["name"] for tool in tools)
        report["tool_count"] = len(tool_names)
        report["tool_names"] = list(tool_names)
        _check(
            report,
            "exact_31_tools",
            tool_names == HUB_V2_TOOL_NAMES
            and len(tool_names) == HUB_V2_EXPECTED_TOOL_COUNT == 31,
        )

        workspaces = await mcp.call("patchbay_workspace_list", {"query": "live-repo"})
        workspace_items = list(workspaces["result"].get("workspaces") or [])
        _check(
            report,
            "two_workspace_projections",
            workspaces["status"] == "ok"
            and len(workspace_items) == 1
            and len(workspace_items[0].get("projections") or []) == 2,
        )

        created = await mcp.call(
            "patchbay_work_group_create",
            {
                "title": "Live V2 evaluator",
                "goal": "Exercise real pull-routed worker and integration behavior.",
                "repo_path": "live-repo",
                "lanes": [
                    {"lane": "research", "title": "Research", "role": "Inspect"},
                    {"lane": "writing", "title": "Writing", "role": "Implement"},
                ],
                "idempotency_key": "live-v2-group-create",
            },
        )
        group = dict(created["result"]["work_group"])
        group_id = str(group["work_group_id"])
        pinned_machine = str(group["pinned_machine_id"])
        pinned_generation = str(group["pinned_edge_generation"])
        pinned_edge = next(edge for edge in edges if edge.machine_id == pinned_machine)
        other_edge = next(edge for edge in edges if edge.machine_id != pinned_machine)
        await _wait_until(
            lambda: app.runtime.work_group_status(
                work_group_id=group_id,
                context=None,
            )["result"]["readiness"]["status"]
            == "ready"
        )
        ready_group = await mcp.call(
            "patchbay_work_group_status",
            {"work_group_id": group_id},
        )
        _check(
            report,
            "real_preflight_ready",
            created["status"] in {"ok", "pending"}
            and ready_group["result"]["readiness"]["status"] == "ready"
            and "codex_open_workspace" in pinned_edge.handler.calls,
            {
                "machine_id": pinned_machine,
                "edge_generation": pinned_generation,
                "envelope_status": created.get("status"),
                "readiness_status": ready_group.get("result", {}).get("readiness", {}).get("status"),
                "preflight_handler_called": "codex_open_workspace" in pinned_edge.handler.calls,
                "claim_requests": pinned_edge.transport.calls[DEFAULT_ENDPOINTS.claim],
                "claimed_attempts": len(pinned_edge.transport.claimed_attempt_ids),
                "runner_errors": list(pinned_edge.runner.background_errors)[-5:],
                "journal_recovery": len(pinned_edge.journal.list_restart_recovery()),
            },
        )

        started = await mcp.call(
            "patchbay_worker_start_batch",
            {
                "work_group_id": group_id,
                "shared_brief": "Complete the lane and report exact local evidence.",
                "workers": [
                    {
                        "item_id": "reader",
                        "idempotency_key": "live-v2-reader-start",
                        "name": "Reader",
                        "lane": "research",
                        "mission": "Inspect the disposable repository without changing it.",
                    },
                    {
                        "item_id": "writer",
                        "idempotency_key": "live-v2-writer-start",
                        "name": "Writer",
                        "lane": "writing",
                        "mission": "Create the required live evaluator worker file.",
                    },
                ],
                "idempotency_key": "live-v2-batch-start",
            },
        )
        await _wait_until(
            lambda: len(_workers(app, group_id)) == 2
            and all(worker.get("turn_state") == "completed" for worker in _workers(app, group_id))
        )
        waited = await mcp.call(
            "patchbay_worker_wait",
            {"work_group_id": group_id, "since_revision": 0, "wait_seconds": 0},
        )
        projected_workers = list(waited["result"]["workers"])
        _check(
            report,
            "two_workers_completed_via_projection",
            started["status"] in {"ok", "pending"}
            and {worker["name"] for worker in projected_workers} == {"Reader", "Writer"}
            and waited["result"]["counts"]["completed"] == 2,
        )

        dispatches = [
            entity["record"]
            for entity in app.store.list_entities(_EDGE_DISPATCH_ENTITY)
            if entity["record"].get("action") in {"patchbay_edge_preflight", "codex_worker_start"}
        ]
        _check(
            report,
            "group_machine_pin_enforced",
            all(
                str(record.get("payload", {}).get("machine_id") or "") == pinned_machine
                for record in dispatches
            )
            and not other_edge.transport.claimed_attempt_ids,
            {
                "pinned_machine_id": pinned_machine,
                "other_machine_id": other_edge.machine_id,
                "other_edge_claimed_attempts": len(other_edge.transport.claimed_attempt_ids),
            },
        )

        reader_report = await mcp.call(
            "patchbay_worker_inspect",
            {"work_group_id": group_id, "worker": "Reader", "view": "report"},
        )
        writer_report = await mcp.call(
            "patchbay_worker_inspect",
            {"work_group_id": group_id, "worker": "Writer", "view": "report"},
        )
        writer_changes = await mcp.call(
            "patchbay_worker_inspect",
            {"work_group_id": group_id, "worker": "Writer", "view": "changes"},
        )
        writer_file = await mcp.call(
            "patchbay_worker_inspect",
            {
                "work_group_id": group_id,
                "worker": "Writer",
                "view": "file",
                "file_path": _WORKER_FILE,
            },
        )
        _check(
            report,
            "worker_inspection",
            "Reader completed" in str(reader_report["result"].get("report") or "")
            and "Writer completed" in str(writer_report["result"].get("report") or "")
            and _WORKER_FILE in writer_changes["result"].get("changed_files", [])
            and "deterministic live V2 worker" in str(writer_file["result"].get("text") or ""),
        )

        reader_before = next(worker for worker in projected_workers if worker["name"] == "Reader")
        messaged = await mcp.call(
            "patchbay_worker_message",
            {
                "work_group_id": group_id,
                "fleet_worker_ref": reader_before["fleet_worker_ref"],
                "message": "Confirm the same repository evidence in a follow-up turn.",
                "idempotency_key": "live-v2-reader-message",
            },
        )
        await _wait_until(
            lambda: next(worker for worker in _workers(app, group_id) if worker["name"] == "Reader").get(
                "turn_count"
            )
            == 2
            and next(worker for worker in _workers(app, group_id) if worker["name"] == "Reader").get(
                "turn_state"
            )
            == "completed"
        )
        reader_jobs = [
            job
            for job in pinned_edge.manager.jobs.values()
            if str((job.options or {}).get("_worker_id") or "") == reader_before["edge_worker_id"]
        ]
        _check(
            report,
            "same_worker_continuation",
            messaged["status"] in {"ok", "pending"}
            and len(reader_jobs) == 2
            and len({str(job.worktree_path) for job in reader_jobs}) == 1
            and reader_jobs[-1].mode == "resume",
        )

        writer_job = next(
            job
            for job in pinned_edge.manager.jobs.values()
            if str((job.options or {}).get("_worker_name") or "") == "Writer"
        )
        writer_worktree = Path(str(writer_job.worktree_path))
        _check(
            report,
            "isolated_worktree_write",
            writer_worktree != alpha_repo
            and (writer_worktree / _WORKER_FILE).is_file()
            and not (alpha_repo / _WORKER_FILE).exists(),
        )

        preview = await mcp.call(
            "patchbay_worker_inspect",
            {"work_group_id": group_id, "worker": "Writer", "view": "integration_preview"},
        )
        preview_token = str(preview["result"].get("preview_token") or "")
        _check(
            report,
            "opaque_integration_preview_token",
            preview["status"] == "ok"
            and preview["result"].get("can_apply") is True
            and preview_token.startswith("pit2."),
            {
                "status": preview.get("status"),
                "can_apply": preview.get("result", {}).get("can_apply"),
                "has_preview_token": bool(preview_token),
                "preview_token_prefix": preview_token[:5],
                "reason": preview.get("result", {}).get("reason"),
            },
        )

        base_repo = alpha_repo if pinned_machine == "machine_alpha" else beta_repo
        base_head_before = _git(base_repo, "rev-parse", "HEAD")
        integrated = await mcp.call(
            "patchbay_worker_integrate",
            {
                "work_group_id": group_id,
                "worker": "Writer",
                "preview_token": preview_token,
                "idempotency_key": "live-v2-writer-integrate",
            },
        )
        integration_operation_id = str(integrated["operation"]["operation_id"])
        await _wait_until(
            lambda: str(
                (app.store.get_operation(integration_operation_id) or {}).get("state")
                or ""
            )
            in _TERMINAL_OPERATIONS
        )
        integration_outcome = await mcp.call(
            "patchbay_operation_status",
            {"operation_id": integration_operation_id, "include_result": True},
        )
        integration_domain = integration_outcome["result"].get("domain_result") or {}
        base_head_after = _git(base_repo, "rev-parse", "HEAD")
        base_status = _git(base_repo, "status", "--porcelain")
        _check(
            report,
            "integration_changes_base_without_commit",
            integrated["status"] in {"ok", "pending"}
            and integration_outcome["status"] == "ok"
            and integration_domain.get("applied") is True
            and (base_repo / _WORKER_FILE).is_file()
            and base_head_after == base_head_before
            and _WORKER_FILE in base_status
            and (writer_worktree / _WORKER_FILE).is_file(),
            {
                "base_changed": True,
                "initial_status": integrated.get("status"),
                "final_status": integration_outcome.get("status"),
                "applied": integration_domain.get("applied"),
                "base_file_exists": (base_repo / _WORKER_FILE).is_file(),
                "base_status": base_status,
                "head_unchanged": base_head_after == base_head_before,
                "worker_worktree_preserved": (writer_worktree / _WORKER_FILE).is_file(),
            },
        )
        post_integration_group = await mcp.call(
            "patchbay_work_group_status",
            {"work_group_id": group_id, "include_workers": False},
        )
        _check(
            report,
            "integration_invalidates_preflight_snapshot",
            post_integration_group["status"] == "ok"
            and post_integration_group["result"].get("readiness", {}).get("status") == "ready"
            and post_integration_group["result"].get("readiness", {}).get("currentness")
            == "refresh_required",
            {
                "readiness": post_integration_group.get("result", {}).get("readiness", {}),
            },
        )

        await _wait_until(
            lambda: next(worker for worker in _workers(app, group_id) if worker["name"] == "Writer").get(
                "integration_state"
            )
            == "applied_to_checkout"
        )
        pinned_edge.transport.lose_and_hold_next_result = True
        jobs_before_lost_result = len(pinned_edge.manager.jobs)
        effects_before_lost_result = len(pinned_edge.executor.effects)
        lost_message = await mcp.call(
            "patchbay_worker_message",
            {
                "work_group_id": group_id,
                "worker": "Reader",
                "message": "Record one final restart checkpoint.",
                "idempotency_key": "live-v2-reader-lost-result",
            },
        )
        await _wait_until(
            lambda: pinned_edge.transport.lost_result_responses == 1
            and bool(pinned_edge.journal.list_pending_outbox())
        )
        await _wait_until(
            lambda: len(
                [
                    job
                    for job in pinned_edge.manager.jobs.values()
                    if str((job.options or {}).get("_worker_id") or "")
                    == reader_before["edge_worker_id"]
                ]
            )
            == 3
            and [
                job
                for job in pinned_edge.manager.jobs.values()
                if str((job.options or {}).get("_worker_id") or "")
                == reader_before["edge_worker_id"]
            ][-1].state
            == JobState.COMPLETED
        )
        pending_receipts_before_restart = [
            receipt["receipt_id"] for receipt in pinned_edge.journal.list_pending_outbox()
        ]
        _check(
            report,
            "lost_result_response_is_durable",
            lost_message["status"] in {"ok", "pending"}
            and len(pending_receipts_before_restart) == 1
            and len(pinned_edge.manager.jobs) == jobs_before_lost_result + 1
            and len(pinned_edge.executor.effects) == effects_before_lost_result + 1,
            {
                "initial_status": lost_message.get("status"),
                "pending_receipts": len(pending_receipts_before_restart),
                "result_delivery_held": pinned_edge.transport.result_delivery_held,
                "blocked_result_retries": pinned_edge.transport.blocked_result_retries,
                "runner_errors": list(pinned_edge.runner.background_errors)[-8:],
                "outbox_rows": [
                    dict(row)
                    for row in pinned_edge.journal.connection.execute(
                        "SELECT receipt_id, acknowledged_at FROM result_outbox ORDER BY created_at"
                    ).fetchall()
                ],
            },
        )

        final_workers = _workers(app, group_id)

        worker_job_count_before_restart = len(pinned_edge.manager.jobs)
        initial_effect_count = sum(len(edge.executor.effects) for edge in edges)
        profiles = {edge.machine_id: edge.profile for edge in edges}
        for edge in edges:
            await edge.stop()
        edges = []
        await mcp.close()
        mcp = None
        app.close()
        app = None

        restarted_app, _, restarted_server = _create_hub(state_path)
        restarted_edges = [
            _EdgeStack(
                name="alpha-restarted",
                machine_id="machine_alpha",
                edge_generation="edgegen_alpha_live",
                config=alpha_config,
                profile=profiles["machine_alpha"],
                journal_path=alpha_journal,
                server=restarted_server,
            ),
            _EdgeStack(
                name="beta-restarted",
                machine_id="machine_beta",
                edge_generation="edgegen_beta_live",
                config=beta_config,
                profile=profiles["machine_beta"],
                journal_path=beta_journal,
                server=restarted_server,
            ),
        ]
        restarted_pinned = next(edge for edge in restarted_edges if edge.machine_id == pinned_machine)
        for edge in restarted_edges:
            await edge.runner.run_once()
        _check(
            report,
            "lost_result_reconciled_after_restart",
            restarted_pinned.journal.list_pending_outbox() == []
            and (
                restarted_pinned.transport.calls[DEFAULT_ENDPOINTS.result] >= 1
                or restarted_pinned.transport.calls[DEFAULT_ENDPOINTS.heartbeat] >= 1
            )
            and len(restarted_pinned.manager.jobs) == worker_job_count_before_restart
            and sum(len(edge.executor.effects) for edge in restarted_edges) == 0,
            {
                "replayed_receipts": restarted_pinned.transport.calls[DEFAULT_ENDPOINTS.result],
                "heartbeat_acknowledgements": restarted_pinned.transport.calls[
                    DEFAULT_ENDPOINTS.heartbeat
                ],
                "new_executor_effects": sum(len(edge.executor.effects) for edge in restarted_edges),
            },
        )

        restarted_mcp = _McpClient(restarted_server)
        await restarted_mcp.initialize()
        closed = await restarted_mcp.call(
            "patchbay_work_group_close",
            {
                "work_group_id": group_id,
                "outcome": "complete",
                "summary": "Two workers completed, the accepted patch was integrated without commit.",
                "worker_dispositions": _dispositions(final_workers),
                "idempotency_key": "live-v2-group-close",
            },
        )
        _check(
            report,
            "group_closed_after_receipt_recovery",
            closed["status"] == "ok"
            and closed["result"]["work_group"]["status"] == "closed"
            and closed["result"]["work_group"]["outcome"] == "complete",
        )
        history = await restarted_mcp.call(
            "patchbay_work_group_list",
            {"scope": "history", "include_closed": True, "limit": 20},
        )
        history_workers = await restarted_mcp.call(
            "patchbay_worker_list",
            {"work_group_id": group_id, "include_stopped": True, "limit": 20},
        )
        historical_group = next(
            item
            for item in history["result"]["work_groups"]
            if item["work_group_id"] == group_id
        )
        _check(
            report,
            "hub_and_edge_history_survive_restart",
            historical_group["status"] == "closed"
            and historical_group["pinned_machine_id"] == pinned_machine
            and {worker["name"] for worker in history_workers["result"]["workers"]}
            == {"Reader", "Writer"}
            and (base_repo / _WORKER_FILE).is_file(),
        )

        report.update(
            {
                "status": "passed",
                "group": {
                    "work_group_id": group_id,
                    "status": "closed",
                    "pinned_machine_id": pinned_machine,
                    "pinned_edge_generation": pinned_generation,
                },
                "workers": {
                    "count": 2,
                    "names": ["Reader", "Writer"],
                    "same_worker_turns": 3,
                },
                "integration": {
                    "base_changed": True,
                    "commit_created": False,
                    "worker_worktree_preserved": True,
                    "changed_file": _WORKER_FILE,
                },
                "failure_scenarios": {
                    "machine_pin": "passed",
                    "lost_result_response": "passed",
                    "pending_receipts_before_restart": len(pending_receipts_before_restart),
                    "new_effects_after_restart": 0,
                },
                "restart": {
                    "hub_history_restored": True,
                    "edge_history_restored": True,
                    "initial_executor_effects": initial_effect_count,
                },
            }
        )
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        if restarted_mcp is not None:
            await restarted_mcp.close()
        for edge in restarted_edges:
            try:
                await edge.stop()
            except Exception:
                pass
        if restarted_app is not None:
            restarted_app.close()
        if mcp is not None:
            await mcp.close()
        for edge in edges:
            try:
                await edge.stop()
            except Exception:
                pass
        if app is not None:
            app.close()
        if owns_root and not keep_temp:
            shutil.rmtree(work_root, ignore_errors=True)
        elif owns_root:
            report["temp_root"] = str(work_root)
    return report


def run_live_hub_v2_eval_sync(
    root: str | Path | None = None,
    *,
    keep_temp: bool = False,
) -> dict[str, Any]:
    """Synchronous entrypoint for scripts and non-async test runners."""

    return asyncio.run(run_live_hub_v2_eval(root, keep_temp=keep_temp))


__all__ = [
    "LiveHubV2EvalError",
    "run_live_hub_v2_eval",
    "run_live_hub_v2_eval_sync",
]
