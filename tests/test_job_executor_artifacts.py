import json
import sys

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState


def make_config(tmp_path, logging_overrides=None):
    repo = tmp_path / "repo"
    repo.mkdir()
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
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
            "job_log_max_bytes": 200_000,
            "write_raw_job_logs": False,
            **(logging_overrides or {}),
        },
    }


@pytest.mark.asyncio
async def test_job_prompt_is_passed_to_subprocess_stdin(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan",
        "stdin prompt text",
        config["repositories"]["default"],
        {},
    )

    def fake_command(mode, prompt, cwd, options=None):
        script = (
            "import json, sys\n"
            "data = sys.stdin.read()\n"
            "print(json.dumps({'summary': data, 'files_changed': []}))\n"
        )
        return [sys.executable, "-c", script, "-"]

    executor._build_codex_command = fake_command

    await executor.execute_job(job_id)

    job = manager.get_job(job_id)

    assert job.state == JobState.COMPLETED
    assert job.result["summary"] == "stdin prompt text"


@pytest.mark.asyncio
async def test_job_artifacts_are_redacted_by_default(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})

    def fake_command(mode, prompt, cwd, options=None):
        secret_value = "fixture-" + "secret-value"
        script = (
            "import json, sys\n"
            "print(json.dumps({'summary': 'done token=fixture-value', 'files_changed': []}))\n"
            f"print('Authorization: Bearer {secret_value}', file=sys.stderr)\n"
        )
        return [sys.executable, "-c", script]

    executor._build_codex_command = fake_command

    await executor.execute_job(job_id)

    job = manager.get_job(job_id)
    stdout_log = tmp_path / "logs" / "jobs" / f"{job_id}_stdout.log"
    stderr_log = tmp_path / "logs" / "jobs" / f"{job_id}_stderr.log"
    result_file = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    serialized = stdout_log.read_text(encoding="utf-8") + stderr_log.read_text(encoding="utf-8")

    assert job.state == JobState.COMPLETED
    assert job.result["summary"] == "done token=[REDACTED_POSSIBLE_SECRET]"
    assert "fixture-value" not in serialized
    assert "fixture-" + "secret-value" not in serialized
    assert "fixture-value" not in result_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_job_artifacts_are_capped_by_default(tmp_path):
    config = make_config(tmp_path, {"job_log_max_bytes": 40})
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {"structured_output": False})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "print('x' * 200)"]

    executor._build_codex_command = fake_command

    await executor.execute_job(job_id)

    stdout_log = tmp_path / "logs" / "jobs" / f"{job_id}_stdout.log"
    text = stdout_log.read_text(encoding="utf-8")

    assert len(text.encode("utf-8")) < 100
    assert "...[log truncated to 40 bytes]" in text


@pytest.mark.asyncio
async def test_raw_job_artifacts_require_explicit_opt_in(tmp_path):
    config = make_config(tmp_path, {"write_raw_job_logs": True})
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {"structured_output": False})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "print('token=fixture-value')"]

    executor._build_codex_command = fake_command

    await executor.execute_job(job_id)

    stdout_log = tmp_path / "logs" / "jobs" / f"{job_id}_stdout.log"

    assert "token=fixture-value" in stdout_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_parse_current_codex_agent_message_jsonl(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    result_file = tmp_path / "result.json"
    stdout = (
        '{"type":"thread.started","thread_id":"session-123"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"item_1","type":"agent_message",'
        '"text":"{\\"summary\\":\\"CODEX_REAL_MCP_OK\\",\\"files_changed\\":[],'
        '\\"commands_run\\":[],\\"tests_run\\":[]}"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
    ).encode("utf-8")

    result = await executor._parse_result(stdout, result_file, {"structured_output": True})

    assert result["summary"] == "CODEX_REAL_MCP_OK"
    assert result["files_changed"] == []
    assert result["commands_run"] == []
    assert json.loads(result_file.read_text(encoding="utf-8"))["summary"] == "CODEX_REAL_MCP_OK"


@pytest.mark.asyncio
async def test_parse_codex_agent_message_content_list_jsonl(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    result_file = tmp_path / "result.json"
    stdout = (
        '{"type":"thread.started","thread":{"id":"session-nested"}}\n'
        '{"type":"item.completed","item":{"id":"item_1","type":"agent_message",'
        '"content":[{"type":"output_text","text":"{\\"summary\\":\\"CONTENT_OK\\",'
        '\\"detailed_report\\":\\"Parsed from content list.\\",\\"evidence\\":[\\"file A\\"],'
        '\\"files_changed\\":[],\\"commands_run\\":[],\\"tests_run\\":[],\\"notes\\":\\"\\",'
        '\\"risks\\":[],\\"open_questions\\":[],\\"next_steps\\":[]}"}]}}\n'
    ).encode("utf-8")

    result = await executor._parse_result(stdout, result_file, {"structured_output": True})
    checkpoint = executor._checkpoint_from_event(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "summary": "Checkpoint content list.",
                                "evidence": ["route traced"],
                                "files_changed": [],
                                "commands_run": [],
                                "tests_run": [],
                                "risks": [],
                                "open_questions": [],
                                "next_steps": [],
                            }
                        ),
                    }
                ],
            },
        }
    )

    assert result["summary"] == "CONTENT_OK"
    assert result["detailed_report"] == "Parsed from content list."
    assert result["evidence"] == ["file A"]
    assert executor._session_id_from_event({"type": "thread.started", "thread": {"id": "session-nested"}}) == "session-nested"
    assert checkpoint["summary"] == "Checkpoint content list."
    assert checkpoint["evidence_count"] == 1


def test_stdout_event_observer_tracks_live_status_counters_and_command_phase(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("interactive", "inspect", config["repositories"]["default"], {})
    state = {"session_id": None}

    executor._observe_stdout_event(job_id, b'{"type":"thread.started","thread_id":"session-live"}\n', state)
    executor._observe_stdout_event(
        job_id,
        b'{"type":"item.started","item":{"type":"command_execution","status":"in_progress","command":"rg worker runtime"}}\n',
        state,
    )
    running = manager.get_job(job_id)

    assert running.session_id == "session-live"
    assert running.event_count == 2
    assert running.stdout_bytes_seen > 0
    assert running.current_phase == "command_running"
    assert running.current_command_preview == "rg worker runtime"
    assert running.current_command_started_at is not None

    executor._observe_stdout_event(
        job_id,
        b'{"type":"item.completed","item":{"type":"command_execution","status":"completed","command":"rg worker runtime"}}\n',
        state,
    )
    completed_command = manager.get_job(job_id)

    assert completed_command.event_count == 3
    assert completed_command.current_phase == "command_completed_waiting_for_model"
    assert completed_command.current_command_preview is None
    assert completed_command.last_command_preview == "rg worker runtime"
    assert completed_command.last_command_completed_at is not None
