import json
import subprocess

import pytest

from patchbay.workers.model_options import (
    build_reasoning_config_override,
    validate_reasoning_effort,
    validate_worker_model,
    worker_option_menu,
)


def test_worker_option_menu_uses_codex_debug_models(monkeypatch, tmp_path):
    catalog = {
        "models": [
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "description": "Frontier model.",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast"},
                    {"effort": "high", "description": "Deep"},
                ],
                "visibility": "list",
                "supported_in_api": True,
                "priority": 10,
                "base_instructions": "must not be returned",
            }
        ]
    }

    def fake_run(cmd, **kwargs):
        if cmd == ["codex", "debug", "models"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(catalog), "")
        if cmd == ["codex", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, "codex-cli 0.test\n", "")
        raise AssertionError(cmd)

    monkeypatch.setattr("patchbay.workers.model_options.subprocess.run", fake_run)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = worker_option_menu({"power_tools": {"codex_home": str(codex_home)}}, model="gpt-5.5")

    assert result["source"] == "codex_debug_models"
    assert result["codex_version"] == "codex-cli 0.test"
    assert result["default_model"] == "gpt-5.5"
    assert result["selected_model"]["id"] == "gpt-5.5"
    guidance = result["model_selection_guidance"]
    assert "hard router" in guidance["summary"]
    ladder = {item["model_family"]: item for item in guidance["default_ladder"]}
    assert set(ladder) == {
        "Spark",
        "GPT-5.4 Mini",
        "GPT-5.4",
        "GPT-5.5",
        "GPT-5.6 Luna",
        "GPT-5.6 Terra",
        "GPT-5.6 Sol",
    }
    assert "Specialized ultra-fast worker" in ladder["Spark"]["role"]
    assert "quota pressure" in ladder["GPT-5.4 Mini"]["caveats"]
    assert "legacy serious-worker fallback" in ladder["GPT-5.4"]["role"]
    assert "Legacy frontier fallback" in ladder["GPT-5.5"]["role"]
    assert "Default compact standard worker" in ladder["GPT-5.6 Luna"]["role"]
    assert "Default serious worker" in ladder["GPT-5.6 Terra"]["role"]
    assert "Highest-authority worker" in ladder["GPT-5.6 Sol"]["role"]
    assert "verified correct result" in guidance["cost_rule"]
    assert "0.144.1 exposes ultra as a reasoning_effort" in guidance["ultra_note"]
    assert "explicit named PatchBay workers remain preferred" in guidance["ultra_note"]
    assert result["selected_model"]["reasoning_efforts"] == [
        {"effort": "low", "description": "Fast"},
        {"effort": "high", "description": "Deep"},
    ]
    assert "base_instructions" not in json.dumps(result)


def test_worker_option_validation_rejects_unsafe_values():
    assert validate_worker_model("gpt-5.5") == "gpt-5.5"
    assert validate_reasoning_effort("HIGH") == "high"
    assert validate_reasoning_effort("none") == "none"
    assert validate_reasoning_effort("max") == "max"
    assert validate_reasoning_effort("ULTRA") == "ultra"
    assert build_reasoning_config_override("xhigh") == 'model_reasoning_effort="xhigh"'
    assert build_reasoning_config_override("max") == 'model_reasoning_effort="max"'
    assert build_reasoning_config_override("ultra") == 'model_reasoning_effort="ultra"'

    with pytest.raises(ValueError, match="model"):
        validate_worker_model("gpt-5.5; rm -rf /")
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_reasoning_effort("maximum")
