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
    """A bounded gate with separate ordinary and authorized urgent capacity.

    Writes leave room for reads; ordinary reads also leave an urgent slot in
    the default gate. Urgent requests have their own concurrency and rate
    bounds. The semantic classifier validates intent without dispatching it;
    final route authorization is still required. Queue waits yield 429 on
    overload and never impose a deadline on admitted domain work.

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
                 semantic_kind: Callable[[dict, bytes | None], str] | None = None,
                 classification_bytes: int = 65536,
                 classification_timeout: float = 0.1,
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
        self.semantic_kind = semantic_kind
        self.classification_bytes = classification_bytes
        self.classification_timeout = classification_timeout
        if classification_bytes < 1 or classification_timeout <= 0:
            raise ValueError('Invalid bounded admission classification budget')
        self._classifying = 0
        self._classification_tasks = set()
        self.clock = clock
        # Preserve a small urgent share against ordinary reads as well as writes.
        # Tiny custom gates retain their existing read reservation.
        self.urgent_reserved = 1 if max_concurrency > 2 and reserved_control else 0
        self.control_capacity = max(1, reserved_control)

        self._condition = asyncio.Condition()
        self._waiters: deque[_Waiter] = deque()
        self._active = 0
        self._active_writes = 0
        self._active_controls = 0
        self._active_by_principal: dict[str, int] = {}
        self._waiting_by_principal: dict[str, int] = {}
        self._rate_buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._control_rate_buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._control_global = _RateBucket(tokens=10.0, updated=self.clock())

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        request_id = self._request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        kind, receive = await self._classify(scope, receive)
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

    async def _classify(self, scope, receive):
        fallback = self._kind(scope)
        if self.semantic_kind is None or self._classifying >= self.max_concurrency:
            return fallback if self.semantic_kind is None else 'write', receive
        self._classifying += 1
        messages = []

        async def classify():
            try:
                raw = None
                if scope.get('method', 'GET').upper() == 'POST':
                    length = next((v for k,v in scope.get('headers',[]) if k.lower()==b'content-length'), None)
                    if length is not None and (not length.isdigit() or int(length) > self.classification_bytes):
                        return 'write'
                    async def prefix():
                        size = 0
                        while True:
                            message = await receive()
                            messages.append(message)
                            if message['type'] != 'http.request':
                                return None
                            size += len(message.get('body', b''))
                            if size > self.classification_bytes:
                                return None
                            if not message.get('more_body', False):
                                return b''.join(m.get('body',b'') for m in messages)
                    raw = await asyncio.wait_for(prefix(), self.classification_timeout)
                    if raw is None:
                        return 'write'
                kind = await asyncio.to_thread(self.semantic_kind, scope, raw)
                return kind if kind in {'read','control','write'} else 'write'
            except Exception:
                # Classification grants no effects. Invalid/large/incomplete
                # input keeps ordinary limits and the normal route validation.
                return 'write'
            finally:
                self._classifying -= 1

        # A disconnected caller cannot release a slot while its bounded
        # classifier is still doing authentication/readback in a worker thread.
        task = asyncio.create_task(classify())
        self._classification_tasks.add(task)
        task.add_done_callback(self._classification_tasks.discard)
        kind = await asyncio.shield(task)
        async def replay():
            return messages.pop(0) if messages else await receive()
        return kind, replay

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

    def _rate_limit(self, principal: str | None, kind: str = 'write') -> int | None:
        if principal is None or self.rate_per_minute is None:
            return None
        now = self.clock()
        ledger = self._control_rate_buckets if kind == 'control' else self._rate_buckets
        burst = 5 if kind == 'control' else self.rate_burst
        rate = min(self.rate_per_minute, 30) if kind == 'control' else self.rate_per_minute
        if kind == 'control':
            global_bucket = self._control_global
            global_bucket.tokens = min(10.0, global_bucket.tokens + max(0.0, now-global_bucket.updated))
            global_bucket.updated = now
            if global_bucket.tokens < 1:
                return 1
        bucket = ledger.get(principal)
        if bucket is None:
            if len(ledger) >= self.max_principals:
                evicted = False
                for old in list(ledger):
                    if self._active_by_principal.get(old, 0) == 0 and self._waiting_by_principal.get(old, 0) == 0:
                        del ledger[old]
                        evicted = True
                        break
                if not evicted:
                    # Active principal state is bounded by max_concurrency; a
                    # config check ensures space exists for a new active key.
                    return 1
            bucket = _RateBucket(tokens=float(burst), updated=now)
            ledger[principal] = bucket
        else:
            ledger.move_to_end(principal)
        refill = rate / 60.0
        bucket.tokens = min(float(burst), bucket.tokens + max(0.0, now - bucket.updated) * refill)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            if kind == 'control':
                self._control_global.tokens -= 1.0
            return None
        return max(1, int((1.0 - bucket.tokens) / refill + 0.999999))

    def _can_use(self, waiter: _Waiter) -> bool:
        if self._active >= self.max_concurrency:
            return False
        if waiter.kind == 'control' and self._active_controls >= self.control_capacity:
            return False
        if waiter.kind != 'control' and self._active-self._active_controls >= self.max_concurrency-self.urgent_reserved:
            return False
        if waiter.kind == "write" and self._active_writes >= self.write_capacity:
            return False
        principal_limit = self.max_per_principal + {'write':0,'read':1,'control':2}[waiter.kind]
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
            retry_after = self._rate_limit(principal, kind)
            if retry_after is not None:
                return False, retry_after

            if not self._waiters and self._can_use(waiter):
                self._active += 1
                if kind == "write":
                    self._active_writes += 1
                if kind == 'control':
                    self._active_controls += 1
                if principal:
                    self._active_by_principal[principal] = self._active_by_principal.get(principal, 0) + 1
                return True, 0

            write_pending_limit = self.max_pending - self.reserved_pending
            if (len(self._waiters) >= self.max_pending
                    or (kind != 'control' and len(self._waiters) >= max(0,self.max_pending-self.urgent_reserved))
                    or (kind == "write" and len(self._waiters) >= write_pending_limit)
                    or (principal and self._waiting_by_principal.get(principal, 0) >= self.max_pending_per_principal + (1 if kind == 'control' else 0))):
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
            if kind == 'control':
                self._active_controls += 1
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
            if kind == 'control':
                self._active_controls = max(0, self._active_controls - 1)
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
