"""Pro Request descriptors with explicit new-worker model policy."""
from __future__ import annotations

from copy import deepcopy

from .tool_surface_legacy import *  # noqa: F401,F403
from . import tool_surface_legacy as _legacy


PRO_REQUEST_TOOLS = deepcopy(_legacy.PRO_REQUEST_TOOLS)
for descriptor in PRO_REQUEST_TOOLS:
    if descriptor.get("name") != "codex_pro_request_dispatch":
        continue
    properties = descriptor["inputSchema"]["properties"]
    properties["model"] = {
        "type": "string",
        "enum": ["gpt-6-luna"],
        "description": (
            "Only gpt-6-luna is authorized for target=new_worker. Omission explicitly selects it."
        ),
    }
    properties["reasoning_effort"] = {
        "type": "string",
        "enum": ["high", "xhigh", "max"],
        "description": (
            "Optional reasoning effort for target=new_worker. Use only a level supported by the chosen model."
        ),
    }
    descriptor["description"] += (
        " For target=new_worker, execution explicitly uses gpt-6-luna and high effort by default; xhigh or max is allowed. No model fallback is permitted."
    )
    break

PRO_REQUEST_TOOL_NAMES = {tool["name"] for tool in PRO_REQUEST_TOOLS}
PRO_REQUEST_NON_IDEMPOTENT_TOOLS = set(_legacy.PRO_REQUEST_NON_IDEMPOTENT_TOOLS)
PRO_REQUEST_OPEN_WORLD_TOOLS = set(_legacy.PRO_REQUEST_OPEN_WORLD_TOOLS)

# The legacy installation function resolves these names from its own module
# globals at call time. Update that module so every existing caller receives
# the corrected catalog without a second installation implementation.
_legacy.PRO_REQUEST_TOOLS = PRO_REQUEST_TOOLS
_legacy.PRO_REQUEST_TOOL_NAMES = PRO_REQUEST_TOOL_NAMES
_legacy.PRO_REQUEST_NON_IDEMPOTENT_TOOLS = PRO_REQUEST_NON_IDEMPOTENT_TOOLS
_legacy.PRO_REQUEST_OPEN_WORLD_TOOLS = PRO_REQUEST_OPEN_WORLD_TOOLS
