from __future__ import annotations

import json

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.packages.inbox import PackageInbox
from patchbay.authoring.persistence.control import Control
from patchbay.authoring.recovery import Recovery
from patchbay.authoring.util import canonical, utc_now
from test_core import package_for


def test_operation_cursor_detects_state_change_without_event(setup_author):
    service, _, _, task, _ = setup_author
    for number in range(2):
        service.store.begin_operation(
            kind="cursor.fixture", task_id=task["id"], key=f"cursor-{number}",
            fingerprint=f"fixture-{number}", payload={"n": number},
        )

    page = Recovery(service.store).operations(task["id"], limit=1)
    operation_id = page["operations"][0]["id"]
    # Model a supervisor transition that updates the durable operation but has
    # not appended an event yet; every visible phase transition still invalidates.
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET state='running',phase='verifying',updated=? WHERE id=?",
                     (utc_now(), operation_id))

    with pytest.raises(AuthorError) as stale:
        Recovery(service.store).operations(task["id"], limit=1, offset=1, cursor=page["cursor"])
    assert stale.value.code == "STALE_CURSOR"


def test_recovery_surfaces_pending_package_and_uses_task_bound_events(setup_author, tmp_path):
    service, _, _, task, _ = setup_author
    package = package_for(service, task, tmp_path, {"new.txt": "awaiting explicit approval\n"})
    operation = PackageInbox(service.store).ingest(package)

    assert operation["state"] == "awaiting_approval"
    row = service.store.one("SELECT task_id FROM operations WHERE id=?", (operation["id"],))
    assert row["task_id"] == task["id"]
    recovery = Recovery(service.store).get(task["id"])
    assert recovery["state"] == "awaiting_approval"
    assert "explicitly approve" in recovery["required_judgment"]


def test_materializer_phase_events_are_task_bound_and_discoverable(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"new.txt": "materialized\n"})
    inbox = PackageInbox(service.store)
    inbox.grant(task["id"], lease["token"], lease["epoch"], auto_validate=True, key="event-grant")
    accepted = inbox.ingest(package)
    operation_id = accepted["result"]["operation_id"]
    events = service.store.rows("SELECT event,task_id,payload FROM events WHERE task_id=? ORDER BY seq", (task["id"],))
    related = [row for row in events if json.loads(row["payload"]).get("operation_id") == operation_id]
    assert {row["event"] for row in related} >= {
        "package.constructing", "package.candidate_pinned", "revision.accepted"
    }
    assert all(row["task_id"] == task["id"] for row in related)


def test_revocation_does_not_hide_uncertain_execution(setup_author):
    service, _, _, task, _ = setup_author
    operation, _ = service.store.begin_operation(
        kind="checks.run", task_id=task["id"], key="uncertain-fixture",
        fingerprint="uncertain-fixture", payload={},
    )
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET state='unknown_execution',phase='ownership_unresolved',updated=? WHERE id=?",
                     (utc_now(), operation["id"]))
    Control(service.store).revoke(task["id"], key="revoke-uncertain", actor="operator")

    recovery = Recovery(service.store).get(task["id"])
    assert recovery["state"] == "awaiting_controller"
    assert "Reconcile the existing uncertain operation" in recovery["required_judgment"]
    assert "grant is also revoked" in recovery["required_judgment"]
    assert operation["id"] in {item["id"] for item in recovery["active_operations"]}


def test_old_policy_receipt_does_not_make_current_candidate_green(setup_author):
    service, _, _, task, _ = setup_author
    candidate = task["revision"]
    receipt = {
        "classification": "READY_FOR_REVIEW", "state": "completed", "task_id": task["id"],
        "revision": 0, "commit": candidate["commit_oid"], "tree": candidate["tree_oid"],
        "policy_digest": "old-policy-digest",
        "verification_receipt": {"outcome": "passed", "source_unchanged": True},
    }
    sha = service.store.blobs.put(canonical(receipt))
    operation_id = "op_" + "a" * 32
    now = utc_now()
    with service.store.transaction() as conn:
        conn.execute(
            "INSERT INTO operations(id,task_id,kind,idempotency_key,fingerprint,revision,state,phase,payload,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], "checks.run", "old-receipt", "old-receipt", 0,
             "completed", "terminal", "{}", now, now),
        )
        conn.execute(
            "INSERT INTO runs(id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,receipt_sha,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], 0, candidate["commit_oid"], candidate["tree_oid"], "{}",
             "profile", "completed", "proven", sha, now, now),
        )

    recovery = Recovery(service.store).get(task["id"])
    assert recovery["state"] == "awaiting_controller"
    assert "does not prove the current candidate and policy" in recovery["required_judgment"]


def test_capacity_replay_does_not_add_records_or_blob_refs(setup_author, tmp_path, monkeypatch):
    service, _, _, task, _ = setup_author
    inbox_module = __import__("patchbay.authoring.packages.inbox", fromlist=["_MAX_RECORDED"])
    monkeypatch.setattr(inbox_module, "_MAX_RECORDED", 1)
    first = package_for(service, task, tmp_path, {"one.txt": "first\n"})
    second = package_for(service, task, tmp_path, {"two.txt": "second\n"})
    inbox = PackageInbox(service.store)

    accepted = inbox.ingest(first)
    assert accepted["state"] == "awaiting_approval"
    references_before = service.store.rows("SELECT owner,sha FROM blob_refs ORDER BY owner,sha")
    replay = inbox.ingest(first)
    assert replay["id"] == accepted["id"]
    with pytest.raises(AuthorError) as full:
        inbox.ingest(second)
    assert full.value.code == "RESOURCE_LIMIT"
    assert service.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox'")["n"] == 1
    assert service.store.rows("SELECT owner,sha FROM blob_refs ORDER BY owner,sha") == references_before


def test_pending_capacity_blocks_new_package_without_recording_it(setup_author, tmp_path, monkeypatch):
    service, _, _, task, _ = setup_author
    inbox_module = __import__("patchbay.authoring.packages.inbox", fromlist=["_MAX_PENDING"])
    monkeypatch.setattr(inbox_module, "_MAX_RECORDED", 10)
    monkeypatch.setattr(inbox_module, "_MAX_PENDING", 1)
    first = package_for(service, task, tmp_path, {"one.txt": "first pending\n"})
    second = package_for(service, task, tmp_path, {"two.txt": "second pending\n"})
    inbox = PackageInbox(service.store)

    pending = inbox.ingest(first)
    assert pending["state"] == "awaiting_approval"
    references_before = service.store.rows("SELECT owner,sha FROM blob_refs ORDER BY owner,sha")
    with pytest.raises(AuthorError) as full:
        inbox.ingest(second)
    assert full.value.code == "RESOURCE_LIMIT"
    assert service.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.inbox'")["n"] == 1
    assert service.store.rows("SELECT owner,sha FROM blob_refs ORDER BY owner,sha") == references_before
