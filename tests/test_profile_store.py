from pathlib import Path

import yaml

from patchbay.connector.profiles import (
    normalize_logging_paths,
    profile_id_for_root,
    profile_path_for_root,
    read_workspace_profile,
    resolve_runtime_path,
    runtime_path,
    runtime_config_path_for_root,
    runtime_status_path_for_root,
    save_workspace_profile,
    write_runtime_config,
    write_runtime_status,
)


def test_profile_paths_are_deterministic_and_home_scoped(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"

    assert profile_id_for_root(root) == profile_id_for_root(root)
    assert profile_path_for_root(root, env).parent == tmp_path / "home" / "profiles"
    assert runtime_config_path_for_root(root, env).parent == tmp_path / "home" / "runtime"
    assert runtime_status_path_for_root(root, env).parent == tmp_path / "home" / "runtime"


def test_runtime_path_respects_patchbay_home(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}

    assert runtime_path("logs", "jobs", environ=env) == tmp_path / "home" / "runtime" / "logs" / "jobs"
    assert resolve_runtime_path(None, "logs", "audit.log", environ=env) == tmp_path / "home" / "runtime" / "logs" / "audit.log"


def test_normalize_logging_paths_defaults_to_runtime_home(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}
    config = {
        "logging": {
            "audit_file": None,
            "job_logs_dir": "",
            "job_state_dir": None,
        }
    }

    normalized = normalize_logging_paths(config, env)

    assert normalized["logging"]["audit_file"] == str(tmp_path / "home" / "runtime" / "logs" / "audit.log")
    assert normalized["logging"]["job_logs_dir"] == str(tmp_path / "home" / "runtime" / "logs" / "jobs")
    assert normalized["logging"]["job_state_dir"] == str(tmp_path / "home" / "runtime" / "logs" / "jobs" / "state")
    assert normalized["logging"]["worktrees_dir"] == str(tmp_path / "home" / "runtime" / "worktrees" / "jobs")


def test_normalize_logging_paths_preserves_explicit_job_log_root(tmp_path):
    job_logs = tmp_path / "custom-logs"
    config = {"logging": {"job_logs_dir": str(job_logs), "job_state_dir": None}}

    normalized = normalize_logging_paths(config, {})

    assert normalized["logging"]["job_logs_dir"] == str(job_logs)
    assert normalized["logging"]["job_state_dir"] == str(job_logs / "state")


def test_workspace_profile_round_trip_redacts_sensitive_keys(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}
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
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"
    root.mkdir()

    path = write_runtime_config(root, {"server": {"port": 8123}}, env)

    assert yaml.safe_load(Path(path).read_text(encoding="utf-8")) == {"server": {"port": 8123}}
    assert oct(Path(path).stat().st_mode & 0o777) == "0o600"


def test_runtime_status_round_trip_strips_token_like_keys(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "home")}
    root = tmp_path / "repo"
    root.mkdir()
    token_value = "fixture-runtime-" + "token"

    path = write_runtime_status(root, {"server_url": "https://bridge.example/mcp", "token": token_value}, env)
    payload = Path(path).read_text(encoding="utf-8")

    assert token_value not in payload
    assert '"server_url": "https://bridge.example/mcp"' in payload
    assert oct(Path(path).stat().st_mode & 0o777) == "0o600"
