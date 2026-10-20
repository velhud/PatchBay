from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from patchbay.authoring.service import AuthorService
from patchbay.authoring.interfaces.catalog import identity as catalog_identity


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/prepare_author_chat_trial.py"


def test_preparation_creates_scoped_private_credentials_and_pinned_context(tmp_path):
    output = tmp_path / "trial"
    result = subprocess.run([sys.executable, str(SCRIPT), "--output-dir", str(output)],
                            cwd=ROOT, text=True, capture_output=True, check=True)
    summary = json.loads(result.stdout)
    assert summary["qualification_status"] == "NOT_RUN"
    assert summary["ordinary_chat_invoked"] is False
    assert summary["approved_check_executed"] is False

    identities = json.loads((output / "prepared-identities.json").read_text())
    assert identities["source_binding"]["commit"] == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    assert identities["source_binding"]["worktree_sha256"]
    assert identities["runtime_catalog_identity"] == catalog_identity()
    assert identities["check_profile"]["id"] == "ordinary-chat-canary"
    assert identities["task_policy_digest"]
    service = AuthorService(output / "author-home")
    task = service.tasks.get(identities["task_id"])
    repo = service.repositories.get(identities["repository_id"])
    assert task["current_revision"] == 0
    assert task["policy"]["profile"]["checks"] == [{
        "id": "unit", "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        "cwd": ".", "timeout_seconds": 30, "required": True, "report": "exit",
        "report_path": None, "min_tests": 1, "allow_skipped": False,
    }]
    assert repo["policy"]["writable_paths"] == ["app.py"]
    assert identities["approved_check_was_run"] is False
    with zipfile.ZipFile(output / "context.zip") as archive:
        assert "source/app.py" in archive.namelist()
        assert b"return left - right" in archive.read("source/app.py")
        assert json.loads(archive.read("CONTEXT.json"))["commit"] == identities["context_commit"]

    private = output / "private/principals.json"
    assert os.stat(private).st_mode & 0o777 == 0o600
    tokens = json.loads(private.read_text())["principals"]
    assert [(item["principal_id"], item["role"], item["task_ids"])
            for item in tokens] == [
        ("trial-author", "author", [identities["task_id"]]),
        ("trial-reader", "reader", [identities["task_id"]]),
    ]
    for item in tokens:
        assert item["token"]
        assert item["token"] not in result.stdout
    profile = json.loads((output / "profile.json").read_text())
    assert profile["qualification_status"] == "NOT_RUN"
    assert profile["prepared_source_commit"] == identities["source_binding"]["commit"]
    assert all(value is None for value in profile["observations"].values())
    evidence = json.loads((output / "qualification-evidence.json").read_text())
    assert evidence["runs"] and all(row["status"] == "NOT_RUN" for row in evidence["runs"].values())
    capture_rows = [json.loads(line) for line in (output / "ab-capture.jsonl").read_text().splitlines()]
    assert capture_rows and all(row["status"] == "NOT_RUN" for row in capture_rows)
    private_state = {output / "private", output / "author-home"}
    public_files = [path for path in output.rglob("*") if path.is_file()
                    and not any(parent in path.parents for parent in private_state)]
    for item in tokens:
        assert all(item["token"].encode() not in path.read_bytes() for path in public_files)
        assert item["token"] not in result.stdout


def test_preparation_needs_no_external_trial_kit_and_can_copy_one_explicitly(tmp_path):
    output = tmp_path / "trial"
    result = subprocess.run([sys.executable, str(SCRIPT), "--output-dir", str(output)],
                            cwd=ROOT, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["qualification_status"] == "NOT_RUN"

    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "profile.json").write_text(json.dumps({"qualification_status": "NOT_RUN", "prepared_source_commit": "old", "observations": {}}))
    (kit / "qualification-evidence.json").write_text(json.dumps({"schema_version": 1, "runs": {"T-test": {"status": "NOT_RUN", "commit": "old"}}}))
    (kit / "ab-capture.jsonl").write_text('{"case_id":"G01","variant":"full-text+structured","status":"NOT_RUN"}\n')
    copied = tmp_path / "trial-with-kit"
    subprocess.run([sys.executable, str(SCRIPT), "--output-dir", str(copied), "--trial-kit", str(kit)],
                   cwd=ROOT, text=True, capture_output=True, check=True)
    source_commit = json.loads((copied / "prepared-identities.json").read_text())["source_binding"]["commit"]
    assert json.loads((copied / "profile.json").read_text())["prepared_source_commit"] == source_commit
    assert json.loads((copied / "qualification-evidence.json").read_text())["runs"]["T-test"]["commit"] == source_commit


def test_preparation_refuses_to_overwrite_existing_output(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--output-dir", str(output)],
                            cwd=ROOT, text=True, capture_output=True)
    assert result.returncode != 0
    assert marker.read_text() == "preserve\n"
