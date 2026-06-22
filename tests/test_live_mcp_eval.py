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
        "blocked_env_read",
        "blocked_symlink_read",
        "direct_write_disabled",
    }
