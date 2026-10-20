"""Hub V2 protocol adapter with the reconciled explicit worker policy."""
from __future__ import annotations

from .protocol_v2_legacy import *  # noqa: F401,F403
from . import protocol_v2_legacy as _legacy


_OLD_POLICY = """Model routing is advisory and patchbay_worker_options is authoritative. Prefer
Spark for bounded small lanes with GPT-5.4 Mini as immediate fallback, Luna for
compact standard work, Terra for most substantial work, and Sol for difficult or
high-authority judgment. Sol medium is the normal default; use higher effort only
when difficulty or consequences justify it."""
_NEW_POLICY = "Call patchbay_worker_options to inspect catalog availability. " + "Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker."


if _OLD_POLICY not in _legacy.HUB_V2_INSTRUCTIONS:
    raise RuntimeError("Legacy Hub V2 model policy changed; reconcile the replacement explicitly")

HUB_V2_INSTRUCTIONS = _legacy.HUB_V2_INSTRUCTIONS.replace(_OLD_POLICY, _NEW_POLICY)
_legacy.HUB_V2_INSTRUCTIONS = HUB_V2_INSTRUCTIONS
