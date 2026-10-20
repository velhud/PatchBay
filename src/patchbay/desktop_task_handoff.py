"""Private Codex app-server ownership handoff for Desktop task continuations.

The Desktop task bridge normally leaves archive state to the operator.  An
operator may explicitly opt a private alias into this adapter when the
configured Codex app-server socket is the private app-server used for the
adapter uses the versioned app-server JSON-RPC protocol over its Unix
WebSocket and never exposes the socket path or underlying thread id.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


logger = logging.getLogger(__name__)

DEFAULT_HANDOFF_TIMEOUT_MS = 5_000
MAX_HANDOFF_MESSAGE_BYTES = 1_000_000


class DesktopHandoffError(RuntimeError):
    """Safe classification for a failed private Desktop ownership handoff."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:512]
        super().__init__(code)


def _status_type(thread: Mapping[str, Any]) -> str:
    status = thread.get("status")
    return str(status.get("type") or "").strip() if isinstance(status, Mapping) else ""


class CodexDesktopAppServerHandoff:
    """Run the official app-server archive -> unarchive -> readiness sequence."""

    def __init__(
        self,
        *,
        connect_factory: Optional[Callable[..., Any]] = None,
        timeout_ms: int = DEFAULT_HANDOFF_TIMEOUT_MS,
    ) -> None:
        self.timeout_ms = max(100, min(int(timeout_ms), DEFAULT_HANDOFF_TIMEOUT_MS))
        self._connect_factory = connect_factory

    def _connect(self, socket_path: str) -> Any:
        factory = self._connect_factory
        if factory is not None:
            return factory(socket_path, self.timeout_ms / 1_000)
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - packaging/runtime guard
            raise DesktopHandoffError(
                "desktop_handoff_unavailable",
                detail="websockets dependency is not installed",
            ) from exc
        # compression=None is required by the Codex app-server control socket;
        # its protocol rejects a per-message-deflate extension negotiation.
        return websockets.unix_connect(
            socket_path,
            compression=None,
            open_timeout=self.timeout_ms / 1_000,
            close_timeout=self.timeout_ms / 1_000,
        )

    async def _request(
        self,
        websocket: Any,
        request_id: int,
        method: str,
        params: Mapping[str, Any],
        deadline: float,
    ) -> Any:
        await websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                },
                separators=(",", ":"),
            )
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise DesktopHandoffError("desktop_handoff_unavailable", detail="app-server response timed out")
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise DesktopHandoffError("desktop_handoff_unavailable", detail="app-server response timed out") from exc
            if isinstance(raw, bytes):
                if len(raw) > MAX_HANDOFF_MESSAGE_BYTES:
                    raise DesktopHandoffError("desktop_handoff_failed", detail="app-server response was too large")
                raw = raw.decode("utf-8", errors="replace")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_HANDOFF_MESSAGE_BYTES:
                raise DesktopHandoffError("desktop_handoff_failed", detail="invalid app-server response")
            try:
                message = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise DesktopHandoffError("desktop_handoff_failed", detail="invalid app-server JSON") from exc
            if not isinstance(message, Mapping) or message.get("id") != request_id:
                # Notifications and unrelated server messages are expected on
                # a shared app-server connection.  Keep waiting for our reply.
                continue
            error = message.get("error")
            if isinstance(error, Mapping):
                detail = str(error.get("message") or error.get("code") or "app-server request failed")
                lowered = detail.lower()
                if "active writer" in lowered or "already has an active writer" in lowered:
                    code = "active_writer"
                elif "archiv" in lowered:
                    code = "archived_thread"
                elif "not found" in lowered or "unknown thread" in lowered:
                    code = "desktop_task_not_found"
                else:
                    code = "desktop_handoff_failed"
                raise DesktopHandoffError(code, detail=detail)
            return message.get("result")

    async def prepare(self, socket_path: str, thread_id: str) -> None:
        """Release the Desktop writer and verify an idle/unloaded thread.

        The target identifiers are intentionally only used in private JSON-RPC
        messages.  Any protocol or connection detail is reduced to a safe
        error category before returning to the Desktop task client.
        """

        path = Path(socket_path)
        if not path.is_absolute() or not path.exists() or not path.is_socket():
            raise DesktopHandoffError("desktop_handoff_unavailable", detail="configured app-server socket is unavailable")
        if not thread_id:
            raise DesktopHandoffError("desktop_handoff_failed", detail="configured Desktop task id is empty")
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1_000
        try:
            async with self._connect(str(path)) as websocket:
                await self._request(
                    websocket,
                    1,
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "patchbay-desktop-bridge",
                            "version": "0.1",
                        }
                    },
                    deadline,
                )
                await websocket.send(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
                await self._request(websocket, 2, "thread/archive", {"threadId": thread_id}, deadline)
                unarchived = await self._request(
                    websocket,
                    3,
                    "thread/unarchive",
                    {"threadId": thread_id},
                    deadline,
                )
                if not isinstance(unarchived, Mapping) or not isinstance(unarchived.get("thread"), Mapping):
                    raise DesktopHandoffError("desktop_handoff_failed", detail="app-server unarchive response was incomplete")
                verified = await self._request(
                    websocket,
                    4,
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": False},
                    deadline,
                )
                thread = verified.get("thread") if isinstance(verified, Mapping) else None
                if not isinstance(thread, Mapping):
                    raise DesktopHandoffError("desktop_handoff_failed", detail="app-server readiness response was incomplete")
                if str(thread.get("id") or "") != thread_id:
                    raise DesktopHandoffError("desktop_handoff_failed", detail="app-server returned a different thread")
                status = _status_type(thread)
                if status not in {"idle", "notLoaded"}:
                    raise DesktopHandoffError("active_writer", detail=f"thread status is {status or 'unknown'}")
        except DesktopHandoffError:
            raise
        except asyncio.TimeoutError as exc:
            raise DesktopHandoffError("desktop_handoff_unavailable", detail="app-server handoff timed out") from exc
        except (OSError, ConnectionError) as exc:
            logger.debug("Desktop app-server connection failed: %s", type(exc).__name__)
            raise DesktopHandoffError("desktop_handoff_unavailable", detail="app-server connection failed") from exc
        except Exception as exc:
            logger.debug("Desktop app-server handoff failed: %s", type(exc).__name__)
            raise DesktopHandoffError("desktop_handoff_failed", detail="app-server handoff failed") from exc


async def prepare_desktop_task(socket_path: str, thread_id: str) -> None:
    """Convenience entry point used by the Desktop task client."""

    await CodexDesktopAppServerHandoff().prepare(socket_path, thread_id)


__all__ = [
    "CodexDesktopAppServerHandoff",
    "DEFAULT_HANDOFF_TIMEOUT_MS",
    "DesktopHandoffError",
    "prepare_desktop_task",
]
