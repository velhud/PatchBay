"""MCP protocol with one current delegated-worker authorization policy."""
from __future__ import annotations
from .mcp_legacy import *  # noqa: F401,F403
from . import mcp_legacy as _legacy
from patchbay.workers.model_options import MODEL_SELECTION_GUIDANCE

_prefix = "3. Model choice should minimize expected subscription use"
_lines = _legacy.SERVER_INSTRUCTIONS.splitlines()
if sum(line.startswith(_prefix) for line in _lines) != 1:
    raise RuntimeError("Reconcile the manager model-policy paragraph before startup")
SERVER_INSTRUCTIONS = "\n".join(
    "3. Call codex_worker_options to inspect availability. " + MODEL_SELECTION_GUIDANCE["manager_rule"]
    if line.startswith(_prefix) else line for line in _lines
)
_legacy.SERVER_INSTRUCTIONS = SERVER_INSTRUCTIONS
