"""Bounded, lossless ASGI request admission for the Author HTTP surface.

This middleware rejects excess ingress before route code starts. It never puts
a deadline around admitted application work: an accepted mutation keeps the
normal request/thread lifecycle, and the middleware does not retry it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_READ_METHODS = {b"GET", b"HEAD", b"OPTIONS"}
_CONTROL_PATHS = ("/api/control", "/api/cancel", "/api/inbox/")


@dataclass(eq=False)
class _Waiter:
    kind: str
    principal: str | None


@dataclass
class _RateBucket:
    tokens: float
    updated: float


class IngressAdmission:
    """A global concurrency gate with reserved read/control capacity.

    Write requests can consume at most ``max_concurrency - reserved_control``
    active slots. Reads and explicit recovery/control routes can use any
    remaining slot. The pending queue is bounded, with a portion reserved for
    reads/control; waiting is deliberately short and yields 429 on overload.

    ``principal_getter`` may read an authenticated principal from the ASGI
    scope (for example, middleware-populated ``scope['state']``). Without one,
    a bearer token is reduced to a one-way SHA-256 key. Raw credentials and
    principal strings are never retained in the rate-limit ledger.
    """

    def __init__(self, app, *, max_concurrency: int = 8, max_pending: int = 16,
                 queue_timeout: float = 0.25, reserved_control: int = 2,
                 reserved_pending: int = 2, max_per_principal: int = 2,
                 max_pending_per_principal: int = 4,
                 rate_per_minute: float | None = 60, rate_burst: int = 20,
                 max_principals: int = 4096,
                 principal_getter: Callable[[dict], str | None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if max_concurrency < 1 or not 0 <= reserved_control < max_concurrency:
            raise ValueError("reserved_control must leave at least one active request slot")
        if max_pending < 0 or not 0 <= reserved_pending <= max_pending:
            raise ValueError("reserved_pending must fit within max_pending")
        if queue_timeout < 0 or max_per_principal < 1 or max_pending_per_principal < 0:
            raise ValueError("invalid ingress queue or principal limits")
        if rate_per_minute is not None and rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive or disabled")
        if rate_burst < 1 or max_principals < max_concurrency:
            raise ValueError("rate burst must be positive and cache must cover active principals")

        self.app = app
        self.max_concurrency = max_concurrency
        self.max_pending = max_pending
        self.queue_timeout = queue_timeout
        self.write_capacity = max_concurrency - reserved_control
        self.reserved_pending = reserved_pending
        self.max_per_principal = max_per_principal
        self.max_pending_per_principal = max_pending_per_principal
        self.rate_per_minute = rate_per_minute
        self.rate_burst = rate_burst
        self.max_principals = max_principals
        self.principal_getter = principal_getter
        self.clock = clock

        self._condition = asyncio.Condition()
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self._active_writes = 0
        self._active_by_principal: dict[str, int] = {}
        self._waiting_by_principal: dict[str, int] = {}
        self._rate_buckets: OrderedDict[str, _RateBucket] = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        request_id = self._request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        kind = self._kind(scope)
        principal = self._principal(scope)
        admitted, retry_after = await self._acquire(kind, principal)
        if not admitted:
            return await self._busy(send, request_id, retry_after)

        async def identified_send(message):
            if message.get("type") == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, identified_send)
        finally:
            await self._release(kind, principal)

    @staticmethod
    def _kind(scope: dict) -> str:
        method = scope.get("method", "GET").encode("ascii", "ignore").upper()
        path = scope.get("path", "")
        if method in _SAFE_READ_METHODS:
            return "read"
        if path == "/api/control" or path.startswith(_CONTROL_PATHS):
            return "control"
        return "write"

    @staticmethod
    def _request_id(scope: dict) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                try:
                    candidate = value.decode("ascii")
                except UnicodeDecodeError:
                    break
                if _REQUEST_ID.fullmatch(candidate):
                    return candidate
                break
        return uuid.uuid4().hex

    def _principal(self, scope: dict) -> str | None:
        raw = None
        if self.principal_getter is not None:
            try:
                raw = self.principal_getter(scope)
            except Exception:
                raw = None
        else:
            for name, value in scope.get("headers", []):
                if name.lower() == b"authorization" and value.startswith(b"Bearer "):
                    token = value[7:]
                    if 0 < len(token) <= 1024:
                        raw = token
                    break
        if raw is None:
            return None
        if isinstance(raw, bytes):
            material = raw
        else:
            material = str(raw).encode("utf-8", "replace")
        return hashlib.sha256(material).hexdigest()

    def _rate_limit(self, principal: str | None) -> int | None:
        if principal is None or self.rate_per_minute is None:
            return None
        now = self.clock()
        bucket = self._rate_buckets.get(principal)
        if bucket is None:
            if len(self._rate_buckets) >= self.max_principals:
                evicted = False
                for old in list(self._rate_buckets):
                    if self._active_by_principal.get(old, 0) == 0 and self._waiting_by_principal.get(old, 0) == 0:
                        del self._rate_buckets[old]
                        evicted = True
                        break
                if not evicted:
                    # Active principal state is bounded by max_concurrency; a
                    # config check ensures space exists for a new active key.
                    return 1
            bucket = _RateBucket(tokens=float(self.rate_burst), updated=now)
            self._rate_buckets[principal] = bucket
        else:
            self._rate_buckets.move_to_end(principal)
        refill = self.rate_per_minute / 60.0
        bucket.tokens = min(float(self.rate_burst), bucket.tokens + max(0.0, now - bucket.updated) * refill)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return None
        return max(1, int((1.0 - bucket.tokens) / refill + 0.999999))

    def _can_use(self, waiter: _Waiter) -> bool:
        if self._active >= self.max_concurrency:
            return False
        if waiter.kind == "write" and self._active_writes >= self.write_capacity:
            return False
        principal_limit = self.max_per_principal + (1 if waiter.kind != "write" else 0)
        if waiter.principal and self._active_by_principal.get(waiter.principal, 0) >= principal_limit:
            return False
        return True

    def _is_first_eligible(self, waiter: _Waiter) -> bool:
        for queued in self._waiters:
            if self._can_use(queued):
                return queued is waiter
        return False

    async def _acquire(self, kind: str, principal: str | None) -> tuple[bool, int]:
        waiter = _Waiter(kind, principal)
        async with self._condition:
            retry_after = self._rate_limit(principal)
            if retry_after is not None:
                return False, retry_after

            if not self._waiters and self._can_use(waiter):
                self._active += 1
                if kind == "write":
                    self._active_writes += 1
                if principal:
                    self._active_by_principal[principal] = self._active_by_principal.get(principal, 0) + 1
                return True, 0

            write_pending_limit = self.max_pending - self.reserved_pending
            if (len(self._waiters) >= self.max_pending
                    or (kind == "write" and len(self._waiters) >= write_pending_limit)
                    or (principal and self._waiting_by_principal.get(principal, 0) >= self.max_pending_per_principal)):
                return False, 2

            self._waiters.append(waiter)
            if principal:
                self._waiting_by_principal[principal] = self._waiting_by_principal.get(principal, 0) + 1
            try:
                ready = lambda: self._is_first_eligible(waiter)
                if not ready():
                    await asyncio.wait_for(self._condition.wait_for(ready), timeout=self.queue_timeout)
            except asyncio.TimeoutError:
                self._remove_waiter(waiter)
                self._condition.notify_all()
                return False, 2
            except asyncio.CancelledError:
                self._remove_waiter(waiter)
                self._condition.notify_all()
                raise
            except BaseException:
                self._remove_waiter(waiter)
                self._condition.notify_all()
                raise
            self._remove_waiter(waiter)
            self._active += 1
            if kind == "write":
                self._active_writes += 1
            if principal:
                self._active_by_principal[principal] = self._active_by_principal.get(principal, 0) + 1
            self._condition.notify_all()
            return True, 0

    def _remove_waiter(self, waiter: _Waiter) -> None:
        with_sentinel = waiter in self._waiters
        if with_sentinel:
            self._waiters.remove(waiter)
        if waiter.principal and with_sentinel:
            remaining = self._waiting_by_principal.get(waiter.principal, 0) - 1
            if remaining > 0:
                self._waiting_by_principal[waiter.principal] = remaining
            else:
                self._waiting_by_principal.pop(waiter.principal, None)

    async def _release(self, kind: str, principal: str | None) -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            if kind == "write":
                self._active_writes = max(0, self._active_writes - 1)
            if principal:
                remaining = self._active_by_principal.get(principal, 0) - 1
                if remaining > 0:
                    self._active_by_principal[principal] = remaining
                else:
                    self._active_by_principal.pop(principal, None)
            self._condition.notify_all()

    async def _busy(self, send, request_id: str, retry_after: int) -> None:
        body = json.dumps({"error": {"code": "INGRESS_BUSY",
                                      "message": "Request admission is at capacity; retry with backoff",
                                      "details": {"request_id": request_id, "retry_after_seconds": retry_after,
                                                  "backoff_jitter_ms": {"min": 250, "max": 1000}}}},
                          separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": 429,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii")),
                                (b"cache-control", b"no-store"),
                                (b"retry-after", str(retry_after).encode("ascii")),
                                (b"x-request-id", request_id.encode("ascii"))]})
        await send({"type": "http.response.body", "body": body, "more_body": False})
