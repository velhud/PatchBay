import subprocess
import sys
from pathlib import Path


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


def test_handoff_execute_and_watch_with_custom_agent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.txt").write_text("start\n", encoding="utf-8")
    bridge = repo / ".ai-bridge"
    bridge.mkdir()
    (bridge / "current-plan.md").write_text("# Test plan\n\nAppend the marker.\n", encoding="utf-8")
    (repo / "fake_agent.py").write_text(
        "import pathlib, sys\n"
        "plan = pathlib.Path(sys.argv[sys.argv.index('--task-file') + 1]).read_text()\n"
        "pathlib.Path('app.txt').write_text('start\\nimplemented: ' + str('marker' in plan) + '\\n')\n"
        "print('fake agent completed')\n",
        encoding="utf-8",
    )
    init_repo(repo)
    subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    dry_run = subprocess.run(
        [
            sys.executable,
            "scripts/handoff.py",
            "execute",
            "--root",
            str(repo),
            "--agent",
            "custom",
            "--command-template",
            f"{sys.executable} fake_agent.py --task-file {{{{plan_file}}}}",
            "--dry-run",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "fake_agent.py" in dry_run.stdout

    subprocess.run(
        [
            sys.executable,
            "scripts/handoff.py",
            "execute",
            "--root",
            str(repo),
            "--agent",
            "custom",
            "--command-template",
            f"{sys.executable} fake_agent.py --task-file {{{{plan_file}}}}",
            "--yes",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "implemented: True" in (repo / "app.txt").read_text(encoding="utf-8")
    assert "fake agent completed" in (bridge / "agent-status.md").read_text(encoding="utf-8")
    assert "implemented: True" in (bridge / "implementation-diff.patch").read_text(encoding="utf-8")
    assert '"event": "execute_handoff"' in (bridge / "execution-log.jsonl").read_text(encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "scripts/handoff.py",
            "watch",
            "--root",
            str(repo),
            "--agent",
            "custom",
            "--command-template",
            f"{sys.executable} fake_agent.py --task-file {{{{plan_file}}}}",
            "--once",
            "--yes",
            "--debounce-ms",
            "0",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=True,
    )
    assert (bridge / "watch-handoff-state.json").exists()

