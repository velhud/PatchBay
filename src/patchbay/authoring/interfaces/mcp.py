"""Author-only MCP catalog with safe public qualification projection."""
from __future__ import annotations

from .mcp_legacy import *  # noqa: F401,F403
from . import mcp_legacy as _legacy


class MCP(_legacy.MCP):
    """Preserve the independent Author transport while hiding private identity state."""

    def call(self, role: str, name: str, arguments: dict) -> dict:
        result = super().call(role, name, arguments)
        if name != "author_capabilities" or not isinstance(result, dict):
            return result
        result = {key: value for key, value in result.items() if key != "instance_id"}
        qualification = result.get("qualification")
        if not isinstance(qualification, dict) or "instance_id" not in qualification:
            return result
        public = dict(result)
        public["qualification"] = {
            key: value for key, value in qualification.items() if key != "instance_id"
        }
        return public
