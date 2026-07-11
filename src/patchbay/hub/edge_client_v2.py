"""Outbound Hub V2 Edge transport and scheduler.

This module deliberately keeps network delivery separate from local execution.
``EdgeExecutionService`` remains the only authority that invokes the local
``ToolHandler`` and records effects in ``EdgeJournal``.  The runner only moves
fenced attempts, projections, lease renewals, reconciliation records, and
durable result receipts across HTTP.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from patchbay.hub.edge import (
    build_capabilities,
    build_resource_status,
    build_workspaces,
    load_edge_profile,
    normalize_hub_url,
    save_edge_profile,
)
from patchbay.hub.edge_journal import (
    RECOVERY_EXECUTE_INTENT,
    RECOVERY_UPLOAD_RESULT,
)
from patchbay.hub.edge_v2 import EdgeAttemptFenceError, EdgeExecutionService


DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_CLAIM_SECONDS = 0.5
DEFAULT_RESULT_RETRY_SECONDS = 1.0
DEFAULT_RECONCILIATION_SECONDS = 5.0
DEFAULT_LEASE_RENEWAL_SECONDS = 10.0
DEFAULT_ATTEMPT_LEASE_SECONDS = 30.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 30.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT_TASKS = 4
logger = logging.getLogger(__name__)
MAX_CONCURRENT_TASKS = 64
DEFAULT_OUTBOX_BATCH_SIZE = 32


def create_edge_v2_runner(
    config: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | EdgeV2Profile | None = None,
) -> "EdgeV2Runner":
    """Compose one production Edge V2 runner over the mature ToolHandler."""

    from patchbay.connector.profiles import resolve_runtime_path
    from patchbay.hub.edge_journal import EdgeJournal
    from patchbay.jobs.executor import JobExecutor
    from patchbay.jobs.manager import JobManager
    from patchbay.tools.handler import ToolHandler

    config_value = dict(config)
    source = profile or load_edge_profile()
    normalized = source if isinstance(source, EdgeV2Profile) else EdgeV2Profile.from_mapping(source)
    hub = config_value.get("hub") if isinstance(config_value.get("hub"), Mapping) else {}
    edge = hub.get("edge") if isinstance(hub.get("edge"), Mapping) else {}
    journal_path = resolve_runtime_path(
        edge.get("journal_file"),
        "hub",
        f"edge-v2-journal-{normalized.edge_generation}.sqlite3",
    )
    manager = JobManager(config_value)
    executor = JobExecutor(config_value, manager)
    handler = ToolHandler(config_value, manager, executor)
    journal = EdgeJournal(journal_path, edge_generation=normalized.edge_generation)
    execution = EdgeExecutionService(
        handler,
        journal,
        machine_id=normalized.machine_id,
        edge_generation=normalized.edge_generation,
        config=config_value,
    )
    return EdgeV2Runner(
        execution,
        config=config_value,
        profile=normalized,
        close_journal_on_shutdown=True,
    )


@dataclass(frozen=True)
class EdgeV2Endpoints:
    """HTTP paths from the Hub V2 Edge server contract."""

    enroll: str = "/edge/v2/enroll"
    heartbeat: str = "/edge/v2/heartbeat"
    claim: str = "/edge/v2/claim"
    renew_lease: str = "/edge/v2/lease"
    result: str = "/edge/v2/result"
    outbox_ack: str = "/edge/v2/outbox/ack"
    projection: str = "/edge/v2/projection"
    reconcile: str = "/edge/v2/reconcile"


DEFAULT_ENDPOINTS = EdgeV2Endpoints()


class EdgeV2HttpError(RuntimeError):
    """One unsuccessful or invalid Hub HTTP exchange."""

    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class EdgeV2Transport(Protocol):
    """Injectable async JSON transport used by ``EdgeV2Runner``."""

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]: ...


def http_post_json(
    hub_url: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str = "",
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """POST one JSON object with bearer authentication using the stdlib."""

    endpoint = str(path or "").strip()
    if not endpoint.startswith("/"):
        raise ValueError("Edge endpoint path must start with '/'")
    try:
        body = json.dumps(
            dict(payload),
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Edge HTTP payload must be JSON serializable") from exc
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{normalize_hub_url(hub_url)}{endpoint}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=float(timeout_seconds)) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise EdgeV2HttpError(
            f"Hub V2 request failed: {error.code} {detail}",
            status_code=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EdgeV2HttpError(f"Hub V2 request failed: {error}") from error
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise EdgeV2HttpError("Hub V2 returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise EdgeV2HttpError("Hub V2 response must be a JSON object")
    return decoded


class UrllibEdgeV2Transport:
    """Async adapter around :func:`http_post_json`."""

    def __init__(
        self,
        hub_url: str,
        *,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        self.hub_url = normalize_hub_url(hub_url)
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.opener = opener

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        return await asyncio.to_thread(
            http_post_json,
            self.hub_url,
            path,
            payload,
            token=token,
            timeout_seconds=timeout,
            opener=self.opener,
        )


# The longer name is useful to callers which describe dependencies by role.
AsyncJsonHttpTransport = UrllibEdgeV2Transport


@dataclass(frozen=True)
class EdgeV2Profile:
    """Normalized private enrollment profile accepted from V1 or V2 enroll."""

    hub_url: str
    machine_id: str
    node_token: str
    edge_generation: str
    display_name: str = ""
    tags: tuple[str, ...] = ()
    role: str = ""

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "EdgeV2Profile":
        if not isinstance(source, Mapping):
            raise ValueError("Edge profile must be an object")
        profile = source.get("profile") if isinstance(source.get("profile"), Mapping) else source
        machine = source.get("machine") if isinstance(source.get("machine"), Mapping) else {}
        hub_url = _required_text(profile.get("hub_url") or source.get("hub_url"), "hub_url")
        machine_id = _required_text(
            profile.get("machine_id") or machine.get("machine_id") or source.get("machine_id"),
            "machine_id",
        )
        token = _required_text(
            profile.get("node_token")
            or profile.get("token")
            or source.get("node_token")
            or source.get("token"),
            "node_token",
        )
        generation = str(
            profile.get("edge_generation")
            or machine.get("edge_generation")
            or source.get("edge_generation")
            or ""
        ).strip()
        if not generation:
            # Legacy profiles had no generation. Persisting the upgraded mapping
            # makes this generated identity stable across future restarts.
            generation = f"edgegen_{secrets.token_hex(12)}"
        tags_value = profile.get("tags")
        tags = (
            tuple(str(item) for item in tags_value)
            if isinstance(tags_value, Sequence) and not isinstance(tags_value, (str, bytes))
            else ()
        )
        return cls(
            hub_url=normalize_hub_url(hub_url),
            machine_id=machine_id,
            node_token=token,
            edge_generation=generation,
            display_name=str(profile.get("display_name") or machine.get("display_name") or ""),
            tags=tags,
            role=str(profile.get("role") or ""),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "hub_url": self.hub_url,
            "machine_id": self.machine_id,
            "node_token": self.node_token,
            "edge_generation": self.edge_generation,
            "display_name": self.display_name,
            "tags": list(self.tags),
            "role": self.role,
        }


def normalize_edge_v2_profile(
    profile: Mapping[str, Any] | None = None,
    *,
    persist_upgrade: bool = False,
) -> EdgeV2Profile:
    """Load and normalize the existing ``edge enroll`` profile format."""

    source = dict(profile or load_edge_profile())
    if not source:
        raise ValueError("No edge profile found. Run `patchbay edge enroll` first.")
    normalized = EdgeV2Profile.from_mapping(source)
    if persist_upgrade and (
        source.get("edge_generation") != normalized.edge_generation
        or source.get("node_token") != normalized.node_token
    ):
        upgraded = dict(source)
        upgraded.update(normalized.as_mapping())
        save_edge_profile(upgraded)
    return normalized


def edge_contract_metadata(
    capabilities: Mapping[str, Any],
    *,
    edge_generation: str,
) -> dict[str, Any]:
    """Return the generation and exact capability fences sent on every call."""

    actions = capabilities.get("action_capabilities")
    if not isinstance(actions, Mapping):
        actions = capabilities.get("action_capability_versions")
    action_capabilities = dict(actions) if isinstance(actions, Mapping) else {}
    return {
        "protocol_version": str(capabilities.get("protocol_version") or ""),
        "contract_version": str(capabilities.get("contract_version") or ""),
        "manifest_hash": str(capabilities.get("manifest_hash") or ""),
        "schema_hash": str(capabilities.get("schema_hash") or ""),
        "contract_hash": str(capabilities.get("contract_hash") or ""),
        "action_capability_version": str(
            capabilities.get("action_capability_version") or ""
        ),
        "action_capabilities": action_capabilities,
        "action_capability_versions": dict(action_capabilities),
        "edge_generation": _required_text(edge_generation, "edge_generation"),
    }


async def enroll_edge_v2(
    config: Mapping[str, Any],
    *,
    hub_url: str,
    code: str,
    machine_id: str = "",
    display_name: str = "",
    tags: Sequence[str] | None = None,
    role: str = "",
    transport: EdgeV2Transport | None = None,
    endpoints: EdgeV2Endpoints = DEFAULT_ENDPOINTS,
    persist_profile: bool = True,
) -> dict[str, Any]:
    """Enroll through the existing endpoint and save a V1-compatible profile."""

    machine = machine_id or socket.gethostname().lower().replace(".", "-")
    display = display_name or socket.gethostname()
    capabilities = build_capabilities(config)
    client = transport or UrllibEdgeV2Transport(hub_url)
    response = await _transport_post(
        client,
        endpoints.enroll,
        {
            "code": _required_text(code, "code"),
            "machine_id": machine,
            "display_name": display,
            "tags": [str(item) for item in (tags or ())],
            "role": str(role or ""),
            "capabilities": capabilities,
            "workspaces": build_workspaces(config),
        },
    )
    token = str(response.get("node_token") or response.get("token") or "").strip()
    generation = str(
        response.get("edge_generation")
        or (
            response.get("machine", {}).get("edge_generation")
            if isinstance(response.get("machine"), Mapping)
            else ""
        )
    ).strip()
    if not token:
        raise EdgeV2HttpError("Hub did not return a node token")
    if not generation:
        raise EdgeV2HttpError("Hub V2 enrollment did not return an edge generation")
    profile = EdgeV2Profile(
        hub_url=normalize_hub_url(hub_url),
        machine_id=machine,
        node_token=token,
        edge_generation=generation,
        display_name=display,
        tags=tuple(str(item) for item in (tags or ())),
        role=str(role or ""),
    )
    profile_path = save_edge_profile(profile.as_mapping()) if persist_profile else ""
    return {
        "profile_path": profile_path,
        "profile": {
            key: value
            for key, value in profile.as_mapping().items()
            if key != "node_token"
        },
        "machine": response.get("machine"),
    }


class EdgeV2Runner:
    """Run independent outbound control loops around ``EdgeExecutionService``."""

    def __init__(
        self,
        execution_service: EdgeExecutionService,
        *,
        config: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | EdgeV2Profile | None = None,
        transport: EdgeV2Transport | None = None,
        endpoints: EdgeV2Endpoints = DEFAULT_ENDPOINTS,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        claim_interval_seconds: float = DEFAULT_CLAIM_SECONDS,
        result_retry_seconds: float = DEFAULT_RESULT_RETRY_SECONDS,
        reconciliation_interval_seconds: float = DEFAULT_RECONCILIATION_SECONDS,
        lease_renewal_seconds: float = DEFAULT_LEASE_RENEWAL_SECONDS,
        attempt_lease_seconds: float = DEFAULT_ATTEMPT_LEASE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        max_concurrent_tasks: int | None = None,
        outbox_batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
        acknowledged_retention_seconds: float = 0.0,
        close_journal_on_shutdown: bool = False,
    ):
        if not isinstance(execution_service, EdgeExecutionService):
            raise TypeError("execution_service must be an EdgeExecutionService")
        self.execution = execution_service
        self.config = dict(config or {})
        if isinstance(profile, EdgeV2Profile):
            normalized_profile = profile
        else:
            source = dict(profile or load_edge_profile())
            if not source:
                raise ValueError("No edge profile found. Run `patchbay edge enroll` first.")
            nested = source.get("profile")
            machine = source.get("machine")
            generation = str(source.get("edge_generation") or "").strip()
            if isinstance(nested, Mapping):
                generation = generation or str(nested.get("edge_generation") or "").strip()
            if isinstance(machine, Mapping):
                generation = generation or str(machine.get("edge_generation") or "").strip()
            if not generation:
                if isinstance(nested, Mapping):
                    source["profile"] = {
                        **dict(nested),
                        "edge_generation": execution_service.edge_generation,
                    }
                else:
                    source["edge_generation"] = execution_service.edge_generation
                if profile is None:
                    save_edge_profile(source)
            normalized_profile = EdgeV2Profile.from_mapping(source)
        if normalized_profile.machine_id != execution_service.machine_id:
            raise ValueError("Edge profile machine_id does not match EdgeExecutionService")
        if normalized_profile.edge_generation != execution_service.edge_generation:
            raise ValueError(
                "Edge profile generation does not match EdgeExecutionService journal"
            )
        self.profile = normalized_profile
        self.transport = transport or UrllibEdgeV2Transport(
            normalized_profile.hub_url,
            timeout_seconds=request_timeout_seconds,
        )
        self.endpoints = endpoints
        self.heartbeat_interval_seconds = _positive_float(
            heartbeat_interval_seconds, "heartbeat_interval_seconds"
        )
        self.claim_interval_seconds = _positive_float(
            claim_interval_seconds, "claim_interval_seconds"
        )
        self.result_retry_seconds = _positive_float(
            result_retry_seconds, "result_retry_seconds"
        )
        self.reconciliation_interval_seconds = _positive_float(
            reconciliation_interval_seconds,
            "reconciliation_interval_seconds",
        )
        self.lease_renewal_seconds = _positive_float(
            lease_renewal_seconds, "lease_renewal_seconds"
        )
        self.attempt_lease_seconds = max(
            _positive_float(attempt_lease_seconds, "attempt_lease_seconds"),
            self.lease_renewal_seconds * 3.0,
        )
        self.shutdown_timeout_seconds = _non_negative_float(
            shutdown_timeout_seconds, "shutdown_timeout_seconds"
        )
        self.request_timeout_seconds = _positive_float(
            request_timeout_seconds, "request_timeout_seconds"
        )
        configured_limit = (
            self._configured_task_limit()
            if max_concurrent_tasks is None
            else max_concurrent_tasks
        )
        self.max_concurrent_tasks = _bounded_positive_int(
            configured_limit,
            "max_concurrent_tasks",
            maximum=MAX_CONCURRENT_TASKS,
        )
        self.outbox_batch_size = _bounded_positive_int(
            outbox_batch_size,
            "outbox_batch_size",
            maximum=10_000,
        )
        self.acknowledged_retention_seconds = _non_negative_float(
            acknowledged_retention_seconds,
            "acknowledged_retention_seconds",
        )
        self.close_journal_on_shutdown = bool(close_journal_on_shutdown)
        self.capabilities = dict(execution_service.capabilities)
        self.contract_metadata = edge_contract_metadata(
            self.capabilities,
            edge_generation=self.profile.edge_generation,
        )

        self._stop_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._outbox_event = asyncio.Event()
        self._reconciliation_event = asyncio.Event()
        self._control_tasks: set[asyncio.Task[Any]] = set()
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lease_tasks: dict[str, asyncio.Task[Any]] = {}
        self._recovery_queue: deque[dict[str, Any]] = deque()
        self._reconciliation_queue: deque[dict[str, Any]] = deque()
        self._reported_recovery: set[tuple[str, str, str, int]] = set()
        self._background_errors: list[str] = []
        self._latest_projection: dict[str, Any] = {}
        self._projection_error = ""
        self._run_task: asyncio.Task[Any] | None = None
        self._shutdown_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def machine_id(self) -> str:
        return self.profile.machine_id

    @property
    def edge_generation(self) -> str:
        return self.profile.edge_generation

    @property
    def active_task_count(self) -> int:
        return len(self._execution_tasks)

    @property
    def background_errors(self) -> tuple[str, ...]:
        return tuple(self._background_errors)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def control_task_count(self) -> int:
        return len(self._control_tasks)

    def _configured_task_limit(self) -> int:
        hub = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        edge = hub.get("edge") if isinstance(hub.get("edge"), Mapping) else {}
        server = (
            self.config.get("server")
            if isinstance(self.config.get("server"), Mapping)
            else {}
        )
        value = edge.get("max_concurrent_commands")
        if value is None:
            value = server.get("max_concurrent_jobs")
        if value is None:
            value = DEFAULT_MAX_CONCURRENT_TASKS
        return int(value)

    def _base_payload(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "edge_generation": self.edge_generation,
            **self.contract_metadata,
            "contract": dict(self.contract_metadata),
        }

    async def _request(
        self,
        path: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = await _transport_post(
            self.transport,
            path,
            payload,
            token=self.profile.node_token,
            timeout_seconds=self.request_timeout_seconds,
        )
        result = dict(response)
        self._apply_receipt_acknowledgements(result)
        return result

    def _apply_receipt_acknowledgements(
        self,
        response: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        acknowledgements = _receipt_acknowledgements(response)
        if not acknowledgements:
            return []
        return self.execution.acknowledge_receipts(acknowledgements)

    async def heartbeat_once(self) -> dict[str, Any]:
        loop_health = self.execution.journal.control_loop_health()
        projection_health = dict(loop_health.get("projection") or {})
        last_success = projection_health.get("last_success_at")
        projection_health["projection_age_seconds"] = (
            max(0.0, time.time() - float(last_success)) if last_success else None
        )
        worker_status = self._latest_projection or (
            {"error": self._projection_error} if self._projection_error else {}
        )
        resource_status = build_resource_status(self.config, worker_status)
        resource_status["projection_health"] = projection_health
        payload = {
            **self._base_payload(),
            "projection_revision": self.execution.journal.projection_revision,
            "capabilities": dict(self.capabilities),
            "workspaces": build_workspaces(self.config),
            "worker_status": worker_status,
            "control_loop_health": loop_health,
            "projection_health": projection_health,
            "resource_status": resource_status,
            "active_edge_tasks": self.active_task_count,
            "free_edge_task_slots": max(
                0, self.max_concurrent_tasks - self.active_task_count
            ),
        }
        response = await self._request(self.endpoints.heartbeat, payload)
        self._queue_response_work(response)
        return response

    async def projection_once(self) -> dict[str, Any]:
        """Build and publish one full, monotonically revisioned projection."""

        try:
            projection = self.execution.projection_snapshot()
        except Exception as error:
            self._projection_error = str(error)
            raise
        self._latest_projection = dict(projection)
        self._projection_error = ""
        response = await self._request(
            self.endpoints.projection,
            {
                **self._base_payload(),
                "projection_revision": int(projection["projection_revision"]),
                "projection": projection,
            },
        )
        self._queue_response_work(response)
        return response

    async def claim_once(self) -> dict[str, Any]:
        await self._schedule_recovery_queue()
        capacity = self.max_concurrent_tasks - self.active_task_count
        if capacity <= 0 or self._stop_event.is_set():
            return {"claimed": False, "reason": "edge_execution_capacity"}
        response = await self._request(
            self.endpoints.claim,
            {
                **self._base_payload(),
                "projection_revision": self.execution.journal.projection_revision,
                "available_slots": capacity,
                "max_attempts": capacity,
                "lease_seconds": self.attempt_lease_seconds,
            },
        )
        self._queue_response_work(response)
        attempts = _claimed_attempts(response)
        scheduled = 0
        duplicates = 0
        rejected = 0
        for attempt in attempts:
            if self.active_task_count >= self.max_concurrent_tasks:
                break
            outcome = await self._schedule_attempt(attempt)
            if outcome == "scheduled":
                scheduled += 1
            elif outcome == "duplicate":
                duplicates += 1
            else:
                rejected += 1
        return {
            **response,
            "claimed_attempts": len(attempts),
            "scheduled_attempts": scheduled,
            "duplicate_attempts": duplicates,
            "rejected_attempts": rejected,
        }

    async def renew_lease_once(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            **self._base_payload(),
            **_attempt_fences(attempt),
            "expected_revision": _attempt_revision(attempt),
            "lease_seconds": self.attempt_lease_seconds,
        }
        response = await self._request(self.endpoints.renew_lease, payload)
        saved = response.get("attempt")
        if isinstance(saved, Mapping):
            for key in ("revision", "lease_expires_at", "state"):
                if key in saved and isinstance(attempt, dict):
                    attempt[key] = saved[key]
        elif isinstance(attempt, dict):
            for key in ("revision", "lease_expires_at", "state"):
                if key in response:
                    attempt[key] = response[key]
        return response

    async def upload_outbox_once(self) -> dict[str, Any]:
        pending = self.execution.pending_results(limit=self.outbox_batch_size)
        uploaded = 0
        acknowledged = 0
        failures = 0
        for receipt in pending:
            try:
                wire_receipt = {
                    **dict(receipt),
                    "machine_id": self.machine_id,
                    "contract_hash": self.contract_metadata["contract_hash"],
                }
                response = await self._request(
                    self.endpoints.result,
                    {
                        **self._base_payload(),
                        **wire_receipt,
                        "receipt": wire_receipt,
                    },
                )
                uploaded += 1
                if _response_acknowledges_receipt(response, receipt):
                    if self.execution.journal.get_outbox(str(receipt["receipt_id"])):
                        self.execution.acknowledge_receipt(receipt)
                    acknowledged += 1
                elif self.execution.journal.get_outbox(str(receipt["receipt_id"])) is not None:
                    saved = self.execution.journal.get_outbox(str(receipt["receipt_id"]))
                    if saved and saved.get("acknowledged_at") is not None:
                        acknowledged += 1
            except Exception as error:
                failures += 1
                self._record_background_error("result_upload", error)
        confirmed = await self._confirm_outbox_acknowledgements()
        return {
            "pending": len(pending),
            "uploaded": uploaded,
            "acknowledged": acknowledged,
            "confirmed": confirmed,
            "failures": failures,
        }

    async def _confirm_outbox_acknowledgements(self) -> int:
        receipt_ids = self._acknowledged_receipt_ids()
        if not receipt_ids:
            return 0
        try:
            response = await self._request(
                self.endpoints.outbox_ack,
                {
                    **self._base_payload(),
                    "receipt_ids": receipt_ids,
                },
            )
        except Exception as error:
            self._record_background_error("outbox_ack", error)
            return 0
        confirmed = {
            str(item.get("receipt_id") if isinstance(item, Mapping) else item)
            for item in _receipt_acknowledgements(response)
        }
        if not confirmed and response.get("accepted") is True:
            confirmed = set(receipt_ids)
        if not set(receipt_ids).issubset(confirmed):
            return 0
        self.execution.journal.prune_acknowledged(
            retention_seconds=self.acknowledged_retention_seconds
        )
        return len(receipt_ids)

    def _acknowledged_receipt_ids(self) -> list[str]:
        rows = self.execution.journal.connection.execute(
            """
            SELECT receipt_id FROM result_outbox
            WHERE acknowledged_at IS NOT NULL AND uncertain = 0
            ORDER BY acknowledged_at, receipt_id
            """
        ).fetchall()
        return [str(row["receipt_id"]) for row in rows]

    async def reconcile_once(self) -> dict[str, Any]:
        records = self._reconciliation_records()
        if not records:
            return {"accepted": True, "reported_records": 0, "responses": []}
        responses: list[dict[str, Any]] = []
        accepted: set[tuple[str, str, str, int]] = set()
        for record in records:
            fences = _attempt_fences(record, allow_missing=True)
            if not fences.get("operation_id") or not fences.get("attempt_id"):
                continue
            response = await self._request(
                self.endpoints.reconcile,
                {
                    **self._base_payload(),
                    **fences,
                    "projection_revision": self.execution.journal.projection_revision,
                    "local_recovery": dict(record),
                },
            )
            responses.append(response)
            self._queue_response_work(response)
            if _response_accepted(response):
                identity = _recovery_identity(record)
                accepted.add(identity)
                self._reported_recovery.add(identity)
        if accepted:
            self._reconciliation_queue = deque(
                record
                for record in self._reconciliation_queue
                if _recovery_identity(record) not in accepted
            )
        return {
            "accepted": len(accepted) == len(records),
            "reported_records": len(accepted),
            "responses": responses,
        }

    async def _schedule_attempt(self, attempt: Mapping[str, Any]) -> str:
        attempt_id = str(attempt.get("attempt_id") or "").strip()
        if not attempt_id:
            await self._report_rejected_claim(attempt, ValueError("attempt_id is required"))
            return "rejected"
        if attempt_id in self._execution_tasks:
            return "duplicate"
        try:
            self.execution.validate_attempt(attempt)
        except Exception as error:
            await self._report_rejected_claim(attempt, error)
            return "rejected"
        if self.active_task_count >= self.max_concurrent_tasks:
            return "capacity"
        mutable_attempt = dict(attempt)
        task = asyncio.create_task(
            self._execute_attempt(mutable_attempt),
            name=f"patchbay-edge-v2-execute-{attempt_id}",
        )
        self._execution_tasks[attempt_id] = task
        task.add_done_callback(
            lambda completed, key=attempt_id: self._collect_execution_task(key, completed)
        )
        return "scheduled"

    async def _execute_attempt(self, attempt: dict[str, Any]) -> None:
        attempt_id = str(attempt["attempt_id"])
        existing = self.execution.journal.get_attempt(attempt_id)
        if existing is not None:
            attempt["correlation"] = dict(existing.get("correlation") or {})
            transport_correlation = attempt["correlation"].get("edge_transport")
            if isinstance(transport_correlation, Mapping):
                if transport_correlation.get("attempt_revision") not in (None, ""):
                    attempt["revision"] = transport_correlation["attempt_revision"]
                if transport_correlation.get("contract_hash"):
                    attempt["contract_hash"] = transport_correlation["contract_hash"]
                    attempt["required_contract_hash"] = transport_correlation[
                        "contract_hash"
                    ]
            if str(existing.get("state") or "") != "intent_recorded":
                try:
                    result = await self.execution.execute_attempt(attempt)
                    if result.get("needs_reconciliation") and not result.get("receipt_id"):
                        self._reconciliation_queue.append(dict(result))
                        self._reconciliation_event.set()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._record_background_error("execution", error)
                    self._reconciliation_queue.append(
                        self.execution.reconciliation_lookup(attempt_id=attempt_id)
                    )
                    self._reconciliation_event.set()
                finally:
                    self._outbox_event.set()
                return
        lease_stop = asyncio.Event()
        try:
            # Recheck the Hub CAS lease before crossing the local intent/effect
            # boundary. This is especially important for restart recovery.
            await self.renew_lease_once(attempt)
        except Exception as error:
            self._record_background_error("initial_lease", error)
            self._reconciliation_queue.append(
                {
                    **_attempt_fences(attempt, allow_missing=True),
                    "recovery_action": "lease_reconciliation",
                    "reason": "lease_not_confirmed",
                    "detail": str(error),
                }
            )
            self._reconciliation_event.set()
            return
        if existing is None:
            correlation = (
                dict(attempt.get("correlation"))
                if isinstance(attempt.get("correlation"), Mapping)
                else {}
            )
            correlation["edge_transport"] = {
                "attempt_revision": _attempt_revision(attempt),
                "lease_expires_at": attempt.get("lease_expires_at"),
                "contract_hash": str(
                    attempt.get("contract_hash")
                    or attempt.get("required_contract_hash")
                    or self.contract_metadata["contract_hash"]
                ),
            }
            attempt["correlation"] = correlation
        lease_task = asyncio.create_task(
            self._lease_loop(attempt, lease_stop),
            name=f"patchbay-edge-v2-lease-{attempt_id}",
        )
        self._lease_tasks[attempt_id] = lease_task
        try:
            result = await self.execution.execute_attempt(attempt)
            if result.get("needs_reconciliation") and not result.get("receipt_id"):
                self._reconciliation_queue.append(dict(result))
                self._reconciliation_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_background_error("execution", error)
            lookup = self.execution.reconciliation_lookup(attempt_id=attempt_id)
            self._reconciliation_queue.append(lookup)
            self._reconciliation_event.set()
        finally:
            lease_stop.set()
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            self._lease_tasks.pop(attempt_id, None)
            self._outbox_event.set()

    async def _lease_loop(
        self,
        attempt: dict[str, Any],
        stop_event: asyncio.Event,
    ) -> None:
        while not self._stop_event.is_set() and not stop_event.is_set():
            delay = _lease_delay(attempt, self.lease_renewal_seconds)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.renew_lease_once(attempt)
            except Exception as error:
                self._record_background_error("lease_renewal", error)

    def _collect_execution_task(
        self,
        attempt_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        self._execution_tasks.pop(attempt_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as error:
            self._record_background_error("execution_task", error)

    async def _report_rejected_claim(
        self,
        attempt: Mapping[str, Any],
        error: BaseException,
    ) -> None:
        reason = error.reason if isinstance(error, EdgeAttemptFenceError) else "invalid_attempt"
        record = {
            **_attempt_fences(attempt, allow_missing=True),
            "found": False,
            "recovery_action": "claim_rejected",
            "reason": reason,
            "detail": str(error),
        }
        self._reconciliation_queue.append(record)
        self._reconciliation_event.set()
        try:
            await self.reconcile_once()
        except Exception as report_error:
            self._record_background_error("claim_rejection", report_error)

    def _queue_response_work(self, response: Mapping[str, Any]) -> None:
        for key in ("resume_attempts", "retry_attempts"):
            attempts = response.get(key)
            if isinstance(attempts, list):
                for attempt in attempts:
                    if isinstance(attempt, Mapping) and attempt.get("attempt_id"):
                        self._recovery_queue.append(dict(attempt))
        requests = response.get("reconciliation_requests")
        if isinstance(requests, list):
            for request in requests:
                if not isinstance(request, Mapping):
                    continue
                lookup = self.execution.reconciliation_lookup(
                    operation_id=str(request.get("operation_id") or ""),
                    attempt_id=str(request.get("attempt_id") or ""),
                )
                self._reconciliation_queue.append(lookup)
            self._reconciliation_event.set()

    async def _recover_startup(self) -> None:
        for record in self.execution.journal.list_restart_recovery():
            action = str(record.get("recovery_action") or "")
            if action == RECOVERY_EXECUTE_INTENT:
                self._recovery_queue.append(self._attempt_from_recovery(record))
            elif action != RECOVERY_UPLOAD_RESULT:
                self._reconciliation_queue.append(dict(record))
        if self.execution.pending_results():
            self._outbox_event.set()
        if self._reconciliation_queue:
            self._reconciliation_event.set()
        await self._schedule_recovery_queue()

    async def _schedule_recovery_queue(self) -> None:
        attempts = len(self._recovery_queue)
        for _ in range(attempts):
            if self.active_task_count >= self.max_concurrent_tasks:
                return
            attempt = self._recovery_queue.popleft()
            outcome = await self._schedule_attempt(attempt)
            if outcome == "capacity":
                self._recovery_queue.appendleft(attempt)
                return

    def _attempt_from_recovery(self, record: Mapping[str, Any]) -> dict[str, Any]:
        action = _required_text(record.get("action"), "action")
        intent = self.execution.journal.get_intent(str(record.get("operation_id") or ""))
        if intent is None:
            raise ValueError("Recovery intent is missing")
        correlation = (
            dict(record.get("correlation"))
            if isinstance(record.get("correlation"), Mapping)
            else {}
        )
        context = (
            dict(correlation.get("context"))
            if isinstance(correlation.get("context"), Mapping)
            else {}
        )
        action_versions = self.contract_metadata["action_capabilities"]
        action_version = str(action_versions.get(action) or "")
        recovered = {
            "operation_id": record["operation_id"],
            "attempt_id": record["attempt_id"],
            "fencing_token": record["fencing_token"],
            "machine_id": self.machine_id,
            "edge_generation": self.edge_generation,
            "action": action,
            "target_key": record["target_key"],
            "payload": dict(record.get("payload") or {}),
            "arguments": dict(record.get("payload") or {}),
            "idempotency_key": str(intent.get("idempotency_key") or ""),
            "correlation": correlation,
            "context": context,
            "required_contract_hash": self.contract_metadata["contract_hash"],
            "required_action_capability_version": action_version,
            "requirements": dict(self.contract_metadata),
        }
        transport_correlation = correlation.get("edge_transport")
        if isinstance(transport_correlation, Mapping):
            if transport_correlation.get("attempt_revision") not in (None, ""):
                recovered["revision"] = transport_correlation["attempt_revision"]
            if transport_correlation.get("lease_expires_at") not in (None, ""):
                recovered["lease_expires_at"] = transport_correlation[
                    "lease_expires_at"
                ]
            if transport_correlation.get("contract_hash"):
                recovered["contract_hash"] = transport_correlation["contract_hash"]
                recovered["required_contract_hash"] = transport_correlation[
                    "contract_hash"
                ]
        if correlation.get("public_tool"):
            recovered["tool_name"] = correlation["public_tool"]
        for key in ("work_group_id", "lane_id", "parent_operation_id", "item_id"):
            if correlation.get(key):
                recovered[key] = correlation[key]
        return recovered

    def _reconciliation_records(self) -> list[dict[str, Any]]:
        records = list(self._reconciliation_queue)
        for record in self.execution.journal.list_restart_recovery():
            if str(record.get("recovery_action") or "") in {
                RECOVERY_EXECUTE_INTENT,
                RECOVERY_UPLOAD_RESULT,
            }:
                continue
            if _recovery_identity(record) not in self._reported_recovery:
                records.append(dict(record))
        unique: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        for record in records:
            unique[_recovery_identity(record)] = dict(record)
        return list(unique.values())

    async def _heartbeat_loop(self) -> None:
        await self._periodic_loop(
            "heartbeat", self.heartbeat_once, self.heartbeat_interval_seconds
        )

    async def _projection_loop(self) -> None:
        await self._periodic_loop(
            "projection", self.projection_once, self.heartbeat_interval_seconds
        )

    async def _claim_loop(self) -> None:
        await self._periodic_loop("claim", self.claim_once, self.claim_interval_seconds)

    async def _outbox_loop(self) -> None:
        while not self._stop_event.is_set():
            self._outbox_event.clear()
            try:
                await self.upload_outbox_once()
            except Exception as error:
                self._record_background_error("result_outbox", error)
            await _wait_for_event_or_stop(
                self._outbox_event,
                self._stop_event,
                self.result_retry_seconds,
            )

    async def _reconciliation_loop(self) -> None:
        while not self._stop_event.is_set():
            self._reconciliation_event.clear()
            try:
                await self.reconcile_once()
            except Exception as error:
                self._record_background_error("reconciliation", error)
            await _wait_for_event_or_stop(
                self._reconciliation_event,
                self._stop_event,
                self.reconciliation_interval_seconds,
            )

    async def _periodic_loop(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
        interval: float,
    ) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            attempted_at = time.time()
            self.execution.journal.record_control_loop_health(
                name, attempted_at=attempted_at
            )
            try:
                result = await operation()
                revision = None
                if name == "projection" and isinstance(result, Mapping):
                    revision = self.execution.journal.projection_revision
                self.execution.journal.record_control_loop_health(
                    name,
                    attempted_at=attempted_at,
                    succeeded_at=time.time(),
                    success_revision=revision,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._record_background_error(name, error)
                self.execution.journal.record_control_loop_health(
                    name,
                    attempted_at=attempted_at,
                    error_category=type(error).__name__,
                )
            remaining = max(0.0, interval - (time.monotonic() - started))
            if remaining:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass

    def _record_background_error(self, source: str, error: BaseException) -> None:
        self._background_errors.append(f"{source}: {error}")
        logger.exception("Edge control failure in %s", source, exc_info=error)
        if len(self._background_errors) > 256:
            del self._background_errors[:-256]

    def start(self) -> asyncio.Task[None]:
        """Start the runner in the current event loop."""

        if self._run_task is not None and not self._run_task.done():
            return self._run_task
        if self._closed:
            raise RuntimeError("Edge V2 runner is closed")
        self._run_task = asyncio.create_task(
            self.run(), name="patchbay-edge-v2-runner"
        )
        return self._run_task

    async def run(self) -> None:
        """Run until :meth:`shutdown` is called or the task is cancelled."""

        if self._started:
            raise RuntimeError("Edge V2 runner is already running")
        self._started = True
        cancelled = False
        try:
            await self._recover_startup()
            loops = (
                ("heartbeat", self._heartbeat_loop),
                ("projection", self._projection_loop),
                ("claim", self._claim_loop),
                ("outbox", self._outbox_loop),
                ("reconciliation", self._reconciliation_loop),
            )
            for name, factory in loops:
                task = asyncio.create_task(
                    self._supervise_control_loop(name, factory),
                    name=f"patchbay-edge-v2-supervisor-{name}",
                )
                self._control_tasks.add(task)
            await self._stop_event.wait()
        except asyncio.CancelledError:
            cancelled = True
            self._stop_event.set()
            raise
        finally:
            await self._shutdown(cancel_active=cancelled)

    async def _supervise_control_loop(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
    ) -> None:
        """Restart one failed/cancelled control loop without affecting peers."""
        backoff = 0.1
        while not self._stop_event.is_set():
            child = asyncio.create_task(factory(), name=f"patchbay-edge-v2-{name}")
            try:
                await child
                if self._stop_event.is_set():
                    return
                error = RuntimeError("control loop returned unexpectedly")
                self._record_background_error(name, error)
                category = "unexpected_return"
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if self._stop_event.is_set() or (current is not None and current.cancelling()):
                    child.cancel()
                    await asyncio.gather(child, return_exceptions=True)
                    raise
                category = "cancelled"
            except Exception as error:
                self._record_background_error(name, error)
                category = type(error).__name__
            self.execution.journal.record_control_loop_health(
                name,
                attempted_at=time.time(),
                error_category=category,
                restarted=True,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(5.0, backoff * 2.0)

    async def run_once(self) -> dict[str, Any]:
        """Perform one bounded control exchange for tests and diagnostics."""

        await self._recover_startup()
        projection = await self.projection_once()
        heartbeat = await self.heartbeat_once()
        claim = await self.claim_once()
        if self._execution_tasks:
            await asyncio.gather(*tuple(self._execution_tasks.values()), return_exceptions=True)
        outbox = await self.upload_outbox_once()
        reconciliation = await self.reconcile_once()
        return {
            "heartbeat": heartbeat,
            "projection": projection,
            "claim": claim,
            "outbox": outbox,
            "reconciliation": reconciliation,
        }

    async def shutdown(
        self,
        *,
        cancel_active: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        """Stop intake, drain bounded work, flush receipts, and close cleanly."""

        self._stop_event.set()
        await self._shutdown(
            cancel_active=cancel_active,
            timeout_seconds=timeout_seconds,
        )

    async def _shutdown(
        self,
        *,
        cancel_active: bool,
        timeout_seconds: float | None = None,
    ) -> None:
        async with self._shutdown_lock:
            if self._closed:
                return
            self._stop_event.set()
            current = asyncio.current_task()
            controls = tuple(
                task for task in self._control_tasks if task is not current
            )
            for task in controls:
                task.cancel()
            if controls:
                await asyncio.gather(*controls, return_exceptions=True)
            self._control_tasks.clear()

            executions = tuple(self._execution_tasks.values())
            timeout = (
                self.shutdown_timeout_seconds
                if timeout_seconds is None
                else _non_negative_float(timeout_seconds, "timeout_seconds")
            )
            if executions:
                if cancel_active:
                    for task in executions:
                        task.cancel()
                    await asyncio.gather(*executions, return_exceptions=True)
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*executions, return_exceptions=True),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        for task in executions:
                            task.cancel()
                        await asyncio.gather(*executions, return_exceptions=True)

            for task in tuple(self._lease_tasks.values()):
                task.cancel()
            if self._lease_tasks:
                await asyncio.gather(
                    *tuple(self._lease_tasks.values()), return_exceptions=True
                )
            self._lease_tasks.clear()
            self._execution_tasks.clear()
            try:
                await self.upload_outbox_once()
            except Exception as error:
                self._record_background_error("shutdown_result_flush", error)
            try:
                await self.reconcile_once()
            except Exception as error:
                self._record_background_error("shutdown_reconciliation", error)
            if self.close_journal_on_shutdown and not self.execution.journal.closed:
                self.execution.journal.close()
            self._closed = True
            self._closed_event.set()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()


# Role-oriented aliases preserve clarity for callers without creating a second
# execution implementation.
OutboundEdgeRunnerV2 = EdgeV2Runner
EdgeClientV2 = EdgeV2Runner


async def _transport_post(
    transport: EdgeV2Transport,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    method = getattr(transport, "post_json", None)
    if not callable(method):
        method = getattr(transport, "post", None)
    if not callable(method):
        raise TypeError("Edge V2 transport must define post_json() or post()")
    parameters = inspect.signature(method).parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "token" in parameters:
        kwargs["token"] = token
    if accepts_kwargs or "timeout_seconds" in parameters:
        kwargs["timeout_seconds"] = timeout_seconds
    result = method(path, dict(payload), **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise EdgeV2HttpError("Hub V2 transport response must be an object")
    return dict(result)


def _claimed_attempts(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for key in ("attempt", "operation_attempt", "claim"):
        value = response.get(key)
        if isinstance(value, Mapping) and value.get("attempt_id"):
            attempts.append(dict(value))
    for key in ("attempts", "claimed_attempts"):
        value = response.get(key)
        if isinstance(value, list):
            attempts.extend(
                dict(item)
                for item in value
                if isinstance(item, Mapping) and item.get("attempt_id")
            )
    if response.get("attempt_id") and response.get("operation_id"):
        attempts.append(dict(response))
    unique: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        unique[str(attempt["attempt_id"])] = attempt
    return list(unique.values())


def _receipt_acknowledgements(
    response: Mapping[str, Any],
) -> list[str | Mapping[str, Any]]:
    for key in (
        "receipt_acknowledgements",
        "acknowledged_receipts",
        "receipt_ids",
    ):
        value = response.get(key)
        if isinstance(value, list):
            return list(value)
    value = response.get("receipt_acknowledgement")
    if isinstance(value, (str, Mapping)):
        return [value]
    return []


def _response_acknowledges_receipt(
    response: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    receipt_id = str(receipt.get("receipt_id") or "")
    for acknowledgement in _receipt_acknowledgements(response):
        if isinstance(acknowledgement, str) and acknowledgement == receipt_id:
            return True
        if (
            isinstance(acknowledgement, Mapping)
            and str(acknowledgement.get("receipt_id") or "") == receipt_id
        ):
            return True
    return bool(
        response.get("acknowledged") is True
        and str(response.get("receipt_id") or receipt_id) == receipt_id
    )


def _response_accepted(response: Mapping[str, Any]) -> bool:
    if response.get("accepted") is False:
        return False
    if response.get("accepted") is True or response.get("found") is True:
        return True
    status = str(response.get("status") or "").lower()
    return status in {"ok", "accepted", "success", "succeeded"}


def _attempt_fences(
    attempt: Mapping[str, Any],
    *,
    allow_missing: bool = False,
) -> dict[str, Any]:
    required = ("operation_id", "attempt_id", "fencing_token")
    if not allow_missing:
        for key in required:
            if attempt.get(key) in (None, ""):
                raise ValueError(f"{key} is required")
    result = {
        key: attempt[key]
        for key in required
        if attempt.get(key) not in (None, "")
    }
    for key in (
        "machine_id",
        "edge_generation",
        "required_contract_hash",
        "contract_hash",
    ):
        if attempt.get(key) not in (None, ""):
            result[key] = attempt[key]
    if "contract_hash" not in result and attempt.get("required_contract_hash"):
        result["contract_hash"] = attempt["required_contract_hash"]
    correlation = attempt.get("correlation")
    transport = (
        correlation.get("edge_transport")
        if isinstance(correlation, Mapping)
        else None
    )
    if isinstance(transport, Mapping) and transport.get("contract_hash"):
        result["contract_hash"] = transport["contract_hash"]
    return result


def _attempt_revision(attempt: Mapping[str, Any]) -> int:
    for key in ("revision", "attempt_revision", "expected_revision"):
        if attempt.get(key) not in (None, ""):
            try:
                return int(attempt[key])
            except (TypeError, ValueError):
                break
    return 1


def _lease_delay(attempt: Mapping[str, Any], configured: float) -> float:
    expires_at = attempt.get("lease_expires_at")
    try:
        remaining = float(expires_at) - time.time() if expires_at is not None else 0.0
    except (TypeError, ValueError):
        remaining = 0.0
    if remaining > 0:
        return max(0.01, min(configured, remaining / 2.0))
    return max(0.01, configured)


def _recovery_identity(record: Mapping[str, Any]) -> tuple[str, str, str, int]:
    try:
        fencing_token = int(record.get("fencing_token") or 0)
    except (TypeError, ValueError):
        fencing_token = 0
    return (
        str(record.get("operation_id") or ""),
        str(record.get("attempt_id") or ""),
        str(record.get("recovery_action") or record.get("reason") or ""),
        fencing_token,
    )


async def _wait_for_event_or_stop(
    event: asyncio.Event,
    stop: asyncio.Event,
    timeout: float,
) -> None:
    if stop.is_set() or event.is_set():
        return
    event_waiter = asyncio.create_task(event.wait())
    stop_waiter = asyncio.create_task(stop.wait())
    try:
        done, pending = await asyncio.wait(
            (event_waiter, stop_waiter),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        del done
        for task in pending:
            task.cancel()
    finally:
        for task in (event_waiter, stop_waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(event_waiter, stop_waiter, return_exceptions=True)


def _required_text(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _positive_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be positive") from exc
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    return number


def _non_negative_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be non-negative") from exc
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def _bounded_positive_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number < 1:
        raise ValueError(f"{field} must be a positive integer")
    return min(number, maximum)


__all__ = [
    "AsyncJsonHttpTransport",
    "DEFAULT_ENDPOINTS",
    "EdgeClientV2",
    "EdgeV2Endpoints",
    "EdgeV2HttpError",
    "EdgeV2Profile",
    "EdgeV2Runner",
    "EdgeV2Transport",
    "OutboundEdgeRunnerV2",
    "UrllibEdgeV2Transport",
    "edge_contract_metadata",
    "enroll_edge_v2",
    "create_edge_v2_runner",
    "http_post_json",
    "normalize_edge_v2_profile",
]
