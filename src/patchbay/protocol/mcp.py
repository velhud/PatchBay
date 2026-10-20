"""MCP protocol implementation with the reconciled manager/worker policy."""
from __future__ import annotations

from .mcp_legacy import *  # noqa: F401,F403
from . import mcp_legacy as _legacy


_OLD_DEFAULTS = (
    "Use GPT-5.6 Luna as the default compact standard worker, GPT-5.6 Terra as the default "
    "serious worker for most substantial lanes, and GPT-5.6 Sol for highest-authority, "
    "creative, unresolved, sensitive, or final-judgment work."
)
_NEW_DEFAULTS = (
    "Use GPT-5.6 Luna as the default compact standard worker and, operationally, explicitly "
    "pass model='gpt-6-luna' with reasoning_effort='high' for ordinary worker starts when the "
    "installed catalog supports it. The prior phrase 'GPT-5.6 Terra as the default serious "
    "worker' is superseded: Terra is a deliberate escalation for work that materially needs "
    "broader context or authority. Use GPT-5.6 Sol for highest-authority, creative, unresolved, "
    "sensitive, or final-judgment work."
)
_OLD_SMALL_LANE = (
    "For every bounded small-worker assignment that either Spark or GPT-5.4 Mini could handle, "
    "choose Spark first because it is dramatically faster and uses a separate preview quota. "
    "If Spark is unavailable, its quota is depleted, or its smaller context is insufficient, "
    "immediately continue or retry the same assignment with GPT-5.4 Mini."
)
_NEW_SMALL_LANE = (
    "The superseded policy said 'choose Spark first' and to 'immediately continue or retry the "
    "same assignment with GPT-5.4 Mini'; do not follow either rule automatically. Spark and "
    "GPT-5.4 Mini are deliberate bounded fallbacks only when Luna is unavailable or evidence "
    "supports the smaller lane. Record the override and preserve the same worker brief."
)

if _OLD_DEFAULTS not in _legacy.SERVER_INSTRUCTIONS or _OLD_SMALL_LANE not in _legacy.SERVER_INSTRUCTIONS:
    raise RuntimeError("Legacy MCP worker policy changed; reconcile the replacement explicitly")

SERVER_INSTRUCTIONS = _legacy.SERVER_INSTRUCTIONS.replace(_OLD_DEFAULTS, _NEW_DEFAULTS).replace(
    _OLD_SMALL_LANE, _NEW_SMALL_LANE
)
_legacy.SERVER_INSTRUCTIONS = SERVER_INSTRUCTIONS
