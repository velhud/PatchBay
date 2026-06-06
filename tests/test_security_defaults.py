from pathlib import Path

import yaml


def load_default_config():
    return yaml.safe_load(Path("config.yaml").read_text())


def test_dangerous_bypass_disabled_by_default():
    config = load_default_config()
    assert config["security"]["allow_dangerously_bypass"] is False


def test_sandbox_tool_hidden_by_default():
    config = load_default_config()
    assert config["security"]["expose_codex_sandbox_tool"] is False


def test_cors_disabled_by_default():
    config = load_default_config()
    assert config["server"].get("enable_cors", False) is False


def test_prompt_and_response_body_logging_disabled_by_default():
    config = load_default_config()
    assert config["logging"].get("log_prompt_bodies", False) is False
    assert config["logging"].get("log_response_bodies", False) is False


def test_child_process_environment_is_allowlisted():
    config = load_default_config()
    allowed = set(config["security"]["allowed_env_keys"])
    assert "OPENAI_API_KEY" in allowed
    assert "GITHUB_TOKEN" not in allowed
    assert "ANTHROPIC_API_KEY" not in allowed


def test_config_overrides_disabled_by_default():
    config = load_default_config()
    assert config["security"]["allowed_config_override_prefixes"] == []
