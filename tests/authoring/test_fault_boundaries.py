"""Portable crash and persistence fault boundaries for Author package intake."""
from __future__ import annotations

import errno
import json
import os
import time
import uuid

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.receipts import new_receipt, record_receipt, classify
from patchbay.authoring.packages.archive import PackageReader
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.policy import Policies
from patchbay.authoring.revisions.git_objects import Git
from patchbay.authoring.service import AuthorService
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.util import canonical, fingerprint

from test_core import package_for, submit


def _candidate_ref(operation_id: str) -> str:
    return "refs/author/candidates/" + operation_id


def _assert_candidate_bytes(service, repo_id, result, path="fault.txt", expected=b"durable payload\n"):
    git = service.repositories.git(repo_id)
    assert git.read_file(result["commit"], path) == expected
    assert git.tree(result["commit"]) == result["tree"]


def _seed_check_run(service, task, *, key: str) -> tuple[dict, dict]:
    current = service.tasks.get(task["id"])
    revision = current["revision"]
    profile = current["policy"]["profile"]
    op, _ = service.store.begin_operation(kind="checks.run", task_id=task["id"], key=key,
                                          fingerprint=fingerprint({"key": key}), payload={"selected": []},
                                          revision=current["current_revision"])
    now = time.time()
    with service.store.transaction() as conn:
        conn.execute("""INSERT INTO runs
            (id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,environment_digest,created,updated)
            VALUES(?,?,?,?,?,?,?,'running','proven',NULL,?,?)""",
            (op["id"], task["id"], current["current_revision"], current["current_commit"],
             revision["tree_oid"], canonical(profile).decode(), fingerprint(profile), now, now))
        conn.execute("UPDATE operations SET state='running',phase='verifying',updated=? WHERE id=?",
                     (now, op["id"]))
    return service.store.one("SELECT * FROM runs WHERE id=?", (op["id"],)), service.tasks.get(task["id"])


def _synthetic_receipt(service, task, run, *, passed: bool) -> dict:
    """Build only the persistence-helper input; this does not run a check."""
    receipt = new_receipt(service.store, run, task["policy_digest"])
    environment = {"backend": "fixture-only", "evidence": "synthetic persistence input; no process executed"}
    environment["environment_digest"] = fingerprint(environment)
    receipt.update(environment=environment, environment_digest=environment["environment_digest"],
                   cleanup="proven", source_changes_during_checks=[])
    if passed:
        profile = json.loads(run["profile"])
        receipt["source_observation"].update(state="complete", boundaries=["initial"])
        for stage in ("setup", "checks"):
            for check in profile.get(stage, []):
                receipt[stage].append({"id": check["id"], "required": check["required"], "state": "completed",
                    "argv": check["argv"], "exit_code": 0, "verification": "passed", "cleanup": {"state": "proven"}})
                receipt["source_observation"]["boundaries"].append(stage + ":" + check["id"])
    classify(receipt, {check["id"] for check in json.loads(run["profile"])["checks"] if check["required"]})
    return receipt


def test_blob_stream_interruption_removes_partial_temporary_file(tmp_path):
    from patchbay.authoring.persistence.blobs import Blobs

    class BrokenStream:
        def __init__(self):
            self.calls = 0

        def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"persisted prefix"
            raise RuntimeError("simulated process loss while streaming")

    blobs = Blobs(tmp_path / "blob-store")
    with pytest.raises(RuntimeError, match="simulated process loss"):
        blobs.put_stream(BrokenStream(), limit=1024)

    assert not list(blobs.root.glob(".blob-*"))
    assert not list(blobs.root.glob("[0-9a-f][0-9a-f]/*"))


