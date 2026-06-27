from job_executor import JobExecutor


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
        },
        DummyJobManager(),
    )


def test_worker_resume_reasserts_sandbox_and_worktree_before_resume(tmp_path):
    executor = make_executor(tmp_path)
    worktree = tmp_path / "worker-worktree"
    worktree.mkdir()

    cmd = executor._build_codex_command(
        "resume",
        "continue in same worker workspace",
        str(tmp_path),
        {
            "resume_session_id": "session-worker",
            "sandbox": "workspace-write",
            "_codex_cwd": str(worktree),
            "structured_output": True,
            "json_events": True,
        },
    )

    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-3:] == ["resume", "session-worker", "-"]
    assert "continue in same worker workspace" not in cmd
    assert executor._stdin_for_command("continue in same worker workspace", cmd) == b"continue in same worker workspace"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--cd") + 1] == str(worktree)
    assert cmd.index("--sandbox") < cmd.index("resume")
    assert cmd.index("--cd") < cmd.index("resume")
    assert cmd.index("--output-schema") < cmd.index("resume")
    assert cmd.index("--json") < cmd.index("resume")
