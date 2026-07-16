"""Outbound Hub V2 Edge transport and scheduler.

This module deliberately keeps network delivery separate from local execution.
``EdgeExecutionService`` remains the only authority that invokes the local
``ToolHandler`` and records effects in ``EdgeJournal``.  The runner only moves
fenced attempts, projections, lease renewals, reconciliation records, and
durable result receipts across HTTP.
"""
from __future__ import annotations

import asyncio
import http.client
import inspect
import json
import logging
import os
import queue
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

try:  # pragma: no cover - supported PatchBay Edge hosts are Unix-like.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from patchbay.hub.edge import (
    EDGE_PROFILE_VERSION,
    build_capabilities,
    build_resource_status,
    build_workspaces,
    edge_profile_path,
    load_edge_profile,
    normalize_hub_url,
    save_edge_profile,
)
from patchbay.hub.edge_journal import (
    EdgeJournalError,
    MAX_OUTBOX_CONFIRMATION_BATCH_SIZE,
    RECOVERY_EXECUTE_INTENT,
    RECOVERY_UPLOAD_RESULT,
    edge_transport_for_attempt,
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
DEFAULT_HTTP_KEEPALIVE_CONNECTIONS = 8
DEFAULT_MAX_CONCURRENT_TASKS = 4
logger = logging.getLogger(__name__)
MAX_CONCURRENT_TASKS = 64
DEFAULT_OUTBOX_BATCH_SIZE = 32
MAX_ERROR_LOG_KEYS = 128
MAX_REPORTED_RECOVERY_IDENTITIES = 4096
MIN_HOT_QUEUE_IDENTITIES = 128
MAX_HOT_QUEUE_IDENTITIES = 4096


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
    normalized = (
        profile
        if isinstance(profile, EdgeV2Profile)
        else normalize_edge_v2_profile(profile, persist_upgrade=True)
    )
    hub = config_value.get("hub") if isinstance(config_value.get("hub"), Mapping) else {}
    edge = hub.get("edge") if isinstance(hub.get("edge"), Mapping) else {}
    journal_path = resolve_runtime_path(
        edge.get("journal_file"),
        "hub",
        f"edge-v2-journal-{normalized.edge_generation}.sqlite3",
    )
    if bool(edge.get("require_existing_journal", False)) and not journal_path.is_file():
        raise RuntimeError(
            f"Configured Edge journal is missing for generation "
            f"{normalized.edge_generation!r}: {journal_path}"
        )
    manager = JobManager(config_value)
    executor = JobExecutor(config_value, manager)
    handler = ToolHandler(config_value, manager, executor)
    journal = EdgeJournal(
        journal_path,
        edge_generation=normalized.edge_generation,
        pre_migration_backup_marker=edge.get("pre_migration_backup_marker"),
    )
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


class PersistentEdgeV2Transport:
    """Async JSON transport with bounded reusable stdlib HTTP connections.

    One request is attempted exactly once. A stale or broken pooled connection
    is discarded and reported to the runner instead of being retried below the
    durable Hub operation boundary.
    """

    def __init__(
        self,
        hub_url: str,
        *,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        max_keepalive_connections: int = DEFAULT_HTTP_KEEPALIVE_CONNECTIONS,
        connection_factory: Callable[[], http.client.HTTPConnection] | None = None,
    ):
        self.hub_url = normalize_hub_url(hub_url)
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.max_keepalive_connections = _bounded_positive_int(
            max_keepalive_connections,
            "max_keepalive_connections",
            maximum=64,
        )
        parsed = urllib.parse.urlsplit(self.hub_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Hub URL must use http or https with a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Hub URL cannot contain credentials, a query, or a fragment")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
        self._connection_factory = connection_factory
        self._pool: queue.LifoQueue[http.client.HTTPConnection] = queue.LifoQueue(
            maxsize=self.max_keepalive_connections
        )
        self._state_lock = threading.Lock()
        self._closed = False

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
            self._post_json,
            path,
            payload,
            token=token,
            timeout_seconds=timeout,
        )

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        while True:
            try:
                connection = self._pool.get_nowait()
            except queue.Empty:
                break
            connection.close()

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
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

        connection = self._acquire_connection(timeout_seconds)
        reusable = False
        try:
            connection.request(
                "POST",
                f"{self._base_path}{endpoint}",
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            reusable = not response.will_close
            if not 200 <= response.status < 300:
                raise EdgeV2HttpError(
                    f"Hub V2 request failed: {response.status} {raw}",
                    status_code=response.status,
                )
        except EdgeV2HttpError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as error:
            raise EdgeV2HttpError(f"Hub V2 request failed: {error}") from error
        finally:
            if reusable:
                self._release_connection(connection)
            else:
                connection.close()
        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise EdgeV2HttpError("Hub V2 returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise EdgeV2HttpError("Hub V2 response must be a JSON object")
        return decoded

    def _acquire_connection(self, timeout_seconds: float) -> http.client.HTTPConnection:
        with self._state_lock:
            if self._closed:
                raise EdgeV2HttpError("Hub V2 transport is closed")
        try:
            connection = self._pool.get_nowait()
        except queue.Empty:
            connection = self._new_connection(timeout_seconds)
        connection.timeout = timeout_seconds
        if connection.sock is not None:
            connection.sock.settimeout(timeout_seconds)
        return connection

    def _new_connection(self, timeout_seconds: float) -> http.client.HTTPConnection:
        if self._connection_factory is not None:
            return self._connection_factory()
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=timeout_seconds,
                context=self._ssl_context,
            )
        return http.client.HTTPConnection(
            self._host,
            self._port,
            timeout=timeout_seconds,
        )

    def _release_connection(self, connection: http.client.HTTPConnection) -> None:
        with self._state_lock:
            closed = self._closed
        if closed:
            connection.close()
            return
        try:
            self._pool.put_nowait(connection)
        except queue.Full:
            connection.close()


# The longer name is useful to callers which describe dependencies by role.
AsyncJsonHttpTransport = PersistentEdgeV2Transport


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
        normalized = _persist_edge_profile_upgrade(source, normalized)
    return normalized


def _persist_edge_profile_upgrade(
    source: Mapping[str, Any],
    normalized: EdgeV2Profile,
) -> EdgeV2Profile:
    """Atomically publish a generated generation before journal selection."""

    path = edge_profile_path()
    lock_path = path.with_name(f".{path.name}.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fcntl is None:
        raise RuntimeError(
            "Atomic Edge profile generation upgrade requires host file locking; "
            "refusing to select or open an Edge journal"
        )
    try:
        os.chmod(path.parent, 0o700)
        with lock_path.open("a+b") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                current = load_edge_profile()
                if current:
                    current_profile = EdgeV2Profile.from_mapping(current)
                    if _edge_profile_authority(current_profile) != _edge_profile_authority(
                        normalized
                    ):
                        raise RuntimeError(
                            "Edge profile changed while its generation was being upgraded; "
                            "refusing to select or open an Edge journal"
                        )
                    if _edge_generation_from_mapping(current):
                        return current_profile

                upgraded = dict(current or source)
                upgraded.update(normalized.as_mapping())
                _atomic_write_edge_profile(path, upgraded)
                persisted = EdgeV2Profile.from_mapping(load_edge_profile())
                if (
                    persisted.edge_generation != normalized.edge_generation
                    or _edge_profile_authority(persisted)
                    != _edge_profile_authority(normalized)
                ):
                    raise RuntimeError(
                        "Atomic Edge profile generation upgrade could not be verified; "
                        "refusing to select or open an Edge journal"
                    )
                return persisted
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Could not atomically persist the generated Edge generation to {path}; "
            "refusing to select or open an Edge journal"
        ) from exc


def _atomic_write_edge_profile(path: Path, profile: Mapping[str, Any]) -> None:
    payload = dict(profile)
    payload["version"] = EDGE_PROFILE_VERSION
    payload["updated_at"] = time.time()
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _edge_profile_authority(profile: EdgeV2Profile) -> tuple[str, str, str]:
    return (profile.hub_url, profile.machine_id, profile.node_token)


def _edge_generation_from_mapping(source: Mapping[str, Any]) -> str:
    profile = source.get("profile") if isinstance(source.get("profile"), Mapping) else {}
    machine = source.get("machine") if isinstance(source.get("machine"), Mapping) else {}
    return str(
        profile.get("edge_generation")
        or machine.get("edge_generation")
        or source.get("edge_generation")
        or ""
    ).strip()


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
        self.transport = transport or PersistentEdgeV2Transport(
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
        self.outbox_confirmation_batch_size = min(
            self.outbox_batch_size,
            MAX_OUTBOX_CONFIRMATION_BATCH_SIZE,
        )
        self.hot_queue_identity_limit = min(
            MAX_HOT_QUEUE_IDENTITIES,
            max(MIN_HOT_QUEUE_IDENTITIES, self.outbox_batch_size * 4),
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
        self._heartbeat_event = asyncio.Event()
        self._outbox_event = asyncio.Event()
        self._reconciliation_event = asyncio.Event()
        self._control_tasks: set[asyncio.Task[Any]] = set()
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lease_tasks: dict[str, asyncio.Task[Any]] = {}
        self._recovery_queue: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._reconciliation_queue: OrderedDict[
            tuple[str, str, int], dict[str, Any]
        ] = OrderedDict()
        self._reported_recovery: OrderedDict[
            tuple[str, str, int], None
        ] = OrderedDict()
        self._recovery_cursor: tuple[float, str] | None = None
        self._reconciliation_cursor: tuple[float, str] | None = None
        self._reconciliation_queue_first = True
        self._outbox_cursor: tuple[float, str] | None = None
        self._outbox_retry_state: OrderedDict[
            str, dict[str, float | int]
        ] = OrderedDict()
        self._reconciliation_retry_state: OrderedDict[
            tuple[str, str, int], dict[str, float | int]
        ] = OrderedDict()
        self._error_log_state: OrderedDict[
            tuple[str, str, str], dict[str, float | int]
        ] = OrderedDict()
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
            "session_contract_hash": self.contract_metadata["contract_hash"],
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
        saved = self.execution.acknowledge_receipts(acknowledgements)
        for receipt in saved:
            identity = _pending_reconciliation_key(receipt)
            self._reconciliation_queue.pop(identity, None)
            self._reconciliation_retry_state.pop(identity, None)
        return saved

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
            projection = await self.execution.projection_snapshot_async()
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
        self._heartbeat_event.set()
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
        retry_receipts = self._due_outbox_retry_receipts(
            limit=max(1, min(self.outbox_batch_size, 32))
        )
        retry_ids = {str(receipt["receipt_id"]) for receipt in retry_receipts}
        forward = self.execution.pending_results(
            limit=self.outbox_batch_size,
            after=self._outbox_cursor,
        )
        if not forward and self._outbox_cursor is not None:
            self._outbox_cursor = None
            forward = self.execution.pending_results(limit=self.outbox_batch_size)
        pending = retry_receipts + [
            receipt
            for receipt in forward
            if str(receipt.get("receipt_id") or "") not in retry_ids
        ]
        uploaded = 0
        acknowledged = 0
        failures = 0
        deferred = 0
        for receipt in pending:
            receipt_id = str(receipt.get("receipt_id") or "")
            if self._retry_is_deferred(self._outbox_retry_state, receipt_id):
                deferred += 1
                continue
            try:
                receipt_contract_hash = str(
                    receipt.get("contract_hash")
                    or self.contract_metadata["contract_hash"]
                )
                wire_receipt = {
                    **dict(receipt),
                    "machine_id": self.machine_id,
                    "contract_hash": receipt_contract_hash,
                }
                response = await self._request(
                    self.endpoints.result,
                    {
                        **self._base_payload(),
                        **wire_receipt,
                        # Authenticate this request with the Edge's current
                        # transport contract.  The nested receipt retains the
                        # immutable contract used by the original attempt.
                        "contract_hash": self.contract_metadata["contract_hash"],
                        "receipt": wire_receipt,
                    },
                )
                uploaded += 1
                if _response_acknowledges_receipt(response, receipt):
                    if self.execution.journal.get_outbox(str(receipt["receipt_id"])):
                        self.execution.acknowledge_receipt(receipt)
                    acknowledged += 1
                    self._outbox_retry_state.pop(receipt_id, None)
                else:
                    saved = self.execution.journal.get_outbox(str(receipt["receipt_id"]))
                    if saved and saved.get("acknowledged_at") is not None:
                        acknowledged += 1
                        self._outbox_retry_state.pop(receipt_id, None)
                    else:
                        failures += 1
                        self._defer_retry(self._outbox_retry_state, receipt_id)
            except Exception as error:
                failures += 1
                self._defer_retry(self._outbox_retry_state, receipt_id)
                self._record_background_error("result_upload", error)
        if forward:
            last = forward[-1]
            self._outbox_cursor = (
                float(last.get("created_at") or 0.0),
                str(last.get("receipt_id") or ""),
            )
        confirmed = await self._confirm_outbox_acknowledgements()
        return {
            "pending": len(pending),
            "uploaded": uploaded,
            "acknowledged": acknowledged,
            "confirmed": confirmed,
            "failures": failures,
            "deferred": deferred,
            "retry_candidates": len(retry_receipts),
            "forward_candidates": len(forward),
        }

    def _due_outbox_retry_receipts(self, *, limit: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        due = sorted(
            (
                (float(retry.get("next_retry_at") or 0.0), receipt_id)
                for receipt_id, retry in self._outbox_retry_state.items()
                if float(retry.get("next_retry_at") or 0.0) <= now
            ),
            key=lambda item: (item[0], item[1]),
        )
        receipts: list[dict[str, Any]] = []
        for _, receipt_id in due:
            saved = self.execution.journal.get_outbox(receipt_id)
            if saved is None or saved.get("acknowledged_at") is not None:
                self._outbox_retry_state.pop(receipt_id, None)
                continue
            receipts.append(saved)
            if len(receipts) >= limit:
                break
        return receipts

    async def _confirm_outbox_acknowledgements(self) -> int:
        receipts = self.execution.journal.list_outbox_pending_confirmation(
            limit=self.outbox_confirmation_batch_size
        )
        if not receipts:
            return 0
        receipt_ids = [str(receipt["receipt_id"]) for receipt in receipts]
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
        confirmed.intersection_update(receipt_ids)
        if not confirmed:
            return 0
        persisted = self.execution.journal.confirm_outbox_deliveries(
            [receipt_id for receipt_id in receipt_ids if receipt_id in confirmed]
        )
        self.execution.journal.prune_acknowledged(
            retention_seconds=self.acknowledged_retention_seconds
        )
        return persisted

    async def reconcile_once(self) -> dict[str, Any]:
        records = self._reconciliation_records()
        if not records:
            return {"accepted": True, "reported_records": 0, "responses": []}
        responses: list[dict[str, Any]] = []
        accepted: set[tuple[str, str, int]] = set()
        failures = 0
        deferred = 0
        for record in records:
            fences = _attempt_fences(record, allow_missing=True)
            if not fences.get("operation_id") or not fences.get("attempt_id"):
                continue
            identity = _pending_reconciliation_key(record)
            if self._retry_is_deferred(self._reconciliation_retry_state, identity):
                deferred += 1
                continue
            try:
                response = await self._request(
                    self.endpoints.reconcile,
                    {
                        **self._base_payload(),
                        **fences,
                        "projection_revision": self.execution.journal.projection_revision,
                        "local_recovery": dict(record),
                    },
                )
            except Exception as error:
                failures += 1
                self._defer_retry(self._reconciliation_retry_state, identity)
                self._record_background_error("reconciliation_record", error)
                continue
            responses.append(response)
            self._queue_response_work(response)
            if self._reconciliation_response_progressed(record, response):
                accepted.add(identity)
                self._record_reported_recovery(identity)
                self._reconciliation_retry_state.pop(identity, None)
            else:
                failures += 1
                self._defer_retry(self._reconciliation_retry_state, identity)
        if accepted:
            for identity in accepted:
                self._reconciliation_queue.pop(identity, None)
        return {
            "accepted": failures == 0 and deferred == 0,
            "reported_records": len(accepted),
            "failed_records": failures,
            "deferred_records": deferred,
            "responses": responses,
        }

    def _reconciliation_response_progressed(
        self,
        record: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> bool:
        if not _response_accepted(response):
            return False
        disposition = str(response.get("disposition") or "")
        if disposition == "manual_recovery":
            response_attempt = response.get("attempt")
            operation = response.get("operation")
            if not (
                isinstance(response_attempt, Mapping)
                and str(response_attempt.get("state") or "") == "manual_recovery"
                and isinstance(operation, Mapping)
                and str(operation.get("state") or "") == "blocked"
            ):
                return False
            attempt_id = str(record.get("attempt_id") or "")
            local_attempt = self.execution.journal.get_attempt(attempt_id)
            if local_attempt is not None and str(local_attempt.get("state") or "") in {
                "executing",
                "effect_recorded",
                "outcome_unknown",
                "manual_recovery",
            }:
                self.execution.journal.mark_manual_recovery(
                    str(local_attempt["operation_id"]),
                    attempt_id,
                    int(local_attempt["fencing_token"]),
                    edge_generation=str(local_attempt["edge_generation"]),
                )
            return True
        if disposition == "retryable":
            operation = response.get("operation")
            return bool(
                response.get("retry_attempts")
                or response.get("resume_attempts")
                or (
                    isinstance(operation, Mapping)
                    and str(operation.get("state") or "")
                    in {"succeeded", "blocked", "failed", "cancelled"}
                )
            )
        receipt = record.get("receipt")
        return bool(
            isinstance(receipt, Mapping)
            and receipt.get("receipt_id")
            and _response_acknowledges_receipt(response, receipt)
        )

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
            transport_correlation = edge_transport_for_attempt(
                attempt["correlation"], attempt_id
            )
            if transport_correlation:
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
                        self._enqueue_reconciliation(result)
                        self._reconciliation_event.set()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._record_background_error("execution", error)
                    self._enqueue_reconciliation(
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
            self._enqueue_reconciliation(
                {
                    **_attempt_fences(attempt, allow_missing=True),
                    "recovery_action": "lease_reconciliation",
                    "found": False,
                    "effect_started": False,
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
                self._enqueue_reconciliation(result)
                self._reconciliation_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_background_error("execution", error)
            lookup = self.execution.reconciliation_lookup(attempt_id=attempt_id)
            self._enqueue_reconciliation(lookup)
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
        self._enqueue_reconciliation(record)
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
                        self._enqueue_recovery_attempt(attempt)
        requests = response.get("reconciliation_requests")
        if isinstance(requests, list):
            for request in requests:
                if not isinstance(request, Mapping):
                    continue
                self._enqueue_reconciliation(request)
            self._reconciliation_event.set()

    async def _recover_startup(self) -> None:
        self._fill_recovery_queue_from_journal()
        if self.execution.pending_results(limit=1):
            self._outbox_event.set()
        if self._reconciliation_queue or self.execution.journal.list_restart_recovery_references(
            limit=1
        ):
            self._reconciliation_event.set()
        await self._schedule_recovery_queue()

    async def _schedule_recovery_queue(self) -> None:
        self._fill_recovery_queue_from_journal()
        attempts = len(self._recovery_queue)
        for _ in range(attempts):
            if self.active_task_count >= self.max_concurrent_tasks:
                return
            attempt_id, attempt = self._recovery_queue.popitem(last=False)
            outcome = await self._schedule_attempt(attempt)
            if outcome == "capacity":
                self._recovery_queue[attempt_id] = attempt
                self._recovery_queue.move_to_end(attempt_id, last=False)
                return

    def _fill_recovery_queue_from_journal(self) -> int:
        """Hydrate one bounded fair page of restart-safe intents."""

        available = self.hot_queue_identity_limit - len(self._recovery_queue)
        if available <= 0:
            return 0
        page_limit = max(1, min(self.outbox_batch_size, available))
        references = self.execution.journal.list_restart_recovery_references(
            limit=page_limit,
            after=self._recovery_cursor,
        )
        if not references and self._recovery_cursor is not None:
            self._recovery_cursor = None
            references = self.execution.journal.list_restart_recovery_references(
                limit=page_limit
            )
        added = 0
        last_examined: Mapping[str, Any] | None = None
        for reference in references:
            last_examined = reference
            record = self.execution.journal.get_restart_recovery(
                str(reference["attempt_id"])
            )
            if record is None:
                continue
            if str(record.get("recovery_action") or "") != RECOVERY_EXECUTE_INTENT:
                continue
            if self._enqueue_recovery_attempt(self._attempt_from_recovery(record)):
                added += 1
        if last_examined is not None:
            self._recovery_cursor = (
                float(last_examined["updated_at"]),
                str(last_examined["attempt_id"]),
            )
        return added

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
        transport_correlation = edge_transport_for_attempt(
            correlation, str(record["attempt_id"])
        )
        if transport_correlation:
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
        active_attempt_ids = set(self._execution_tasks)
        records: list[dict[str, Any]] = []
        selected: set[tuple[str, str, int]] = set()
        if self.outbox_batch_size == 1:
            queue_budget = 1 if self._reconciliation_queue_first else 0
            self._reconciliation_queue_first = not self._reconciliation_queue_first
        else:
            queue_budget = max(1, self.outbox_batch_size // 2)

        def append_queued(limit: int) -> None:
            if limit <= 0:
                return
            for identity, compact in self._reconciliation_queue.items():
                if len(records) >= limit:
                    break
                if identity in selected:
                    continue
                if str(compact.get("attempt_id") or "") in active_attempt_ids:
                    continue
                if self._retry_is_deferred(self._reconciliation_retry_state, identity):
                    continue
                records.append(self._hydrate_reconciliation_record(compact))
                selected.add(identity)

        append_queued(queue_budget)
        remaining = self.outbox_batch_size - len(records)
        if remaining <= 0:
            return records
        references = self.execution.journal.list_restart_recovery_references(
            limit=max(remaining * 2, remaining),
            after=self._reconciliation_cursor,
        )
        if not references and self._reconciliation_cursor is not None:
            self._reconciliation_cursor = None
            references = self.execution.journal.list_restart_recovery_references(
                limit=max(remaining * 2, remaining)
            )
        last_examined: Mapping[str, Any] | None = None
        for reference in references:
            if len(records) >= self.outbox_batch_size:
                break
            last_examined = reference
            if str(reference.get("attempt_id") or "") in active_attempt_ids:
                continue
            identity = _pending_reconciliation_key(reference)
            if identity in selected or identity in self._reported_recovery:
                continue
            if self._retry_is_deferred(self._reconciliation_retry_state, identity):
                continue
            record = self._hydrate_reconciliation_record(reference)
            if str(record.get("recovery_action") or "") in {
                RECOVERY_EXECUTE_INTENT,
                RECOVERY_UPLOAD_RESULT,
            }:
                continue
            records.append(record)
            selected.add(identity)
        if last_examined is not None:
            self._reconciliation_cursor = (
                float(last_examined["updated_at"]),
                str(last_examined["attempt_id"]),
            )
        append_queued(self.outbox_batch_size)
        return records

    def _enqueue_recovery_attempt(self, attempt: Mapping[str, Any]) -> bool:
        attempt_id = str(attempt.get("attempt_id") or "").strip()
        if not attempt_id:
            return False
        if attempt_id in self._recovery_queue or attempt_id in self._execution_tasks:
            return False
        if len(self._recovery_queue) >= self.hot_queue_identity_limit:
            return False
        self._recovery_queue[attempt_id] = dict(attempt)
        return True

    def _enqueue_reconciliation(self, record: Mapping[str, Any]) -> bool:
        compact = _compact_reconciliation_record(record)
        identity = _pending_reconciliation_key(compact)
        if not identity[0] or not identity[1]:
            return False
        if identity in self._reported_recovery:
            return False
        existing = self._reconciliation_queue.get(identity)
        if existing is None:
            if len(self._reconciliation_queue) >= self.hot_queue_identity_limit:
                return False
            self._reconciliation_queue[identity] = compact
            return True
        else:
            existing.update(compact)
            return False

    def _hydrate_reconciliation_record(
        self, compact: Mapping[str, Any]
    ) -> dict[str, Any]:
        lookup = self.execution.reconciliation_lookup(
            operation_id=str(compact.get("operation_id") or ""),
            attempt_id=str(compact.get("attempt_id") or ""),
        )
        if lookup.get("found") is False:
            return {**dict(compact), "found": False}
        return {**dict(compact), **dict(lookup)}

    def _record_reported_recovery(
        self, identity: tuple[str, str, int]
    ) -> None:
        self._reported_recovery[identity] = None
        self._reported_recovery.move_to_end(identity)
        while len(self._reported_recovery) > MAX_REPORTED_RECOVERY_IDENTITIES:
            self._reported_recovery.popitem(last=False)

    async def _heartbeat_loop(self) -> None:
        await self._periodic_loop(
            "heartbeat",
            self.heartbeat_once,
            self.heartbeat_interval_seconds,
            wake_event=self._heartbeat_event,
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
        *,
        wake_event: asyncio.Event | None = None,
    ) -> None:
        while not self._stop_event.is_set():
            if wake_event is not None:
                wake_event.clear()
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
                if wake_event is None:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    await _wait_for_event_or_stop(
                        wake_event,
                        self._stop_event,
                        remaining,
                    )

    def _record_background_error(self, source: str, error: BaseException) -> None:
        category = type(error).__name__
        status = (
            f"http_{error.status_code}"
            if isinstance(error, EdgeV2HttpError) and error.status_code is not None
            else "runtime"
        )
        # Journal exceptions contain stable protocol reason codes, not user
        # content.  Retaining that code makes production reconciliation faults
        # diagnosable without exposing command output or payloads.
        reason = str(error) if isinstance(error, EdgeJournalError) else ""
        diagnostic = f"{source}: {category}:{status}"
        if reason:
            diagnostic = f"{diagnostic}:{reason}"
        self._background_errors.append(diagnostic)
        key = (source, category, status)
        now = time.monotonic()
        state = self._error_log_state.get(key)
        if state is None:
            state = {"last_logged_at": 0.0, "suppressed": 0}
            self._error_log_state[key] = state
            while len(self._error_log_state) > MAX_ERROR_LOG_KEYS:
                self._error_log_state.popitem(last=False)
        else:
            self._error_log_state.move_to_end(key)
        last_logged = float(state["last_logged_at"])
        if not last_logged or now - last_logged >= 60.0:
            suppressed = int(state["suppressed"])
            if suppressed:
                logger.warning(
                    "Edge control failure in %s repeated %d additional times",
                    source,
                    suppressed,
                )
            logger.error(
                "Edge control failure in %s: category=%s status=%s reason=%s",
                source,
                category,
                status,
                reason or "none",
            )
            state["last_logged_at"] = now
            state["suppressed"] = 0
        else:
            state["suppressed"] = int(state["suppressed"]) + 1
        if len(self._background_errors) > 256:
            del self._background_errors[:-256]

    @staticmethod
    def _retry_is_deferred(
        state: Mapping[Any, Mapping[str, float | int]], key: Any
    ) -> bool:
        retry = state.get(key)
        return bool(
            retry
            and float(retry.get("next_retry_at") or 0.0) > time.monotonic()
        )

    def _defer_retry(
        self,
        state: OrderedDict[Any, dict[str, float | int]],
        key: Any,
    ) -> None:
        previous = state.get(key) or {}
        failures = int(previous.get("failures") or 0) + 1
        delay = min(60.0, float(2 ** min(failures - 1, 6)))
        state[key] = {
            "failures": failures,
            "next_retry_at": time.monotonic() + delay,
        }
        state.move_to_end(key)
        while len(state) > self.hot_queue_identity_limit:
            state.popitem(last=False)

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
            close_transport = getattr(self.transport, "aclose", None)
            if callable(close_transport):
                try:
                    await close_transport()
                except Exception as error:
                    self._record_background_error("shutdown_transport", error)
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
    transport = edge_transport_for_attempt(
        correlation if isinstance(correlation, Mapping) else {},
        str(attempt.get("attempt_id") or ""),
    )
    if transport.get("contract_hash"):
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


def _pending_reconciliation_key(
    record: Mapping[str, Any],
) -> tuple[str, str, int]:
    try:
        fencing_token = int(record.get("fencing_token") or 0)
    except (TypeError, ValueError):
        fencing_token = 0
    return (
        str(record.get("operation_id") or ""),
        str(record.get("attempt_id") or ""),
        fencing_token,
    )


def _compact_reconciliation_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Retain fences and disposition facts, never result/report object graphs."""

    fields = (
        "operation_id",
        "attempt_id",
        "machine_id",
        "edge_generation",
        "fencing_token",
        "current_fencing_token",
        "state",
        "revision",
        "expected_revision",
        "contract_hash",
        "required_contract_hash",
        "recovery_action",
        "found",
        "effect_started",
        "needs_reconciliation",
        "reason",
    )
    return {
        key: record[key]
        for key in fields
        if key in record and record[key] not in (None, "")
    }


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
    "PersistentEdgeV2Transport",
    "UrllibEdgeV2Transport",
    "edge_contract_metadata",
    "enroll_edge_v2",
    "create_edge_v2_runner",
    "http_post_json",
    "normalize_edge_v2_profile",
]