def test_package_blobs_survive_crash_before_database_reference_and_recover(setup_author, tmp_path, monkeypatch):
    service, source, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    imported = PackageReader(service.store).read(package_path)
    original_reference = service.store.reference
    fired = False

    def fail_before_reference(conn, owner, *hashes):
        nonlocal fired
        if owner.startswith("package:") and not fired:
            fired = True
            raise RuntimeError("simulated process loss after blob persistence")
        return original_reference(conn, owner, *hashes)

    monkeypatch.setattr(service.store, "reference", fail_before_reference)
    with pytest.raises(RuntimeError, match="after blob persistence"):
        submit(service, task, package_path, lease, "blob-reference-boundary")

    operation = service.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                  (task["id"],))
    assert operation["phase"] == "accepted"
    assert service.tasks.get(task["id"])["current_revision"] == 0
    assert not service.store.rows("SELECT * FROM packages WHERE fingerprint=?", (imported.fingerprint,))
    assert not service.store.rows("SELECT * FROM blob_refs WHERE owner=?", ("package:" + imported.fingerprint,))
    assert service.store.blobs.verify(imported.archive_sha)
    assert all(service.store.blobs.verify(sha) for sha in imported.payloads.values())
    assert service.repositories.git(repo["id"]).run(["show-ref", "--verify", _candidate_ref(operation["id"])], check=False) == b""
    assert (source / "app.py").read_bytes().endswith(b"a - b\n")

    monkeypatch.setattr(service.store, "reference", original_reference)
    reopened = AuthorService(service.store.home)
    recovered = reopened.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])

    assert recovered["revision"] == 1
    _assert_candidate_bytes(reopened, repo["id"], recovered)
    assert reopened.store.blobs.verify(imported.archive_sha)
    assert all(reopened.store.blobs.verify(sha) for sha in imported.payloads.values())
    assert reopened.tasks.get(task["id"])["current_revision"] == 1


def test_tree_persisted_without_candidate_ref_is_rebuilt_after_reopen(setup_author, tmp_path, monkeypatch):
    service, _, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    original = Git.text
    captured = {}

    def stop_after_tree(self, args, **kwargs):
        value = original(self, args, **kwargs)
        if args[:1] == ["write-tree"] and not captured:
            captured["tree"] = value
            raise RuntimeError("simulated process loss after Git tree write")
        return value

    monkeypatch.setattr(Git, "text", stop_after_tree)
    with pytest.raises(RuntimeError, match="after Git tree write"):
        submit(service, task, package_path, lease, "tree-before-ref")
    operation = service.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                  (task["id"],))
    git = service.repositories.git(repo["id"])
    assert operation["phase"] == "constructing"
    assert git.text(["cat-file", "-t", captured["tree"]]) == "tree"
    assert git.run(["show-ref", "--verify", _candidate_ref(operation["id"])], check=False) == b""
    assert service.tasks.get(task["id"])["current_revision"] == 0

    monkeypatch.setattr(Git, "text", original)
    reopened = AuthorService(service.store.home)
    recovered = reopened.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])
    assert recovered["tree"] == captured["tree"]
    _assert_candidate_bytes(reopened, repo["id"], recovered)


