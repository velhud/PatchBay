"""Hub V2 protocol adapter with the reconciled explicit worker policy."""
from __future__ import annotations

from .protocol_v2_legacy import *  # noqa: F401,F403
from . import protocol_v2_legacy as _legacy


_OLD_POLICY = """Model routing is advisory and patchbay_worker_options is authoritative. Prefer
Spark for bounded small lanes with GPT-5.4 Mini as immediate fallback, Luna for
compact standard work, Terra for most substantial work, and Sol for difficult or
high-authority judgment. Sol medium is the normal default; use higher effort only
when difficulty or consequences justify it."""
_NEW_POLICY = """Model routing is advisory and patchbay_worker_options is authoritative. For
ordinary worker starts, explicitly select model='gpt-6-luna' with
reasoning_effort='high' when the installed catalog supports it. Keep that
effective model and effort on follow-up unless the manager deliberately changes
them. Terra and Sol are explicit escalations. Spark, GPT-5.4 Mini, GPT-5.4, and
GPT-5.5 are explicit availability, compatibility, or evidence-backed fallbacks;
never route to them silently."""

if _OLD_POLICY not in _legacy.HUB_V2_INSTRUCTIONS:
    raise RuntimeError("Legacy Hub V2 model policy changed; reconcile the replacement explicitly")

HUB_V2_INSTRUCTIONS = _legacy.HUB_V2_INSTRUCTIONS.replace(_OLD_POLICY, _NEW_POLICY)
_legacy.HUB_V2_INSTRUCTIONS = HUB_V2_INSTRUCTIONS
