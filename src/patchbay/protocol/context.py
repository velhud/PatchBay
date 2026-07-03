"""Internal request context for MCP transport-aware tool handling."""
from __future__ import annotations

import hmac
import hashlib
from dataclasses import dataclass, field
from typing import Any, MutableMapping


ANONYMOUS_CLIENT_REF = "anonymous"


def make_client_ref(session_id: str | None, *, salt: str) -> str:
    """Return a public, non-reversible-ish client reference for a private session id."""
    if not session_id:
        return ANONYMOUS_CLIENT_REF
    digest = hmac.new(salt.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"client_{digest[:12]}"


@dataclass(frozen=True)
class RequestContext:
    """Per-request coordination data.

    `transport_session_id` is private transport state and must not be returned
    in public tool results. Use `client_ref` for public coordination hints.
    """

    transport_session_id: str | None = field(default=None, repr=False)
    client_ref: str = ANONYMOUS_CLIENT_REF
    owner_ref: str = ""
    owner_scope: str = ""
    client_label: str = ""
    tool_mode: str | None = None
    active_mcp_sessions: int | None = None
    session_data: MutableMapping[str, Any] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def anonymous(cls) -> "RequestContext":
        return cls()

    @classmethod
    def from_session(
        cls,
        session_id: str,
        session_data: MutableMapping[str, Any],
        *,
        salt: str,
        active_mcp_sessions: int | None = None,
    ) -> "RequestContext":
        return cls(
            transport_session_id=session_id,
            client_ref=make_client_ref(session_id, salt=salt),
            owner_ref=str(session_data.get("owner_ref") or ""),
            owner_scope=str(session_data.get("owner_scope") or ""),
            client_label=str(session_data.get("client_label") or ""),
            tool_mode=session_data.get("tool_mode"),
            active_mcp_sessions=active_mcp_sessions,
            session_data=session_data,
        )

    @property
    def has_transport_session(self) -> bool:
        return bool(self.transport_session_id)

    def public_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "client_ref": self.client_ref,
            "has_mcp_session": self.has_transport_session,
        }
        if self.client_label:
            data["client_label"] = self.client_label
        if self.owner_ref:
            data["owner_ref"] = self.owner_ref
        if self.owner_scope:
            data["owner_scope"] = self.owner_scope
        if self.tool_mode:
            data["tool_mode"] = self.tool_mode
        if self.active_mcp_sessions is not None:
            data["active_mcp_sessions"] = self.active_mcp_sessions
        return data
