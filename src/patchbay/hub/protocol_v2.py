"""Hub V2 adapter with independently assembled current manager instructions."""
from __future__ import annotations
from .protocol_v2_legacy import *  # noqa: F401,F403
from . import protocol_v2_legacy as _legacy
from patchbay.protocol.manager_instructions import assemble

HUB_V2_INSTRUCTIONS = assemble("hub")
_legacy.HUB_V2_INSTRUCTIONS = HUB_V2_INSTRUCTIONS
