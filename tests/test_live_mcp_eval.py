import json
import subprocess
import sys


def test_disposable_live_mcp_eval_passes():
    completed = subprocess.run(
        [sys.executable, "scripts/live_mcp_eval.py", "--json"],
        cwd=".",
        capture_output=True,
        text=True,
        timeout=40,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["tool_count"] >= 28
    assert {check["name"] for check in report["checks"]} >= {
        "initialize",
        "tools_list",
        "open_workspace",
        "list_skills",
        "load_skill",
        "worker_tool_descriptors",
        "tool_mode",
        "env_read_allowed",
        "blocked_symlink_read",
    }


def test_terminal_reconciliation_eval_holds_cleanup_gate_deterministically():
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/live_mcp_eval.py",
            "--json",
            "--exercise-terminal-reconciliation",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    checks = {check["name"]: check["ok"] for check in report["checks"]}
    assert checks["terminal_cleanup_barrier_intentionally_held"] is True
    assert checks[
        "terminal_cleanup_rejects_real_integration_while_barrier_held"
    ] is True
    assert checks["terminal_cleanup_blocks_same_worker_followup"] is True
    assert checks["same_worker_followup_succeeds_once_after_cleanup"] is True
