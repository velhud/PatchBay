from pathlib import Path

import yaml


def load_default_config():
    return yaml.safe_load(Path("config.yaml").read_text())


def test_dangerous_bypass_disabled_by_default():
    config = load_default_config()
    assert config["security"]["allow_dangerously_bypass"] is False


def test_legacy_sandbox_tool_config_removed():
    config = load_default_config()
    assert "expose_codex_sandbox_tool" not in config["security"]


def test_cors_disabled_by_default():
    config = load_default_config()
    assert config["server"].get("enable_cors", False) is False


def test_request_size_limit_configured_by_default():
    config = load_default_config()
    assert config["server"]["max_request_bytes"] == 1_048_576


def test_http_auth_defaults_are_public_exposure_ready():
    config = load_default_config()
    assert config["auth"]["enabled"] is False
    assert config["auth"]["token_env"] == "CODEX_MCP_HTTP_TOKEN"
    assert config["auth"]["allow_query_token"] is True
    assert "codex_mcp_token" in config["auth"]["query_token_names"]
    assert "codexpro_token" in config["auth"]["query_token_names"]
    assert config["auth"]["require_for_non_loopback"] is True
    assert config["auth"]["require_for_tunnel"] is True
    assert config["auth"]["tunnel_mode"] == "none"


def test_tunnel_defaults_do_not_expose_network_or_store_tokens():
    config = load_default_config()
    tunnel = config["tunnel"]
    assert tunnel["cloudflared"] == "cloudflared"
    assert tunnel["ngrok"] == "ngrok"
    assert tunnel["cloudflare_token_env"] == "CLOUDFLARE_TUNNEL_TOKEN"
    assert tunnel["cloudflare_token_file"] == ""
    assert tunnel["timeout_seconds"] == 45


def test_prompt_and_response_body_logging_disabled_by_default():
    config = load_default_config()
    assert config["logging"].get("access_log", False) is False
    assert config["logging"].get("job_state_dir") == "./logs/jobs/state"
    assert config["logging"].get("job_log_max_bytes") == 200_000
    assert config["logging"].get("write_raw_job_logs", False) is False
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


def test_workspace_context_blocks_common_sensitive_paths_by_default():
    config = load_default_config()
    blocked = set(config["security"]["blocked_globs"])
    assert ".env" in blocked
    assert ".git/**" in blocked
    assert "**/*.pem" in blocked
    assert "logs/**" in blocked
    assert "worktrees/**" in blocked
    assert config["security"]["context_dir"] == ".ai-bridge"
    assert config["security"]["max_write_bytes"] > 0
    assert config["security"]["max_diff_bytes"] > 0


def test_power_tools_are_disabled_by_default():
    config = load_default_config()
    power = config["power_tools"]
    assert power["direct_write"] is False
    assert power["bash_mode"] == "off"
    assert power["bash_transcript"] == "compact"
    assert power["bash_session_id"] == ""
    assert power["require_bash_session"] is False
    assert power["bash_timeout_ms"] > 0
    assert power["bash_max_output_bytes"] > 0
    assert power["codex_session_read"] is False
    assert power["codex_home"] == ""
    assert power["codex_session_max_messages"] > 0
    assert power["codex_session_max_bytes"] > 0
