from pathlib import Path

import yaml

from profile_store import (
    profile_id_for_root,
    profile_path_for_root,
    read_workspace_profile,
    runtime_config_path_for_root,
    runtime_status_path_for_root,
    save_workspace_profile,
    write_runtime_config,
    write_runtime_status,
)


def test_profile_paths_are_deterministic_and_home_scoped(tmp_path):
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"

    assert profile_id_for_root(root) == profile_id_for_root(root)
    assert profile_path_for_root(root, env).parent == tmp_path / "home" / "profiles"
    assert runtime_config_path_for_root(root, env).parent == tmp_path / "home" / "runtime"
    assert runtime_status_path_for_root(root, env).parent == tmp_path / "home" / "runtime"


def test_workspace_profile_round_trip_redacts_sensitive_keys(tmp_path):
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"
    root.mkdir()
    token_value = "fixture-" + "token-value"
    cloudflare_value = "fixture-" + "cloudflare-value"

    path = save_workspace_profile(
        root,
        {
            "server": {"port": 8123},
            "auth": {"token": token_value, "tunnel_mode": "custom"},
            "cloudflare_token": cloudflare_value,
            "power_tools": {"bash_mode": "safe"},
        },
        env,
    )

    raw = Path(path).read_text(encoding="utf-8")
    assert token_value not in raw
    assert cloudflare_value not in raw

    loaded = read_workspace_profile(root, env)
    assert loaded["server"] == {"port": 8123}
    assert loaded["auth"] == {"tunnel_mode": "custom"}
    assert loaded["power_tools"] == {"bash_mode": "safe"}
    assert loaded["root"] == str(root.resolve())
    assert loaded["profile_path"] == path


def test_runtime_config_is_private_yaml(tmp_path):
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"
    root.mkdir()

    path = write_runtime_config(root, {"server": {"port": 8123}}, env)

    assert yaml.safe_load(Path(path).read_text(encoding="utf-8")) == {"server": {"port": 8123}}
    assert oct(Path(path).stat().st_mode & 0o777) == "0o600"


def test_runtime_status_round_trip_strips_token_like_keys(tmp_path):
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"
    root.mkdir()
    token_value = "fixture-runtime-" + "token"

    path = write_runtime_status(root, {"server_url": "https://bridge.example/mcp", "token": token_value}, env)
    payload = Path(path).read_text(encoding="utf-8")

    assert token_value not in payload
    assert '"server_url": "https://bridge.example/mcp"' in payload
    assert oct(Path(path).stat().st_mode & 0o777) == "0o600"
