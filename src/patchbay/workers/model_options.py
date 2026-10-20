"""Safe Codex model discovery with the current explicit worker policy."""
from __future__ import annotations

from typing import Any, Dict

from .model_options_legacy import *  # noqa: F401,F403
from . import model_options_legacy as _legacy


MODEL_SELECTION_GUIDANCE: Dict[str, Any] = {
    "summary": (
        "The ordinary PatchBay worker lane is GPT-5.6 Luna, selected explicitly as "
        "model='gpt-6-luna' with reasoning_effort='high' when the installed Codex "
        "catalog supports that selector. This is an operator-visible policy, not a "
        "hidden router: call codex_worker_options, pass deliberate values, and record "
        "any fallback or escalation. The installed Codex catalog remains authoritative."
    ),
    "effective_default": {
        "model_family": "GPT-5.6 Luna",
        "model_id": "gpt-6-luna",
        "reasoning_effort": "high",
        "scope": "ordinary worker starts and new-worker Pro dispatch",
        "availability_rule": (
            "Use the exact selector when returned by the installed catalog. If the catalog "
            "uses a different Luna identifier, use that returned identifier. If Luna is "
            "unavailable, choose and report a deliberate evidence-based fallback."
        ),
    },
    "default_ladder": [
        {
            "model_family": "GPT-5.6 Luna",
            "role": (
                "Effective default for bounded and substantial implementation, investigation, "
                "testing, review, debugging, and coordination lanes."
            ),
            "reasoning": "Use high by default. Lower or raise effort only as a deliberate, reported exception.",
            "caveats": "Escalate only when the assignment materially needs broader authority, context, or synthesis.",
        },
        {
            "model_family": "GPT-5.6 Terra",
            "role": "Deliberate escalation for unusually broad, difficult, long-context, scientific, or architectural work.",
            "reasoning": "Use high or xhigh normally; reserve max/ultra for exceptional cases supported by the live catalog.",
            "caveats": "Terra is not the silent default for ordinary lanes.",
        },
        {
            "model_family": "GPT-5.6 Sol",
            "role": "Highest-authority escalation for unresolved, creative, sensitive, or final-judgment work.",
            "reasoning": "Use medium or high deliberately; deeper effort requires a concrete reason.",
            "caveats": "Do not consume Sol or deep Sol effort for routine work that Luna can verify correctly.",
        },
        {
            "model_family": "Spark",
            "role": (
                "Optional small-lane fallback for narrowly bounded reading, search, extraction, "
                "or simple checks when selected deliberately. It is not the effective default."
            ),
            "reasoning": "Use only an effort level returned as supported by the installed catalog.",
            "caveats": "Do not choose Spark first merely because older guidance preferred its speed or preview quota.",
        },
        {
            "model_family": "GPT-5.4 Mini",
            "role": "Optional compact fallback when Luna is unavailable or a bounded task has a documented reason to use it.",
            "reasoning": "Use medium or high for routine trusted fallback work.",
            "caveats": "Do not silently retry or reroute to Mini; report the fallback and preserve the worker brief.",
        },
        {
            "model_family": "GPT-5.4",
            "role": "Legacy compatibility or evidence-backed regression fallback.",
            "reasoning": "Use the effort supported by the live catalog and justified by the assignment.",
            "caveats": "Do not select it by age-based habit when Luna is available and suitable.",
        },
        {
            "model_family": "GPT-5.5",
            "role": "Legacy frontier compatibility or evidence-backed regression fallback.",
            "reasoning": "Use high or xhigh only when the fallback itself requires serious judgment.",
            "caveats": "Prefer the current Luna/Terra/Sol policy unless evidence favors GPT-5.5.",
        },
    ],
    "manager_rule": (
        "Call codex_worker_options once per relevant runtime/catalog state. For every ordinary "
        "new worker, explicitly pass model='gpt-6-luna' and reasoning_effort='high' when "
        "available. Follow-up messages keep the same effective settings unless the manager "
        "deliberately changes them. Terra and Sol are explicit escalations; Spark, GPT-5.4 "
        "Mini, GPT-5.4, and GPT-5.5 are explicit availability, compatibility, or evidence-backed "
        "fallbacks. Never hide model routing from the manager or fabricate catalog support."
    ),
    "cost_rule": (
        "Optimize expected subscription use to a verified correct result, but do not replace "
        "the explicit Luna/High default with an unreported cost router. Use the live Codex "
        "usage dashboard and catalog because availability, quotas, and supported efforts change."
    ),
    "ultra_note": (
        "Ultra may perform internal delegation and consume materially more tokens. Explicit named "
        "PatchBay workers remain preferred when visible lanes, independent reports, isolated "
        "worktrees, or controlled integration matter."
    ),
}

# Functions defined in the preserved implementation resolve this module-global in
# their original module, so patch that binding as well as the public re-export.
_legacy.MODEL_SELECTION_GUIDANCE = MODEL_SELECTION_GUIDANCE
