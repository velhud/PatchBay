"""ASGI-only tests for bounded ingress; no external services are used."""
from __future__ import annotations

import asyncio
import hashlib
import json

from patchbay.authoring.interfaces.ingress import IngressAdmission


def _header(response, name: bytes) -> bytes | None:
    return dict(response["headers"]).get(name)


async def _request(app, *, method="GET", path="/api/ping", headers=()):
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": list(headers),
             "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return {"status": start["status"], "headers": start["headers"], "body": body, "scope": scope}


def test_saturated_uploads_do_not_block_reserved_recovery_read_and_return_correlation_id():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def app(scope, receive, send):
            if scope["path"].endswith("/packages"):
                entered.set()
                await release.wait()
            body = b'{"ok":true}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"x-request-id", b"app-value-must-not-win")]})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        gate = IngressAdmission(app, max_concurrency=2, max_pending=2, queue_timeout=.03,
                                reserved_control=1, reserved_pending=1, rate_per_minute=None)
        first_task = asyncio.create_task(_request(gate, method="POST", path="/api/tasks/a/packages",
                                                  headers=[(b"authorization", b"Bearer secret-token"),
                                                           (b"x-request-id", b"accepted-request-17")]))
        await asyncio.wait_for(entered.wait(), 1)

        # The reserved read/control slot remains live while the write slot is occupied.
        recovery = await _request(gate, path="/api/tasks/task/recovery", headers=[(b"authorization", b"Bearer secret-token")])
        assert recovery["status"] == 200

        started = asyncio.get_running_loop().time()
        busy = await _request(gate, method="POST", path="/api/tasks/b/packages",
                              headers=[(b"x-request-id", b"overloaded-request-2")])
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < .5
        assert busy["status"] == 429
        assert _header(busy, b"x-request-id") == b"overloaded-request-2"
        assert _header(busy, b"retry-after") == b"2"
        body = json.loads(busy["body"])
        assert body["error"]["code"] == "INGRESS_BUSY"
        assert body["error"]["details"]["request_id"] == "overloaded-request-2"
        assert body["error"]["details"]["backoff_jitter_ms"] == {"min": 250, "max": 1000}

        release.set()
        accepted = await first_task
        assert accepted["status"] == 200
        assert _header(accepted, b"x-request-id") == b"accepted-request-17"

        generated = await _request(gate, headers=[(b"x-request-id", b"../unsafe\nvalue")])
        generated_id = _header(generated, b"x-request-id").decode()
        assert len(generated_id) == 32 and all(char in "0123456789abcdef" for char in generated_id)
        assert generated["scope"]["state"]["request_id"] == generated_id

    asyncio.run(scenario())


def test_per_principal_rate_window_resets_and_ledger_is_bounded_and_opaque():
    async def scenario():
        now = [100.0]

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        def principal(scope):
            for key, value in scope["headers"]:
                if key == b"x-principal":
                    return value.decode()
            return None

        gate = IngressAdmission(app, max_concurrency=2, max_pending=1, reserved_control=1,
                                reserved_pending=1, rate_per_minute=60, rate_burst=1,
                                max_principals=2, principal_getter=principal,
                                clock=lambda: now[0])
        alice = [(b"x-principal", b"alice"), (b"x-request-id", b"alice-1")]
        bob = [(b"x-principal", b"bob")]
        charlie = [(b"x-principal", b"charlie")]

        assert (await _request(gate, headers=alice))["status"] == 200
        limited = await _request(gate, headers=alice)
        assert limited["status"] == 429
        assert json.loads(limited["body"])["error"]["code"] == "INGRESS_BUSY"
        assert (await _request(gate, headers=bob))["status"] == 200

        now[0] += 1.1
        assert (await _request(gate, headers=alice))["status"] == 200
        assert (await _request(gate, headers=charlie))["status"] == 200
        assert len(gate._rate_buckets) == 2
        assert hashlib.sha256(b"alice").hexdigest() in gate._rate_buckets
        assert b"alice" not in repr(gate._rate_buckets).encode()

    asyncio.run(scenario())


def test_same_principal_cannot_monopolize_write_slots_or_pending_queue():
    async def scenario():
        entered = {name: asyncio.Event() for name in ("first", "same-principal", "other-principal")}
        release = {name: asyncio.Event() for name in entered}

        async def app(scope, receive, send):
            name = scope["path"].split("/")[-1]
            entered[name].set()
            await release[name].wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        def principal(scope):
            return next((value.decode() for key, value in scope["headers"] if key == b"x-principal"), None)

        gate = IngressAdmission(app, max_concurrency=3, max_pending=3, queue_timeout=.2,
                                reserved_control=1, reserved_pending=1, max_per_principal=1,
                                max_pending_per_principal=1, rate_per_minute=None,
                                principal_getter=principal)
        first = asyncio.create_task(_request(gate, method="POST", path="/write/first",
                                             headers=[(b"x-principal", b"owner-a")]))
        await asyncio.wait_for(entered["first"].wait(), 1)
        same = asyncio.create_task(_request(gate, method="POST", path="/write/same-principal",
                                            headers=[(b"x-principal", b"owner-a")]))
        await asyncio.sleep(.01)
        other = asyncio.create_task(_request(gate, method="POST", path="/write/other-principal",
                                             headers=[(b"x-principal", b"owner-b")]))
        await asyncio.wait_for(entered["other-principal"].wait(), 1)
        assert not entered["same-principal"].is_set()

        release["other-principal"].set()
        assert (await other)["status"] == 200
        release["first"].set()
        await asyncio.wait_for(entered["same-principal"].wait(), 1)
        release["same-principal"].set()
        assert (await first)["status"] == 200
        assert (await same)["status"] == 200

    asyncio.run(scenario())
