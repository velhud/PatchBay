"""Safe Codex model/reasoning discovery for worker setup."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]


REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
MAX_MODEL_ID_CHARS = 160
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")


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
        "allows_custom_model_string": True,
        "worker_start_fields": {
            "model": "Optional string. Omit to use Codex default; otherwise pass one of the returned model ids or a current Codex model id.",
            "reasoning_effort": "Optional string: minimal, low, medium, high, or xhigh. Omit to use Codex/model default.",
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
    configured = config.get("power_tools", {}).get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(str(configured)).expanduser()


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
