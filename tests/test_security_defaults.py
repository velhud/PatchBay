from pathlib import Path

import yaml

from patchbay.connector.profiles import normalize_logging_paths


def load_default_config():
    return yaml.safe_load(Path("config.yaml").read_text())


def test_dangerous_bypass_enabled_for_full_permission_profile():
    config = load_default_config()
    assert config["security"]["allow_dangerously_bypass"] is True
    assert config["security"]["default_sandbox"] == "danger-full-access"
    assert config["security"]["require_git_repo"] is False


def test_legacy_sandbox_tool_config_removed():
    config = load_default_config()
    assert "expose_codex_sandbox_tool" not in config["security"]


def test_cors_disabled_by_default():
    config = load_default_config()
    assert config["server"].get("enable_cors", False) is False


def test_request_size_limit_configured_by_default():
    config = load_default_config()
    assert config["server"]["max_request_bytes"] == 16_777_216


def test_http_auth_defaults_are_public_exposure_ready():
    config = load_default_config()
    assert config["auth"]["enabled"] is False
    assert config["auth"]["token_env"] == "PATCHBAY_HTTP_TOKEN"
    assert config["auth"]["allow_query_token"] is True
    assert "patchbay_token" in config["auth"]["query_token_names"]
    assert "token" in config["auth"]["query_token_names"]
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


def test_prompt_and_response_body_logging_disabled_by_default(tmp_path):
    config = load_default_config()
    assert config["logging"].get("access_log", False) is False
    assert config["logging"].get("audit_file") is None
    assert config["logging"].get("job_logs_dir") is None
    assert config["logging"].get("job_state_dir") is None
    assert config["logging"].get("private_evidence_dir") is None
    assert config["logging"].get("job_log_max_bytes") == 200_000
    assert config["logging"].get("write_raw_job_logs", False) is False
    assert config["logging"].get("private_evidence_log", False) is False
    assert config["logging"].get("store_job_prompts", False) is False
    assert config["logging"].get("store_mcp_transcripts", False) is False
    assert config["logging"].get("log_prompt_bodies", False) is False
    assert config["logging"].get("log_response_bodies", False) is False

    normalized = normalize_logging_paths(config, {"PATCHBAY_HOME": str(tmp_path / "home")})
    assert normalized["logging"]["audit_file"] == str(tmp_path / "home" / "runtime" / "logs" / "audit.log")
    assert normalized["logging"]["job_logs_dir"] == str(tmp_path / "home" / "runtime" / "logs" / "jobs")
    assert normalized["logging"]["job_state_dir"] == str(tmp_path / "home" / "runtime" / "logs" / "jobs" / "state")
    assert normalized["logging"]["private_evidence_dir"] == str(tmp_path / "home" / "runtime" / "logs" / "private-evidence")


def test_child_process_environment_is_inherited_for_full_permission_profile():
    config = load_default_config()
    assert config["security"]["allowed_env_keys"] == ["*"]


def test_config_overrides_enabled_for_full_permission_profile():
    config = load_default_config()
    assert config["security"]["allowed_config_override_prefixes"] == [""]


def test_workspace_context_has_no_blocked_globs_in_full_permission_profile():
    config = load_default_config()
    assert config["security"]["blocked_globs"] == []
    assert config["security"]["context_dir"] == ".ai-bridge"
    assert config["security"]["max_write_bytes"] > 0
    assert config["security"]["max_diff_bytes"] > 0


def test_power_tools_are_enabled_for_full_permission_profile():
    config = load_default_config()
    power = config["power_tools"]
    assert power["direct_write"] is True
    assert power["bash_mode"] == "full"
    assert power["bash_transcript"] == "compact"
    assert power["bash_session_id"] == ""
    assert power["require_bash_session"] is False
    assert power["bash_timeout_ms"] > 0
    assert power["bash_max_output_bytes"] > 0
    assert power["codex_session_read"] is True
    assert power["codex_home"] == "~/.codex"
    assert power["codex_session_max_messages"] > 0
    assert power["codex_session_max_bytes"] > 0
