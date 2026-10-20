import os
import plistlib
from pathlib import Path

import pytest

from scripts.install_macos_launch_agent import (
    LaunchAgentError,
    build_launch_agent_plist,
    install_launch_agent,
    write_server_wrapper,
    write_plist_atomic,
)


def _inputs(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    config = tmp_path / "config.yaml"
    config.write_text("server: {}\n", encoding="utf-8")
    config.chmod(0o600)
    home = tmp_path / "patchbay-home"
    home.mkdir(mode=0o700)
    logs = tmp_path / "logs"
    return {
        "repo": str(repo),
        "python_executable": str(python),
        "config": str(config),
        "patchbay_home": str(home),
        "log_dir": str(logs),
    }


def test_build_launch_agent_is_user_scoped_and_restartable(tmp_path):
    plist = build_launch_agent_plist(**_inputs(tmp_path), label="com.example.patchbay", codex_bin="/opt/homebrew/bin/codex")

    assert plist["Label"] == "com.example.patchbay"
    assert plist["ProgramArguments"][-2:] == ["-m", "patchbay.server"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProcessType"] == "Background"
    assert plist["LowPriorityIO"] is True
    assert plist["Umask"] == 0o077
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["EnvironmentVariables"]["PATH"].split(os.pathsep)[0] == "/opt/homebrew/bin"
    assert plist["EnvironmentVariables"]["PYTHONPATH"].endswith("/src")
    assert plist["StandardOutPath"].endswith("/logs/server.stdout.log")
    assert plist["StandardErrorPath"].endswith("/logs/server.stderr.log")


def test_write_server_wrapper_preserves_virtualenv_interpreter(tmp_path):
    inputs = _inputs(tmp_path)
    wrapper = Path(inputs["patchbay_home"]) / "runtime" / "launchd" / "patchbay-server"

    written = write_server_wrapper(wrapper, inputs["python_executable"])

    assert written == wrapper.resolve()
    assert written.stat().st_mode & 0o777 == 0o700
    assert str(Path(inputs["python_executable"]).absolute()) in written.read_text(encoding="utf-8")
    assert written.read_text(encoding="utf-8").endswith(" -m patchbay.server\n")


def test_build_launch_agent_rejects_insecure_runtime_config(tmp_path):
    inputs = _inputs(tmp_path)
    Path(inputs["config"]).chmod(0o644)

    with pytest.raises(LaunchAgentError, match="private"):
        build_launch_agent_plist(**inputs)


def test_build_launch_agent_rejects_relative_paths(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["repo"] = "relative-repo"

    with pytest.raises(LaunchAgentError, match="absolute"):
        build_launch_agent_plist(**inputs)


def test_write_plist_atomic_is_private_and_round_trips(tmp_path, monkeypatch):
    # The production writer is confined to ~/Library/LaunchAgents.  Point the
    # module's home-derived constant at a disposable equivalent for this test.
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setattr("scripts.install_macos_launch_agent.DEFAULT_LAUNCH_AGENTS", launch_agents)
    destination = launch_agents / "com.example.patchbay.plist"
    payload = {"Label": "com.example.patchbay", "KeepAlive": True}

    written = write_plist_atomic(destination, payload)

    assert written == destination.resolve()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(destination.read_bytes()) == payload
    assert not list(launch_agents.glob("*.tmp"))


def test_install_launch_agent_boots_out_and_kickstarts_exact_user_service(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr("scripts.install_macos_launch_agent.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "scripts.install_macos_launch_agent._launchctl",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr("scripts.install_macos_launch_agent._wait_for_unload", lambda service: None)

    install_launch_agent(tmp_path / "agent.plist", label="com.example.patchbay")

    domain = f"gui/{os.getuid()}"
    assert calls == [
        (("bootout", f"{domain}/com.example.patchbay"), {"check": False}),
        (("bootstrap", domain, str(tmp_path / "agent.plist")), {}),
        (("kickstart", "-k", f"{domain}/com.example.patchbay"), {}),
    ]
