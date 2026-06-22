import pytest

from auth import AuthConfigurationError, build_auth_policy, request_is_authorized


def base_config(host="127.0.0.1", auth=None):
    return {
        "server": {"host": host, "port": 8000},
        "auth": {
            "enabled": False,
            "token_env": "CODEX_MCP_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["codex_mcp_token", "codexpro_token", "token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
            **(auth or {}),
        },
    }


def test_loopback_defaults_to_no_auth():
    policy = build_auth_policy(base_config(), environ={})

    assert policy.enabled is False
    assert request_is_authorized(policy, {}, {}) is True


def test_configured_token_enables_bearer_auth():
    policy = build_auth_policy(base_config(), environ={"CODEX_MCP_HTTP_TOKEN": "test-token"})

    assert policy.enabled is True
    assert request_is_authorized(policy, {"Authorization": "Bearer test-token"}, {}) is True
    assert request_is_authorized(policy, {"Authorization": "Bearer wrong"}, {}) is False


def test_query_token_flow_supports_chatgpt_url_auth():
    policy = build_auth_policy(base_config(), environ={"CODEX_MCP_HTTP_TOKEN": "test-token"})

    assert request_is_authorized(policy, {}, {"codex_mcp_token": "test-token"}) is True
    assert request_is_authorized(policy, {}, {"codexpro_token": "test-token"}) is True
    assert request_is_authorized(policy, {}, {"token": "test-token"}) is True
    assert request_is_authorized(policy, {}, {"codex_mcp_token": "wrong"}) is False


def test_query_token_can_be_disabled():
    policy = build_auth_policy(
        base_config(auth={"allow_query_token": False}),
        environ={"CODEX_MCP_HTTP_TOKEN": "test-token"},
    )

    assert request_is_authorized(policy, {}, {"codex_mcp_token": "test-token"}) is False
    assert request_is_authorized(policy, {"Authorization": "Bearer test-token"}, {}) is True


def test_non_loopback_bind_fails_closed_without_token():
    with pytest.raises(AuthConfigurationError, match="HTTP token is required"):
        build_auth_policy(base_config(host="0.0.0.0"), environ={})


def test_tunnel_mode_fails_closed_without_token():
    with pytest.raises(AuthConfigurationError, match="HTTP token is required"):
        build_auth_policy(base_config(auth={"tunnel_mode": "cloudflare"}), environ={})


def test_tunnel_mode_accepts_token():
    policy = build_auth_policy(
        base_config(auth={"tunnel_mode": "cloudflare"}),
        environ={"CODEX_MCP_HTTP_TOKEN": "test-token"},
    )

    assert policy.enabled is True
    assert "tunnel_mode" in policy.required_reasons
