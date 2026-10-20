"""Portable operational regressions and evidence-evaluator safeguards."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.revisions.git_objects import Git
from patchbay.authoring.service import AuthorService

from conftest import git
from test_core import package_for, submit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import author_requirements_eval as requirements_eval  # noqa: E402


def test_candidate_ref_crash_recovers_after_store_reopen_and_replay(setup_author, tmp_path, monkeypatch):
    service, _, repo, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"recovered.txt": "pinned bytes"})
    original = Git.create_candidate
    pinned = {}

    def crash_after_ref(self, *args, **kwargs):
        pinned["candidate"] = original(self, *args, **kwargs)
        raise RuntimeError("injected termination after immutable candidate ref")

    monkeypatch.setattr(Git, "create_candidate", crash_after_ref)
    with pytest.raises(RuntimeError, match="injected termination"):
        submit(service, task, package, lease, "same-request")
    operation = service.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'", (task["id"],))
    # The candidate ref is durable before the journal phase advances, so a
    # crash in this exact gap must be resolved from the idempotent Git ref.
    assert operation["phase"] == "constructing"
    assert service.repositories.git(repo["id"]).resolve(pinned["candidate"][0]) == pinned["candidate"][0]

    home = service.store.home
    reopened = AuthorService(home)
    monkeypatch.setattr(Git, "create_candidate", original)
    recovered = reopened.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])
    replay = reopened.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])
    assert recovered["commit"] == pinned["candidate"][0] == replay["commit"]
    assert reopened.tasks.get(task["id"])["current_revision"] == 1
    assert len(reopened.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 2


def test_unresolved_owned_run_fences_dispatch_and_backup_on_portable_host(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    import time
    import uuid

    operation_id = "op_" + uuid.uuid4().hex
    now = time.time()
    task_record = service.tasks.get(task["id"])
    git = service.repositories.git(task_record["repository_id"])
    commit = task_record["current_commit"]
    with service.store.transaction() as conn:
        conn.execute(
            "INSERT INTO operations(id,task_id,kind,idempotency_key,fingerprint,revision,state,phase,payload,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], "checks.run", "portable-unknown", "fixture", 0, "running", "launching", "{}", now, now),
        )
        conn.execute(
            "INSERT INTO runs(id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], 0, commit, git.tree(commit), "{}", "fixture", "launching", "pending", now, now),
        )
    with pytest.raises(AuthorError) as error:
        Maintenance(service.store).backup(tmp_path / "blocked-backup.zip")
    assert error.value.code == "MAINTENANCE_BLOCKED"
    assert not (tmp_path / "blocked-backup.zip").exists()


def test_reference_aware_gc_keeps_snapshot_and_git_refs_after_restore(setup_author, tmp_path):
    service, _, repo, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"retained.txt": "canonical"})
    accepted = submit(service, task, package, lease)
    context = service.snapshots.export(task["id"])
    unused = service.store.blobs.put(b"old unreferenced disposable bytes")
    import os
    import time

    old = time.time() - 3600
    os.utime(service.store.blobs.path(unused), (old, old))
    os.utime(service.store.blobs.path(context["bundle_sha"]), (old, old))
    backup = tmp_path / "before-gc.zip"
    Maintenance(service.store).backup(backup)
    removed = Maintenance(service.store).gc(minimum_age_seconds=60)
    assert removed["unreferenced_blobs_removed"] == 1
    assert not service.store.blobs.path(unused).exists()
    assert service.store.blobs.verify(context["bundle_sha"])
    assert service.repositories.git(repo["id"]).resolve(accepted["commit"]) == accepted["commit"]

    restored = Maintenance.restore(backup, tmp_path / "restored-author")
    restored_service = AuthorService(restored.home)
    assert restored_service.tasks.get(task["id"])["current_commit"] == accepted["commit"]
    assert restored.blobs.verify(context["bundle_sha"])
    assert restored_service.repositories.get(repo["id"])["id"] == repo["id"]


def test_requirements_evaluator_keeps_missing_runs_unrun_and_verifies_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(requirements_eval, "current_commit", lambda: "abc123")
    baseline = requirements_eval.evaluate(None)
    assert baseline["overall"] == "INCOMPLETE"
    assert len(baseline["scenarios"]) == 52
    assert baseline["counts"]["NOT_RUN"] == 52
    assert all(row["status"] == "NOT_RUN_IN_THIS_RESEARCH" for row in baseline["scenarios"])
    assert not any(row["evaluated_status"] == "PASS" for row in baseline["scenarios"])
    # Original identity is pinned to the research register; the old mistaken
    # backup/disk-full labels must never replace the source meanings.
    t23 = next(row for row in baseline["scenarios"] if row["id"] == "T23")
    assert t23["title"] == "Long tool call versus durable job"
    assert t23["requires_actual_ordinary_chat"] is False

    artifact = tmp_path / "evidence.txt"
    artifact.write_text("reproducible evidence\n", encoding="utf-8")
    record = {
        "schema_version": 1,
        "runs": {
            "T23": {
                "status": "PASS",
                "commit": "abc123",
                "command": "python -m pytest tests/authoring/test_operational_requirements.py -q",
                "artifacts": [{"path": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            }
        },
    }
    evidence = tmp_path / "qualification.json"
    evidence.write_text(json.dumps(record), encoding="utf-8")
    checked = requirements_eval.evaluate(evidence)
    accepted = next(row for row in checked["scenarios"] if row["id"] == "T23")
    assert accepted["evaluated_status"] == "PASS"
    assert accepted["status"] == "NOT_RUN_IN_THIS_RESEARCH"
    assert checked["counts"]["NOT_RUN"] == 51

    record["runs"]["T23"]["artifacts"][0]["sha256"] = "0" * 64
    evidence.write_text(json.dumps(record), encoding="utf-8")
    rejected = requirements_eval.evaluate(evidence)
    assert next(row for row in rejected["scenarios"] if row["id"] == "T23")["evaluated_status"] == "FAIL"


def test_interface_evaluator_measures_hub_and_synthetic_author_profiles():
    import author_interface_eval as interface_eval

    absent = interface_eval.evaluate(None)
    measured = absent["measured"]
    assert measured["ab_capture"]["status"] == "NOT_RUN"
    assert measured["ab_capture"]["actual_chat_outcomes"] == "NOT_RUN"
    assert measured["hub"]["catalog"]["tools"] == 31
    assert measured["hub"]["output_contracts"]["serialized_utf8_bytes"] > 0
    profiles = measured["author"]["profiles"]
    assert set(profiles) == {"reader", "operator", "author", "operator-qualified-synthetic", "author-qualified-synthetic"}
    assert all(not item["profile_input"]["live_qualified"] for item in profiles.values())
    assert profiles["operator-qualified-synthetic"]["tool_list"]["count"] > profiles["operator"]["tool_list"]["count"]
    assert profiles["author-qualified-synthetic"]["tool_list"]["count"] > profiles["author"]["tool_list"]["count"]
    assert profiles["author-qualified-synthetic"]["output_schemas"]["count"] == profiles["author-qualified-synthetic"]["tool_list"]["count"]


def test_interface_evaluator_pairs_actual_capture_with_status_coverage_and_raw_bytes(tmp_path):
    import author_interface_eval as interface_eval

    capture = tmp_path / "observed.jsonl"
    base = {"case_id": "G01", "status": "PASS", "required_tool_ids": ["author_read_file"],
            "tool_ids": ["author_read_file"], "execution_surface": "ordinary_chat", "profile_id": "fixture-profile"}
    rows = [
        {**base, "variant": "full-text+structured", "input_raw": "full text µ", "output_raw": '{"ok":true}'},
        {**base, "variant": "concise+structured", "input_raw": "short", "output_raw": '{"ok":true}'},
    ]
    capture.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    observed = interface_eval.evaluate(capture)["measured"]["ab_capture"]
    assert observed["status"] == "CAPTURED"
    assert observed["actual_chat_outcomes"] == "CAPTURED"
    case = observed["cases"][0]
    assert case["status_match"] and case["coverage_comparable"]
    assert case["variants"]["full-text+structured"]["input_bytes"] == len("full text µ".encode())
    assert case["variants"]["concise+structured"]["coverage"]["fraction"] == 1.0


def test_interface_evaluator_does_not_call_non_chat_capture_actual_chat(tmp_path):
    import author_interface_eval as interface_eval

    capture = tmp_path / "not-chat.jsonl"
    row = {"case_id": "G01", "variant": "full-text+structured", "status": "PASS",
           "input_raw": "x", "output_raw": "y", "execution_surface": "api", "profile_id": "synthetic"}
    capture.write_text(json.dumps(row) + "\n")
    result = interface_eval.compare_ab(capture)
    assert result["status"] == "PARTIAL_CAPTURE"
    assert result["actual_chat_outcomes"] == "NOT_RUN"


def test_interface_evaluator_requires_complete_ordinary_chat_pairs(tmp_path):
    import author_interface_eval as interface_eval

    capture = tmp_path / "unpaired-chat.jsonl"
    row = {"case_id": "G01", "variant": "full-text+structured", "status": "PASS",
           "input_raw": "x", "output_raw": "y", "execution_surface": "ordinary_chat",
           "profile_id": "observed-profile"}
    capture.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = interface_eval.compare_ab(capture)
    assert result["status"] == "PARTIAL_CAPTURE"
    assert result["actual_chat_outcomes"] == "NOT_RUN"
    assert result["cases"][0]["paired"] is False


def test_interface_evaluator_rejects_unknown_golden_case(tmp_path):
    import author_interface_eval as interface_eval

    capture = tmp_path / "unknown-case.jsonl"
    row = {"case_id": "NOT-A-GOLDEN-CASE", "variant": "full-text+structured", "status": "PASS",
           "input_raw": "x", "output_raw": "y"}
    capture.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown golden prompt case ID"):
        interface_eval.compare_ab(capture)
