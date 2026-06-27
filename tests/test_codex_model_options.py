import json
import subprocess

import pytest

from codex_model_options import (
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

    monkeypatch.setattr("codex_model_options.subprocess.run", fake_run)
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
    assert result["selected_model"]["reasoning_efforts"] == [
        {"effort": "low", "description": "Fast"},
        {"effort": "high", "description": "Deep"},
    ]
    assert "base_instructions" not in json.dumps(result)


def test_worker_option_validation_rejects_unsafe_values():
    assert validate_worker_model("gpt-5.5") == "gpt-5.5"
    assert validate_reasoning_effort("HIGH") == "high"
    assert build_reasoning_config_override("xhigh") == 'model_reasoning_effort="xhigh"'

    with pytest.raises(ValueError, match="model"):
        validate_worker_model("gpt-5.5; rm -rf /")
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_reasoning_effort("maximum")
