import subprocess
import sys
from pathlib import Path


def test_pro_context_bundle_and_apply(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Probe\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Use probe rules.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    bundle = subprocess.run(
        [
            sys.executable,
            "scripts/pro_context.py",
            "bundle",
            "--root",
            str(repo),
            "--path",
            "README.md",
            "--include-diff",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "pro-context.md" in bundle.stdout
    context = (repo / ".ai-bridge" / "pro-context.md").read_text(encoding="utf-8")
    assert "README.md" in context
    assert "Probe" in context

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("Add the final implementation marker.\n", encoding="utf-8")
    apply = subprocess.run(
        [
            sys.executable,
            "scripts/pro_context.py",
            "apply",
            "--root",
            str(repo),
            "--file",
            str(plan_file),
            "--agent",
            "codex",
        ],
        cwd=".",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "current-plan.md" in apply.stdout
    current_plan = (repo / ".ai-bridge" / "current-plan.md").read_text(encoding="utf-8")
    assert "Planning Model Handoff" in current_plan
    assert "final implementation marker" in current_plan

