"""Compatibility protocol using one assembled current manager policy."""
from __future__ import annotations
from .mcp_legacy import *  # noqa: F401,F403
from . import mcp_legacy as _legacy
from .manager_instructions import assemble

SERVER_INSTRUCTIONS = assemble("local")
_legacy.SERVER_INSTRUCTIONS = SERVER_INSTRUCTIONS