def test_acceptance_transaction_rollback_keeps_candidate_for_recovery(setup_author, tmp_path, monkeypatch):
    service, _, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    original_event = service.store.event

    def interrupt_accept(conn, event, task_id, payload):
        if event == "revision.accepted":
            raise RuntimeError("simulated process loss inside task acceptance transaction")
        return original_event(conn, event, task_id, payload)

    monkeypatch.setattr(service.store, "event", interrupt_accept)
    with pytest.raises(RuntimeError, match="inside task acceptance transaction"):
        submit(service, task, package_path, lease, "accept-cas-boundary")
    operation = service.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                  (task["id"],))
    ref = _candidate_ref(operation["id"])
    assert operation["phase"] == "candidate_pinned"
    assert service.tasks.get(task["id"])["current_revision"] == 0
    assert len(service.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 1
    candidate_oid = service.repositories.git(repo["id"]).resolve(ref)

    monkeypatch.setattr(service.store, "event", original_event)
    reopened = AuthorService(service.store.home)
    recovered = reopened.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])
    assert recovered["commit"] == candidate_oid
    assert reopened.tasks.get(task["id"])["current_revision"] == 1
    assert len(reopened.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 2
    _assert_candidate_bytes(reopened, repo["id"], recovered)


def test_lost_post_accept_response_replays_one_revision_and_gc_retains_references(setup_author, tmp_path, monkeypatch):
    service, _, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    original_accept = service.materializer._accept
    accepted = {}

    def lose_response(*args, **kwargs):
        accepted["result"] = original_accept(*args, **kwargs)
        raise RuntimeError("simulated lost response after durable task CAS")

    monkeypatch.setattr(service.materializer, "_accept", lose_response)
    with pytest.raises(RuntimeError, match="lost response"):
        submit(service, task, package_path, lease, "lost-after-accept")
    durable = accepted["result"]
    operation = service.store.one("SELECT * FROM operations WHERE id=?", (durable["operation_id"],))
    assert operation["state"] == "completed"
    assert service.tasks.get(task["id"])["current_revision"] == 1

    monkeypatch.setattr(service.materializer, "_accept", original_accept)
    reopened = AuthorService(service.store.home)
    recovered = reopened.materializer.recover(durable["operation_id"], token=lease["token"], epoch=lease["epoch"])
    replayed = submit(reopened, task, package_path, lease, "lost-after-accept")
    assert recovered == durable == replayed
    assert len(reopened.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 2
    _assert_candidate_bytes(reopened, repo["id"], recovered)
    unused = reopened.store.blobs.put(b"old unreferenced fault fixture")
    os.utime(reopened.store.blobs.path(unused), (time.time() - 3600, time.time() - 3600))
    removed = Maintenance(reopened.store).gc(minimum_age_seconds=60)
    assert removed["unreferenced_blobs_removed"] == 1
    assert not reopened.store.blobs.path(unused).exists()
    assert reopened.store.blobs.verify(reopened.store.one("SELECT archive_sha FROM packages WHERE fingerprint=?",
                                                         (reopened.store.one("SELECT fingerprint FROM revisions WHERE task_id=? AND number=1",
                                                                             (task["id"],))["fingerprint"],))["archive_sha"])
    assert reopened.repositories.git(repo["id"]).resolve(_candidate_ref(durable["operation_id"])) == durable["commit"]


def test_enospc_before_durable_package_blob_never_accepts_operation(setup_author, tmp_path, monkeypatch):
    service, source, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    original_put_stream = service.store.blobs.put_stream

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "injected full filesystem")

    monkeypatch.setattr(service.store.blobs, "put_stream", disk_full)
    with pytest.raises(OSError) as error:
        submit(service, task, package_path, lease, "enospc-package")
    assert error.value.errno == errno.ENOSPC
    assert not service.store.rows("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                  (task["id"],))
    assert service.tasks.get(task["id"])["current_revision"] == 0
    assert service.repositories.git(repo["id"]).run(["for-each-ref", "refs/author/candidates"]) == b""
    assert (source / "app.py").read_bytes().endswith(b"a - b\n")
    monkeypatch.setattr(service.store.blobs, "put_stream", original_put_stream)


def test_enospc_receipt_blob_failure_does_not_acknowledge_pass(setup_author, monkeypatch):
    service, _, _, task, _ = setup_author
    run, current = _seed_check_run(service, task, key="receipt-enospc")
    receipt = _synthetic_receipt(service, current, run, passed=True)
    original_put = service.store.blobs.put

    def disk_full(_data):
        raise OSError(errno.ENOSPC, "injected full filesystem during receipt persistence")

    monkeypatch.setattr(service.store.blobs, "put", disk_full)
    with pytest.raises(OSError) as error:
        record_receipt(service.store, run["id"], receipt)
    assert error.value.errno == errno.ENOSPC
    assert service.store.one("SELECT state,receipt_sha FROM runs WHERE id=?", (run["id"],)) == {
        "state": "running", "receipt_sha": None}
    assert service.store.one("SELECT state,result FROM operations WHERE id=?", (run["id"],))["state"] == "running"
    assert service.tasks.get(task["id"])["status"] != "ready_for_review"
    assert not service.store.rows("SELECT * FROM blob_refs WHERE owner=?", ("receipt:" + run["id"],))
    monkeypatch.setattr(service.store.blobs, "put", original_put)


def test_receipt_blob_written_before_reference_is_recoverable_without_duplicate_receipt(setup_author, monkeypatch):
    service, _, _, task, _ = setup_author
    run, current = _seed_check_run(service, task, key="receipt-reference-crash")
    receipt = _synthetic_receipt(service, current, run, passed=False)
    receipt["completed"] = "2026-10-20T12:00:00Z"
    original_reference = service.store.reference
    original_put = service.store.blobs.put
    persisted = {}
    fired = False

    def capture_receipt_blob(data):
        sha = original_put(data)
        persisted["sha"] = sha
        return sha

    def crash_reference(conn, owner, *hashes):
        nonlocal fired
        if owner == "receipt:" + run["id"] and not fired:
            fired = True
            raise RuntimeError("simulated loss after receipt blob persistence")
        return original_reference(conn, owner, *hashes)

    monkeypatch.setattr(service.store.blobs, "put", capture_receipt_blob)
    monkeypatch.setattr(service.store, "reference", crash_reference)
    with pytest.raises(RuntimeError, match="after receipt blob persistence"):
        record_receipt(service.store, run["id"], receipt)
    receipt_blob = persisted["sha"]
    assert service.store.blobs.verify(receipt_blob)
    assert service.store.one("SELECT state,receipt_sha FROM runs WHERE id=?", (run["id"],)) == {
        "state": "running", "receipt_sha": None}

    monkeypatch.setattr(service.store, "reference", original_reference)
    monkeypatch.setattr(service.store.blobs, "put", original_put)
    recorded = record_receipt(service.store, run["id"], receipt)
    reopened = AuthorService(service.store.home)
    final = reopened.store.one("SELECT state,receipt_sha FROM runs WHERE id=?", (run["id"],))
    assert recorded["state"] == "failed"
    assert final == {"state": "failed", "receipt_sha": receipt_blob}
    assert reopened.store.blobs.verify(receipt_blob)
    assert reopened.store.rows("SELECT * FROM blob_refs WHERE owner=?", ("receipt:" + run["id"],))
    assert reopened.store.one("SELECT state FROM operations WHERE id=?", (run["id"],))["state"] == "failed"


def test_admitted_old_policy_receipt_is_retained_as_historical_only(setup_author):
    service, _, _, task, _ = setup_author
    run, current = _seed_check_run(service, task, key="old-policy-receipt")
    admitted_policy = current["policy_digest"]
    operation = service.store.one("SELECT payload FROM operations WHERE id=?", (run["id"],))
    payload = json.loads(operation["payload"])
    payload["policy"] = admitted_policy
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET payload=? WHERE id=?", (canonical(payload).decode(), run["id"]))

    revised = TaskPolicy.model_validate(current["policy"]).model_copy(update={"allow_background_checks": False})
    change = Policies(service.store).update(task["id"], revised, expected_digest=admitted_policy,
                                            actor="fixture-operator", key="old-policy-revision")
    assert change["previous_policy_digest"] == admitted_policy
    assert service.tasks.get(task["id"])["status"] == "policy_changed"

    receipt = _synthetic_receipt(service, current, run, passed=True)
    receipt["completed"] = "2026-10-20T12:00:00Z"
    recorded = record_receipt(service.store, run["id"], receipt)
    historical = Feedback(service.store).get(task["id"])

    assert recorded["state"] == "completed"
    assert historical["receipt"]["policy_digest"] == admitted_policy
    assert historical["historical_classification"] == "READY_FOR_REVIEW"
    assert historical["evidence_current"] is False
    assert historical["classification"] == "CHECKS_INCOMPLETE"
    assert service.tasks.get(task["id"])["status"] == "policy_changed"


def test_missing_incompatible_and_corrupt_state_fail_closed(tmp_path):
    with pytest.raises(AuthorError) as missing:
        Store(tmp_path / "missing-state")
    assert missing.value.code == "STATE_UNAVAILABLE"

    incompatible_home = tmp_path / "incompatible-state"
    incompatible = Store.initialize(incompatible_home)
    identity = json.loads((incompatible_home / "identity.json").read_text())
    identity["schema_version"] = 999
    (incompatible_home / "identity.json").write_bytes(canonical(identity))
    with pytest.raises(AuthorError) as incompatible_error:
        Store(incompatible_home)
    assert incompatible_error.value.code == "INCOMPATIBLE_STATE"

    corrupt_home = tmp_path / "corrupt-state"
    corrupt = Store.initialize(corrupt_home)
    identity = json.loads((corrupt_home / "identity.json").read_text())
    identity["instance_id"] = "instance_mismatched"
    (corrupt_home / "identity.json").write_bytes(canonical(identity))
    with pytest.raises(AuthorError) as corrupt_error:
        Store(corrupt_home)
    assert corrupt_error.value.code == "CORRUPT_STATE"
    assert corrupt.identity["instance_id"] != "instance_mismatched"


def test_corrupt_payload_fails_closed_without_candidate_or_partial_revision(setup_author, tmp_path):
    service, _, repo, task, lease = setup_author
    package_path = package_for(service, task, tmp_path, {"fault.txt": "durable payload\n"})
    imported = PackageReader(service.store).read(package_path)
    payload_sha = next(iter(imported.payloads.values()))
    service.store.blobs.path(payload_sha).write_bytes(b"corrupted payload")

    with pytest.raises(AuthorError) as error:
        service.materializer.submit_imported(task["id"], imported, key="corrupt-payload",
                                            token=lease["token"], epoch=lease["epoch"])

    assert error.value.code == "CORRUPT_STATE"
    operation = service.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                  (task["id"],))
    assert operation["state"] == "failed"
    assert service.tasks.get(task["id"])["current_revision"] == 0
    assert len(service.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 1
    assert service.repositories.git(repo["id"]).run(["for-each-ref", "refs/author/candidates"]) == b""
