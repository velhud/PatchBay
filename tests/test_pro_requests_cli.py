import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


def env():
    result = dict(os.environ)
    result["PYTHONPATH"] = os.pathsep.join(["src", result.get("PYTHONPATH", "")])
    return result


def init_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# CLI Pro Requests\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Pro CLI", "-c", "user.email=pro-cli@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def config(tmp_path: Path, repo: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": {"max_concurrent_jobs": 2, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
                "repositories": {"default": str(repo), "allowed": [str(repo)]},
                "security": {"require_git_repo": False, "blocked_globs": []},
                "logging": {
                    "job_logs_dir": str(tmp_path / "logs" / "jobs"),
                    "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
                },
                "pro_requests": {
                    "root": str(tmp_path / "runtime" / "pro-requests"),
                    "mirror_enabled": True,
                    "mirror_dir": ".ai-bridge/pro-requests",
                },
                "workers": {"worktree_root": str(tmp_path / "workers")},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_pro_request_cli_create_list_show_close(tmp_path):
    repo = init_repo(tmp_path / "repo")
    config_path = config(tmp_path, repo)
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nNeed Pro help.\n", encoding="utf-8")

    create = subprocess.run(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "pro-request",
            "create",
            "--config",
            str(config_path),
            "--repo",
            str(repo),
            "--title",
            "Need Pro help",
            "--report",
            str(report),
            "--json",
        ],
        cwd=".",
        env=env(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert create.returncode == 0, create.stderr
    request_id = json.loads(create.stdout)["id"]

    listed = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", "pro-request", "list", "--config", str(config_path), "--json"],
        cwd=".",
        env=env(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["requests"][0]["id"] == request_id

    shown = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", "pro-request", "show", "--config", str(config_path), request_id, "--json"],
        cwd=".",
        env=env(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert shown.returncode == 0
    assert "Need Pro help" in json.loads(shown.stdout)["report_markdown"]

    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "pro-request",
            "close",
            "--config",
            str(config_path),
            request_id,
            "--reason",
            "done",
            "--json",
        ],
        cwd=".",
        env=env(),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert closed.returncode == 0
    assert json.loads(closed.stdout)["request"]["status"] == "closed"
