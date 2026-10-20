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
                "slug": "gpt-6-luna",
                "display_name": "GPT-6 Luna",
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
        'model = "gpt-6-luna"\nmodel_reasoning_effort = "high"\n',
        encoding="utf-8",
    )

    result = worker_option_menu(
        {"power_tools": {"codex_home": str(codex_home)}},
        model="gpt-6-luna",
    )

    assert result["source"] == "codex_debug_models"
    assert result["codex_version"] == "codex-cli 0.test"
    assert result["default_model"] == "gpt-6-luna"
    assert result["selected_model"]["id"] == "gpt-6-luna"
    guidance = result["model_selection_guidance"]
    assert guidance["effective_default"]["model_id"] == "gpt-6-luna"
    assert guidance["effective_default"]["reasoning_effort"] == "high"
    assert [x["model_family"] for x in guidance["default_ladder"]] == ["GPT-6 Luna"]
    assert result["worker_model_available"] is True
    assert result["allows_custom_model_string"] is False
    assert result["reasoning_efforts"] == [{"effort": "high", "description": "Deep"}]
    assert "base_instructions" not in json.dumps(result)


def test_worker_option_validation_rejects_unsafe_values():
    assert validate_worker_model("gpt-6-luna") == "gpt-6-luna"
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


def test_luna_discovery_is_not_lost_beyond_catalog_display_cap(monkeypatch, tmp_path):
    from patchbay.workers import model_options_legacy as legacy
    catalog = [{'slug':f'fixture-{i}', 'priority':100, 'visibility':'list'} for i in range(40)]
    catalog.append({'slug':'gpt-6-luna', 'priority':0, 'visibility':'list'})
    monkeypatch.setattr(legacy, '_load_model_catalog', lambda config: (catalog, 'fixture', None))
    monkeypatch.setattr(legacy, '_codex_version', lambda: 'fixture')
    result = worker_option_menu({'power_tools':{'codex_home':str(tmp_path)}})
    assert result['worker_model_available'] is True
    assert [item['id'] for item in result['models']] == ['gpt-6-luna']
    catalog.pop()
    result = worker_option_menu({'power_tools':{'codex_home':str(tmp_path)}})
    assert result['worker_model_available'] is False and not result['selected_model']
