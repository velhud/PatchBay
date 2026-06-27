import importlib.util
import json
from pathlib import Path


def load_trial_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "real_mcp_worker_trial.py"
    spec = importlib.util.spec_from_file_location("real_mcp_worker_trial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trial_recorder_progressively_writes_sanitized_artifacts(tmp_path):
    module = load_trial_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    recorder = module.TrialRecorder(tmp_path / "report", {str(repo): "<trial-repo>"})

    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "codex_worker_start",
            "arguments": {
                "repo_path": str(repo),
                "brief": "raw natural language assignment",
            },
        },
    }
    recorder.log_call_event(
        {
            "phase": "request",
            "method": "tools/call",
            "tool": "codex_worker_start",
            "mcp_id": 7,
            "request": recorder.sanitize(request, request=True),
        }
    )
    recorder.log_call_event(
        {
            "phase": "response",
            "method": "tools/call",
            "tool": "codex_worker_start",
            "mcp_id": 7,
            "ok": True,
            "response": {"result": {"structuredContent": {"workspace": str(repo), "token": "sk-testsecretvalue"}}},
        }
    )
    recorder.check("evidence_written", True, classification="direct_evidence", evidence=f"Checked {repo}")

    calls = (tmp_path / "report" / "calls.jsonl").read_text(encoding="utf-8")
    results = json.loads((tmp_path / "report" / "results.json").read_text(encoding="utf-8"))
    summary = (tmp_path / "report" / "summary.md").read_text(encoding="utf-8")

    assert str(repo) not in calls
    assert str(repo) not in json.dumps(results)
    assert str(repo) not in summary
    assert "raw natural language assignment" not in calls
    assert "<redacted-natural-language-field" in calls
    assert "<trial-repo>" in calls
    assert "sk-testsecretvalue" not in calls
    assert results["checks"][0]["name"] == "evidence_written"
    assert "| `evidence_written` | pass |" in summary


def test_trial_recorder_redacts_uuid_like_session_values(tmp_path):
    module = load_trial_module()
    raw_session = "dc82f84c-13d1-4a7f-8076-6236c33ac4c2"
    raw_branch = "codex/worker-wrk_a1091e0937904cdd97db"
    recorder = module.TrialRecorder(tmp_path / "report", {})

    recorder.set_metadata(launcher_output_tail=[f"New MCP session created: {raw_session} on {raw_branch}"])

    results = (tmp_path / "report" / "results.json").read_text(encoding="utf-8")
    assert raw_session not in results
    assert raw_branch not in results
    assert "<uuid>" in results
    assert "<worker-branch>" in results


def test_render_summary_includes_call_sequence_and_error(tmp_path):
    module = load_trial_module()
    report = {
        "status": "failed",
        "classification": "runtime_bug",
        "tool_mode": "worker",
        "checks": [{"name": "initialize", "ok": False, "classification": "runtime_bug", "evidence": "timed out"}],
        "mcp_call_sequence": [{"index": 2, "method": "initialize", "tool": None, "mcp_id": 1, "ok": False, "duration_seconds": 10.0}],
        "error": "initialize timed out",
    }

    summary = module.render_summary(report)

    assert "Status: `failed`" in summary
    assert "| `initialize` | fail | `runtime_bug` | timed out |" in summary
    assert "#2 `initialize`" in summary
    assert "initialize timed out" in summary


def test_path_aliases_cover_macos_private_var_alias():
    module = load_trial_module()

    aliases = module.path_aliases("/var/folders/example")

    assert "/var/folders/example" in aliases
    assert "/private/var/folders/example" in aliases


def test_trial_config_isolates_runtime_state(tmp_path):
    module = load_trial_module()
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    config_path = module.write_trial_config(repo, runtime, tool_mode="worker")
    config = module.yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["repositories"]["default"] == str(repo)
    assert config["repositories"]["allowed"] == [str(repo)]
    assert config["app"]["tool_mode"] == "worker"
    assert config["logging"]["job_logs_dir"].startswith(str(runtime))
    assert config["logging"]["job_state_dir"].startswith(str(runtime))
    assert config["workers"]["worktree_root"].startswith(str(runtime))
    assert config["workers"]["ignore_user_config"] is True
    assert ".env" in config["security"]["blocked_globs"]


def test_connector_noise_scan_reports_categories_without_log_lines(tmp_path):
    module = load_trial_module()
    log_dir = tmp_path / "runtime" / "logs" / "jobs"
    log_dir.mkdir(parents=True)
    (log_dir / "job-fixture_stderr.log").write_text("Remote MCP connector OAuth warning details\n", encoding="utf-8")

    result = module.scan_job_stderr_for_connector_noise(tmp_path / "runtime")

    assert result["stderr_logs_scanned"] == 1
    assert result["matches"] > 0
    assert "oauth" in result["matched_categories"]
    assert "Remote MCP connector" not in str(result)
