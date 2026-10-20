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

    result = worker_option_menu(
        {"power_tools": {"codex_home": str(codex_home)}},
        model="gpt-5.5",
    )

    assert result["source"] == "codex_debug_models"
    assert result["codex_version"] == "codex-cli 0.test"
    assert result["default_model"] == "gpt-5.5"
    assert result["selected_model"]["id"] == "gpt-5.5"
    guidance = result["model_selection_guidance"]
    assert "not a hidden router" in guidance["summary"]
    assert guidance["effective_default"] == {
        "model_family": "GPT-5.6 Luna",
        "model_id": "gpt-6-luna",
        "reasoning_effort": "high",
        "scope": "ordinary worker starts and new-worker Pro dispatch",
        "availability_rule": guidance["effective_default"]["availability_rule"],
    }
    ladder = {item["model_family"]: item for item in guidance["default_ladder"]}
    assert list(ladder)[0] == "GPT-5.6 Luna"
    assert set(ladder) == {
        "Spark",
        "GPT-5.4 Mini",
        "GPT-5.4",
        "GPT-5.5",
        "GPT-5.6 Luna",
        "GPT-5.6 Terra",
        "GPT-5.6 Sol",
    }
    assert "Effective default" in ladder["GPT-5.6 Luna"]["role"]
    assert "high by default" in ladder["GPT-5.6 Luna"]["reasoning"]
    assert "not the effective default" in ladder["Spark"]["role"]
    assert "Do not choose Spark first" in ladder["Spark"]["caveats"]
    assert "Do not silently retry" in ladder["GPT-5.4 Mini"]["caveats"]
    assert "model='gpt-6-luna'" in guidance["manager_rule"]
    assert "reasoning_effort='high'" in guidance["manager_rule"]
    assert "Never hide model routing" in guidance["manager_rule"]
    assert "installed Codex catalog" in guidance["summary"]
    assert "verified correct result" in guidance["cost_rule"]
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
