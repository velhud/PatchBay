import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest

from patchbay.desktop_task_handoff import (
    CodexDesktopAppServerHandoff,
    DesktopHandoffError,
)


THREAD_ID = "00000000-0000-7000-8000-000000000001"


class FakeWebSocket:
    def __init__(self, *, status="notLoaded"):
        self.status = status
        self.sent = []
        self.responses = []

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("method") == "initialize":
            result = {}
        elif message.get("method") == "thread/archive":
            result = {}
        elif message.get("method") == "thread/unarchive":
            result = {"thread": {"id": THREAD_ID, "status": {"type": "idle"}}}
        elif message.get("method") == "thread/read":
            result = {
                "thread": {
                    "id": THREAD_ID,
                    "status": {"type": self.status},
                }
            }
        else:
            return
        self.responses.append(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}))

    async def recv(self):
        while not self.responses:
            await asyncio.sleep(0)
        return self.responses.pop(0)


class FakeConnection:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        return False


def unix_socket_path(tmp_path):
    path = Path(tempfile.mkdtemp(prefix="pb-")) / "app-server.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    return path, server


@pytest.mark.asyncio
async def test_app_server_handoff_uses_official_archive_unarchive_read_sequence(tmp_path):
    path, server = unix_socket_path(tmp_path)
    websocket = FakeWebSocket()
    handoff = CodexDesktopAppServerHandoff(
        connect_factory=lambda _path, _timeout: FakeConnection(websocket),
    )

    try:
        await handoff.prepare(str(path), THREAD_ID)
    finally:
        server.close()

    methods = [message.get("method") for message in websocket.sent if message.get("method")]
    assert methods == ["initialize", "initialized", "thread/archive", "thread/unarchive", "thread/read"]
    assert all(THREAD_ID in json.dumps(message) for message in websocket.sent if message.get("method", "").startswith("thread/"))


@pytest.mark.asyncio
async def test_app_server_handoff_rejects_active_thread_without_public_identifiers(tmp_path):
    path, server = unix_socket_path(tmp_path)
    websocket = FakeWebSocket(status="active")
    handoff = CodexDesktopAppServerHandoff(
        connect_factory=lambda _path, _timeout: FakeConnection(websocket),
    )

    try:
        with pytest.raises(DesktopHandoffError) as raised:
            await handoff.prepare(str(path), THREAD_ID)
    finally:
        server.close()

    assert raised.value.code == "active_writer"
    assert THREAD_ID not in str(raised.value)


@pytest.mark.asyncio
async def test_app_server_handoff_requires_a_private_unix_socket(tmp_path):
    handoff = CodexDesktopAppServerHandoff(connect_factory=lambda *_args: None)

    with pytest.raises(DesktopHandoffError) as raised:
        await handoff.prepare(str(tmp_path / "missing.sock"), THREAD_ID)

    assert raised.value.code == "desktop_handoff_unavailable"
