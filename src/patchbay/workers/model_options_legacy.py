"""Safe Codex model/reasoning discovery for worker setup."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from patchbay.codex_home import resolve_codex_home

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]


REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MAX_MODEL_ID_CHARS = 160
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")


MODEL_SELECTION_GUIDANCE: Dict[str, Any] = {
    "summary": (
        "Choose the worker model by task complexity, context size, decision authority, and expected cost to a "
        "verified result. This is advisory guidance for intelligent delegation, not a hard router or prompt filter. "
        "The installed Codex catalog remains the authority for current availability."
    ),
    "default_ladder": [
        {
            "model_family": "Spark",
            "role": (
                "Preferred first-choice small worker for bounded reading, focused search, direct checks, simple edits, "
                "tests, documentation, extraction, and interactive exploration. Prefer it over GPT-5.4 Mini whenever "
                "the assignment fits because Spark is dramatically faster and uses a separate research-preview quota."
            ),
            "reasoning": "Use medium or high for ordinary compact work; use higher reasoning only when the brief is still small but judgment matters.",
            "caveats": (
                "Spark has a smaller context window and slightly lower published coding results than GPT-5.4 Mini. "
                "Do not force it onto work that needs broader context or stronger judgment. If Spark is unavailable, "
                "its preview quota is depleted, or its context is insufficient, fall back immediately to GPT-5.4 Mini."
            ),
        },
        {
            "model_family": "GPT-5.4 Mini",
            "role": (
                "Immediate fallback for the same bounded small-worker assignments when Spark is unavailable, quota-"
                "depleted, or context-constrained; also use it directly when a small task exceeds Spark's context or reliability."
            ),
            "reasoning": "Use medium or high for routine trusted worker tasks.",
            "caveats": "Between Spark and Mini, choose Spark first whenever it can handle the assignment; Mini is the automatic operational fallback.",
        },
        {
            "model_family": "GPT-5.4",
            "role": (
                "Stable legacy serious-worker fallback when GPT-5.6 Terra is unavailable or a known task-specific "
                "regression makes the older behavior preferable."
            ),
            "reasoning": "Use medium/high for most serious work; use xhigh when the task is hard enough to justify deeper thought.",
            "caveats": "GPT-5.6 Terra replaces GPT-5.4 as the normal serious-worker default.",
        },
        {
            "model_family": "GPT-5.5",
            "role": (
                "Legacy frontier fallback for known regressions, compatibility checks, or periods when GPT-5.6 is "
                "unavailable; it remains strong on long context, multimodal work, and selected tool evaluations."
            ),
            "reasoning": "Use high or xhigh for serious final/creative/authority work.",
            "caveats": "Prefer GPT-5.6 Terra for price-performance and GPT-5.6 Sol for highest authority unless evidence favors GPT-5.5.",
        },
        {
            "model_family": "GPT-5.6 Luna",
            "role": (
                "Default compact standard worker for bounded implementation, investigation, tests, review helpers, "
                "and high-volume team lanes that need substantially more judgment than Mini or Spark."
            ),
            "reasoning": "Use low/medium for routine work and high/xhigh when a compact lane still needs meaningful judgment.",
            "caveats": "Escalate long-context, difficult scientific, architectural, ambiguous, or final-authority work to Terra or Sol.",
        },
        {
            "model_family": "GPT-5.6 Terra",
            "role": (
                "Default serious worker for normal above-average repository work, multi-step analysis, implementation, "
                "debugging, verification, and most investigator/implementer/reviewer lanes."
            ),
            "reasoning": "Use medium/high normally, xhigh for hard work, max for the hardest single-agent work, and ultra only when the live catalog supports automatic internal delegation and that behavior is appropriate.",
            "caveats": "Use Sol when maximum authority, creativity, ambiguity resolution, or final judgment is worth the additional subscription use.",
        },
        {
            "model_family": "GPT-5.6 Sol",
            "role": (
                "Highest-authority worker for innovation, creative architecture, difficult synthesis, unresolved problems, "
                "sensitive or final judgment, and the hardest implementation or review lanes."
            ),
            "reasoning": (
                "Use medium as Sol's normal daily-driver effort. Above-medium effort is rarely necessary: use high only "
                "for materially difficult or high-consequence work, xhigh for genuinely hard problems, serious bug diagnosis, "
                "or sensitive development where mistakes are unusually costly, and max/ultra only as deliberate exceptional escalations."
            ),
            "caveats": (
                "Do not use Sol or deep Sol effort for every lane. Medium normally captures Sol's value; ultra may consume "
                "roughly 5-10x the tokens of medium depending on task difficulty. Delegate ordinary serious work to Terra and compact work to Luna."
            ),
        },
    ],
    "manager_rule": (
        "For worker teams, use GPT-5.6 Luna for compact standard lanes, GPT-5.6 Terra for the main serious lanes, "
        "and GPT-5.6 Sol at medium effort for final authority or unusually hard synthesis. Raise Sol above medium only "
        "for genuinely hard, serious-bug, sensitive, or otherwise high-consequence work where mistakes cost more. For every bounded small-worker assignment "
        "that either Spark or GPT-5.4 Mini could handle, choose Spark first for its speed and separate preview quota; "
        "fall back immediately to GPT-5.4 Mini when Spark is unavailable, depleted, or too context-constrained. Keep "
        "GPT-5.4/GPT-5.5 as availability or regression fallbacks."
    ),
    "cost_rule": (
        "Optimize expected subscription use to a verified correct result, not nominal cost per turn. Prefer the live "
        "Codex usage dashboard and catalog over hard-coded prices, because credits, included limits, and preview quotas change."
    ),
    "ultra_note": (
        "Codex CLI 0.144.1 exposes ultra as a reasoning_effort for supported models such as GPT-5.6 Terra and Sol. "
        "Ultra can perform automatic internal task delegation inside one Codex worker and may consume roughly 5-10x "
        "the tokens of medium depending on task difficulty. PatchBay accepts the value, "
        "but explicit named PatchBay workers remain preferred when the manager needs visible lanes, independent reports, "
        "separate worktrees, or controlled integration."
    ),
}


def validate_worker_model(value: Optional[str]) -> str:
    """Return a safe model id or an empty string for default Codex model."""
    model = str(value or "").strip()
    if not model:
        return ""
    if len(model) > MAX_MODEL_ID_CHARS or not MODEL_ID_PATTERN.match(model):
        raise ValueError("model must be a Codex model id without whitespace or shell metacharacters")
    return model


def validate_reasoning_effort(value: Optional[str]) -> str:
    """Return a safe Codex reasoning effort or an empty string for default effort."""
    effort = str(value or "").strip().lower()
    if not effort:
        return ""
    if effort not in REASONING_EFFORTS:
        allowed = ", ".join(REASONING_EFFORTS)
        raise ValueError(f"reasoning_effort must be one of: {allowed}")
    return effort


def build_reasoning_config_override(effort: str) -> str:
    effort = validate_reasoning_effort(effort)
    if not effort:
        return ""
    return f'model_reasoning_effort="{effort}"'


def worker_option_menu(
    config: Dict[str, Any],
    *,
    model: Optional[str] = None,
    max_models: int = 12,
    include_model_details: bool = False,
) -> Dict[str, Any]:
    """Return a bounded, public menu of Codex worker execution options."""
    selected_model_id = validate_worker_model(model)
    max_models = max(1, min(int(max_models or 12), 30))
    codex_config = _read_codex_config(config)
    catalog, source, error = _load_model_catalog(config)

    models = _sanitize_models(catalog, include_model_details=include_model_details)
    default_model = str(codex_config.get("model") or "")
    default_reasoning = validate_reasoning_effort(codex_config.get("model_reasoning_effort"))
    selected = _select_model(models, selected_model_id or default_model)
    reasoning_options = _reasoning_options_for(selected)
    effective_reasoning = (
        default_reasoning
        if default_reasoning and any(item["effort"] == default_reasoning for item in reasoning_options)
        else (selected or {}).get("default_reasoning_effort", "")
    )

    result = {
        "source": source,
        "codex_version": _codex_version(),
        "default_model": default_model,
        "default_reasoning_effort": default_reasoning,
        "selected_model": selected or _custom_model_entry(selected_model_id or default_model),
        "selected_reasoning_effort": effective_reasoning,
        "reasoning_efforts": reasoning_options,
        "models": models[:max_models],
        "model_count": len(models),
        "models_truncated": len(models) > max_models,
        "model_selection_guidance": MODEL_SELECTION_GUIDANCE,
        "allows_custom_model_string": True,
        "worker_start_fields": {
            "model": "Optional string. Omit to use Codex default; otherwise pass one of the returned model ids or a current Codex model id.",
            "reasoning_effort": "Optional string: none, minimal, low, medium, high, xhigh, max, or ultra. Omit to use Codex/model default; the selected model may support only a subset.",
        },
        "next_step": (
            "Call codex_worker_start with name, brief, optional workspace_mode, and optional model/reasoning_effort. "
            "Call codex_worker_message with model or reasoning_effort only when intentionally changing that worker's execution settings."
        ),
        "note": "Only bounded model metadata is returned; raw Codex config, catalog paths, prompts, and provider/auth data are not exposed.",
    }
    if error:
        result["catalog_error"] = error
    return result


def _load_model_catalog(config: Dict[str, Any]) -> tuple[list[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
    try:
        completed = subprocess.run(
            ["codex", "debug", "models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            payload = json.loads(completed.stdout)
            models = payload.get("models") if isinstance(payload, dict) else payload
            if isinstance(models, list):
                return [item for item in models if isinstance(item, dict)], "codex_debug_models", None
        debug_error = {
            "message": "codex debug models did not return a model catalog.",
            "exit_code": completed.returncode,
        }
    except Exception as error:
        debug_error = {"message": "Unable to run codex debug models.", "error_type": type(error).__name__}

    cache_path = _codex_home(config) / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        models = payload.get("models") if isinstance(payload, dict) else payload
        if isinstance(models, list):
            return [item for item in models if isinstance(item, dict)], "codex_models_cache", debug_error
    except Exception:
        pass

    return [], "unavailable", debug_error


def _sanitize_models(catalog: list[Dict[str, Any]], *, include_model_details: bool) -> list[Dict[str, Any]]:
    models: list[Dict[str, Any]] = []
    for item in catalog:
        slug = _safe_catalog_model_id(item.get("slug"))
        if not slug:
            continue
        visibility = str(item.get("visibility") or "").strip().lower()
        if visibility and visibility not in {"list", "default"}:
            continue
        model: Dict[str, Any] = {
            "id": slug,
            "display_name": _clip(item.get("display_name") or slug, 80),
            "default_reasoning_effort": _safe_catalog_reasoning_effort(item.get("default_reasoning_level")),
            "reasoning_efforts": _reasoning_options_from_model(item),
            "supported_in_api": bool(item.get("supported_in_api", True)),
            "priority": _safe_int(item.get("priority")),
        }
        description = _clip(item.get("description") or "", 220)
        if description:
            model["description"] = description
        if include_model_details:
            tiers = item.get("service_tiers")
            if isinstance(tiers, list):
                model["service_tiers"] = [
                    {
                        "id": _clip(tier.get("id") or "", 80),
                        "name": _clip(tier.get("name") or "", 80),
                        "description": _clip(tier.get("description") or "", 180),
                    }
                    for tier in tiers
                    if isinstance(tier, dict) and tier.get("id")
                ][:6]
        models.append(model)
    models.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("display_name") or item["id"])))
    return models


def _reasoning_options_from_model(item: Dict[str, Any]) -> list[Dict[str, str]]:
    raw_levels = item.get("supported_reasoning_levels") or []
    options: list[Dict[str, str]] = []
    for level in raw_levels:
        if isinstance(level, dict):
            effort = _safe_catalog_reasoning_effort(level.get("effort"))
            description = _clip(level.get("description") or "", 180)
        else:
            effort = _safe_catalog_reasoning_effort(level)
            description = ""
        if effort and effort not in {option["effort"] for option in options}:
            entry = {"effort": effort}
            if description:
                entry["description"] = description
            options.append(entry)
    return options


def _reasoning_options_for(model: Optional[Dict[str, Any]]) -> list[Dict[str, str]]:
    if model and model.get("reasoning_efforts"):
        return list(model["reasoning_efforts"])
    return [{"effort": effort} for effort in REASONING_EFFORTS]


def _select_model(models: list[Dict[str, Any]], model_id: str) -> Optional[Dict[str, Any]]:
    if not model_id:
        return models[0] if models else None
    expected = model_id.casefold()
    for model in models:
        if str(model.get("id") or "").casefold() == expected:
            return model
    return None


def _safe_catalog_model_id(value: Any) -> str:
    try:
        return validate_worker_model(value)
    except ValueError:
        return ""


def _safe_catalog_reasoning_effort(value: Any) -> str:
    try:
        return validate_reasoning_effort(value)
    except ValueError:
        return ""


def _custom_model_entry(model_id: str) -> Dict[str, Any]:
    if not model_id:
        return {}
    return {
        "id": model_id,
        "display_name": model_id,
        "catalog_match": False,
        "reasoning_efforts": [{"effort": effort} for effort in REASONING_EFFORTS],
        "note": "This model was not found in the bounded Codex catalog returned to PatchBay; Codex may still accept or reject it at launch.",
    }


def _codex_home(config: Dict[str, Any]) -> Path:
    return resolve_codex_home(config, os.environ)


def _read_codex_config(config: Dict[str, Any]) -> Dict[str, Any]:
    path = _codex_home(config) / "config.toml"
    try:
        data = path.read_bytes()
    except Exception:
        return {}
    if tomllib is not None:
        try:
            payload = tomllib.loads(data.decode("utf-8"))
            if isinstance(payload, dict):
                return {
                    "model": str(payload.get("model") or ""),
                    "model_reasoning_effort": str(payload.get("model_reasoning_effort") or ""),
                }
        except Exception:
            pass
    text = data.decode("utf-8", errors="replace")
    return {
        "model": _read_toml_string_key(text, "model"),
        "model_reasoning_effort": _read_toml_string_key(text, "model_reasoning_effort"),
    }


def _read_toml_string_key(text: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*[\"']([^\"']+)[\"']", text)
    return match.group(1).strip() if match else ""


def _codex_version() -> str:
    try:
        completed = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5)
        if completed.returncode == 0:
            return completed.stdout.strip()
    except Exception:
        pass
    return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clip(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."
