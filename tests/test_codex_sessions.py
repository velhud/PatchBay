import json

import pytest

from codex_sessions import CodexSessionReader
from job_executor import JobExecutor
from job_manager import JobManager
from tools import ToolHandler


SESSION_ID = "019e4789-9b15-77e0-8ddc-13b9525fd730"


def make_config(tmp_path, enabled=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_home = tmp_path / "codex-home"
    return {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".git", ".git/**", "**/.git/**"],
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "bash_transcript": "compact",
            "bash_session_id": "",
            "require_bash_session": False,
            "bash_timeout_ms": 30_000,
            "bash_max_output_bytes": 20_000,
            "codex_session_read": enabled,
            "codex_home": str(codex_home),
            "codex_session_max_messages": 80,
            "codex_session_max_bytes": 80_000,
            "codex_session_max_file_bytes": 20_000_000,
            "codex_session_max_scan_files": 3000,
            "codex_session_max_scan_depth": 6,
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


def write_session(config, messages=None):
    codex_home = config["power_tools"]["codex_home"]
    session_dir = f"{codex_home}/sessions/2026/06/22"
    secret_value = "fixture-" + "secret-value"
    import pathlib

    path = pathlib.Path(session_dir)
    path.mkdir(parents=True)
    session_path = path / f"rollout-2026-06-22T00-00-00-{SESSION_ID}.jsonl"
    rows = [
        {
            "timestamp": "2026-06-22T10:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": SESSION_ID,
                "cwd": "/private/path/that/must/not/return",
            },
        },
        *(
            messages
            or [
                {
                    "timestamp": "2026-06-22T10:01:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "please inspect token=fixture-value"}],
                    },
                },
                {
                    "timestamp": "2026-06-22T10:02:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                },
                {
                    "timestamp": "2026-06-22T10:03:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": f"Authorization: Bearer {secret_value}",
                    },
                },
            ]
        ),
    ]
    session_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return session_path


def test_codex_session_read_is_disabled_by_default(tmp_path):
    config = make_config(tmp_path, enabled=False)
    write_session(config)
    reader = CodexSessionReader(config)

    with pytest.raises(ValueError, match="codex_read_session is disabled"):
        reader.read_session({"session_id": SESSION_ID})


def test_codex_session_read_returns_bounded_redacted_transcript(tmp_path):
    config = make_config(tmp_path, enabled=True)
    write_session(config)
    reader = CodexSessionReader(config)

    result = reader.read_session({"session_id": SESSION_ID})
    serialized = json.dumps(result)

    assert result["session"]["session_id"] == SESSION_ID
    assert result["session"]["source_path_returned"] is False
    assert result["message_count"] == 3
    assert result["messages"][0]["role"] == "user"
    assert "token=[REDACTED_POSSIBLE_SECRET]" in result["messages"][0]["content"]
    assert "fixture-value" not in serialized
    assert "fixture-" + "secret-value" not in serialized
    assert "/private/path/that/must/not/return" not in serialized
    assert result["transcript_returned"] is True
    assert result["paths_returned"] is False
    assert result["source_path_returned"] is False


def test_codex_session_read_caps_messages_and_bytes(tmp_path):
    config = make_config(tmp_path, enabled=True)
    messages = []
    for index in range(5):
        messages.append(
            {
                "timestamp": f"2026-06-22T10:0{index}:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "x" * 100}],
                },
            }
        )
    write_session(config, messages=messages)
    reader = CodexSessionReader(config)

    by_messages = reader.read_session({"session_id": SESSION_ID, "max_messages": 2})
    by_bytes = reader.read_session({"session_id": SESSION_ID, "max_total_bytes": 4_000})

    assert by_messages["message_count"] == 2
    assert by_messages["truncated"] is True
    assert by_bytes["message_count"] <= 5


@pytest.mark.asyncio
async def test_codex_read_session_tool_uses_power_gate(tmp_path):
    config = make_config(tmp_path, enabled=True)
    write_session(config)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, JobExecutor(config, manager))

    result = await handler.handle_tool_call("codex_read_session", {"session_id": SESSION_ID})

    assert result["session"]["session_id"] == SESSION_ID
    assert result["transcript_returned"] is True
