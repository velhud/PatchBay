"""Safe Codex model discovery with the current explicit worker policy."""
from __future__ import annotations

from typing import Any, Dict

from .model_options_legacy import *  # noqa: F401,F403
from . import model_options_legacy as _legacy


WORKER_MODEL = "gpt-6-luna"
WORKER_EFFORTS = ("high", "xhigh", "max")


def worker_execution_policy(model=None, reasoning_effort=None):
    """Enforce the versioned operator policy without semantic model routing."""
    selected = validate_worker_model(model) or WORKER_MODEL
    effort = validate_reasoning_effort(reasoning_effort) or "high"
    if selected != WORKER_MODEL:
        raise ValueError(f"Luna-only worker policy requires model={WORKER_MODEL}; no model-family fallback is permitted")
    if effort not in WORKER_EFFORTS:
        raise ValueError("Luna-only worker policy requires reasoning_effort high, xhigh, or max")
    return selected, effort


def pin_execution_options(options):
    """Bind low-level jobs and resume paths to the same delegated-agent policy."""
    result = dict(options or {})
    effort = result.get("_worker_reasoning_effort") or result.get("reasoning_effort")
    overrides = []
    for override in (result.get("config_overrides") or []):
        key, separator, value = str(override).partition("=")
        if separator and key.strip() in {"model", "model_reasoning_effort"}:
            value = value.strip().strip('"\'')
            if key.strip() == "model":
                worker_execution_policy(value, effort)
            else:
                worker_execution_policy(result.get("model"), value)
                effort = value
        else:
            overrides.append(override)
    model, effort = worker_execution_policy(result.get("model"), effort)
    result["model"] = model
    overrides.append(build_reasoning_config_override(effort))
    result["config_overrides"] = overrides
    return result


MODEL_SELECTION_GUIDANCE: Dict[str, Any] = {
    "summary": (
        "Every delegated worker uses GPT-6 Luna explicitly. This is an operator authorization "
        "policy, not a hidden router. The installed Codex catalog reports availability; "
        "unavailability must block execution, never select another family or older Luna."
    ),
    "effective_default": {
        "model_family": "GPT-6 Luna", "model_id": WORKER_MODEL,
        "reasoning_effort": "high", "scope": "all delegated worker starts and continuations",
        "availability_rule": "Report unavailable Luna and stop; no alternate model or older Luna is permitted.",
    },
    "default_ladder": [{
        "model_family": "GPT-6 Luna", "role": "Required for every delegated worker.",
        "reasoning": "Use high by default, xhigh or max when appropriate.",
        "caveats": "No other model family or older Luna is permitted, including nested delegation.",
    }],
    "manager_rule": (
        "Use model='gpt-6-luna' and reasoning_effort='high'; xhigh or max may be selected. "
        "Never substitute another family or older Luna. Carry this rule into every worker "
        "brief and nested handoff. If Luna cannot run, report the blocker. The primary manager "
        "may use its own model; Author Mode uses no delegated author or recovery worker."
    ),
    "cost_rule": "Model availability and quota failures are blockers, never authority to substitute a model.",
    "ultra_note": "Luna supports up to max; ultra is not permitted for delegated workers.",
}

_legacy.MODEL_SELECTION_GUIDANCE = MODEL_SELECTION_GUIDANCE


def worker_option_menu(config, *, model=None, max_models=12, include_model_details=False):
    selected, _ = worker_execution_policy(model)
    # Do not mistake a synthetic custom model entry for observed availability.
    result = _legacy.worker_option_menu(config, model=selected, max_models=30,
                                      include_model_details=include_model_details)
    available = [item for item in result["models"] if item["id"] == selected]
    discovered = result.get("selected_model") or {}
    if not available and discovered.get("id") == selected and discovered.get("catalog_match") is not False:
        available = [discovered]
    result.update(default_model=WORKER_MODEL, default_reasoning_effort="high",
                  selected_model=available[0] if available else {},
                  selected_reasoning_effort="high", models=available,
                  model_count=len(available), models_truncated=False,
                  allows_custom_model_string=False, worker_model_available=bool(available),
                  worker_start_fields={"model": f"Only {WORKER_MODEL}; omission explicitly selects it.",
                                       "reasoning_effort": "high (default), xhigh, or max only."},
                  next_step="Start a Luna worker if available; otherwise report the availability blocker.")
    result["reasoning_efforts"] = [item for item in result["reasoning_efforts"] if item["effort"] in WORKER_EFFORTS] if available else []
    return result
