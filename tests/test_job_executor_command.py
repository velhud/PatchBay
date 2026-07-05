import pytest

from patchbay.jobs.executor import JobExecutor


class DummyJobManager:
    pass


def make_executor(tmp_path):
    return JobExecutor(
        {
            "server": {"job_timeout_seconds": 30},
            "repositories": {"allowed": [str(tmp_path)]},
            "security": {
                "default_sandbox": "read-only",
                "allow_dangerously_bypass": False,
                "allowed_env_keys": ["PATH"],
            },
            "logging": {"job_logs_dir": str(tmp_path / "logs")},
            "power_tools": {"codex_home": str(tmp_path / "configured-codex-home")},
        },
        DummyJobManager(),
    )


def test_codex_exec_options_are_before_prompt(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command(
        "apply",
        "change the code",
        str(tmp_path),
        {
            "model": "gpt-5",
            "images": ["screen.png"],
            "features": {"enable": ["json"], "disable": ["web_search"]},
            "profile": "work",
            "config_overrides": ['model_reasoning_effort="high"'],
            "structured_output": True,
            "json_events": True,
        },
    )

    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-1] == "-"
    assert "change the code" not in cmd
    assert executor._stdin_for_command("change the code", cmd) == b"change the code"
    assert "--model" in cmd
    assert "-c" in cmd
    assert 'model_reasoning_effort="high"' in cmd
    assert "--json" in cmd
    assert "--output-schema" in cmd
    assert cmd.index("--model") < len(cmd) - 1
    assert cmd.index("-c") < len(cmd) - 1
    assert cmd.index("--json") < len(cmd) - 1


def test_plan_jobs_use_configured_sandbox_and_disable_stale_full_auto(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command(
        "plan",
        "inspect only",
        str(tmp_path),
        {"sandbox": "workspace-write", "full_auto": True},
    )

    assert cmd[-1] == "-"
    assert "inspect only" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--full-auto" not in cmd
    assert "skills" not in cmd


def test_jobs_can_ignore_user_config_without_discarding_auth_home(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command(
        "plan",
        "inspect only",
        str(tmp_path),
        {"ignore_user_config": True},
    )

    assert "--ignore-user-config" in cmd
    assert cmd.index("--ignore-user-config") < len(cmd) - 1
    env = executor._build_env()
    assert env["CODEX_HOME"] == str(tmp_path / "configured-codex-home")


def test_apply_jobs_default_to_workspace_write(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command("apply", "make a change", str(tmp_path), {})

    assert cmd[-1] == "-"
    assert "make a change" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_resume_jobs_put_options_before_session_and_prompt(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command(
        "resume",
        "continue the task",
        str(tmp_path),
        {
            "resume_session_id": "session-123",
            "model": "gpt-5",
            "images": ["screen.png"],
            "features": {"enable": ["json"], "disable": ["web_search"]},
            "json_events": True,
            "sandbox": "workspace-write",
            "_codex_cwd": str(tmp_path),
            "config_overrides": ['model_reasoning_effort="xhigh"'],
        },
    )

    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-2:] == ["session-123", "-"]
    assert "continue the task" not in cmd
    assert executor._stdin_for_command("continue the task", cmd) == b"continue the task"
    assert "resume" in cmd
    assert "--json" in cmd
    assert "--model" in cmd
    assert 'model_reasoning_effort="xhigh"' in cmd
    assert "--output-schema" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--cd") + 1] == str(tmp_path)
    assert cmd.index("--sandbox") < cmd.index("resume")
    assert cmd.index("--cd") < cmd.index("resume")
    assert cmd.index("--json") < cmd.index("session-123")
    assert cmd.index("--model") < cmd.index("session-123")
    assert cmd.index("-c") < cmd.index("session-123")


def test_resume_jobs_can_ignore_user_config(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command(
        "resume",
        "continue the task",
        str(tmp_path),
        {
            "resume_session_id": "session-123",
            "ignore_user_config": True,
        },
    )

    assert "--ignore-user-config" in cmd
    assert cmd.index("--ignore-user-config") < cmd.index("resume")


def test_resume_jobs_require_session_id(tmp_path):
    executor = make_executor(tmp_path)

    with pytest.raises(ValueError, match="resume_session_id is required"):
        executor._build_codex_command("resume", "continue", str(tmp_path), {})


def test_empty_prompt_does_not_add_stdin_sentinel(tmp_path):
    executor = make_executor(tmp_path)

    cmd = executor._build_codex_command("plan", "", str(tmp_path), {})

    assert cmd[-1] != "-"
    assert executor._stdin_for_command("", cmd) is None


def test_dangerous_bypass_requires_config_opt_in(tmp_path):
    executor = make_executor(tmp_path)

    with pytest.raises(PermissionError, match="dangerously_bypass is disabled"):
        executor._build_codex_command(
            "apply",
            "make a change",
            str(tmp_path),
            {"dangerously_bypass": True},
        )


def test_star_allowed_env_inherits_full_environment(monkeypatch, tmp_path):
    executor = make_executor(tmp_path)
    executor.config["security"]["allowed_env_keys"] = ["*"]
    monkeypatch.setenv("PATCHBAY_WRAPPER_TEST_ENV", "visible")

    env = executor._build_env()

    assert env["PATCHBAY_WRAPPER_TEST_ENV"] == "visible"
