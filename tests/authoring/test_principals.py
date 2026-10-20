from __future__ import annotations

import json

import pytest

from patchbay.authoring.authority import AuthIdentity, assert_operator, current_identity, identity_context
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.util import fingerprint


def _issue(access, principal_id, role, *, key, task_ids=None):
    return access.issue(principal_id, role, task_ids, key=key)


def test_authenticate_legacy_roles_and_issue_private_replay(setup_author):
    service, _source, _repo, task, _lease = setup_author
    access = Access(service.store)
    tokens = json.loads((service.store.home / "tokens.json").read_text())
    assert access.role("Bearer " + tokens["author"]) == "author"
    legacy = access.authenticate("Bearer " + tokens["author"])
    assert legacy == AuthIdentity("local-operator", "author", None, legacy=True)
    assert current_identity().principal_id == "local-operator"

    issued = _issue(access, "reviewer-one", "author", key="issue-reviewer-one", task_ids=[task["id"]])
    replay = _issue(access, "reviewer-one", "author", key="issue-reviewer-one", task_ids=[task["id"]])
    assert issued == replay
    identity = access.authenticate("Bearer " + issued["token"])
    assert identity == AuthIdentity("reviewer-one", "author", (task["id"],))
    access.assert_task(task["id"], identity)
    with pytest.raises(AuthorError) as denied:
        access.assert_task("another-task", identity)
    assert denied.value.code == "FORBIDDEN"

    operation = service.store.operation(issued["request_operation_id"])
    assert issued["token"] not in json.dumps(operation)
    assert issued["token"] not in "\n".join(row["payload"] for row in service.store.rows("SELECT payload FROM events"))
    assert issued["token"] not in "\n".join(row["value"] for row in service.store.rows("SELECT value FROM metadata WHERE key NOT LIKE 'author.principal.issue.%'"))


def test_principal_local_idempotency_and_scope_binding(setup_author):
    service, _source, repo, task, _lease = setup_author
    access = Access(service.store)
    alice = access.authenticate("Bearer " + _issue(access, "alice", "author", key="issue-alice",
                                                     task_ids=[task["id"]])["token"])
    bob = access.authenticate("Bearer " + _issue(access, "bob", "author", key="issue-bob")["token"])
    request_fingerprint = fingerprint({"same": "request"})

    with identity_context(alice):
        alice_op, created = service.store.begin_operation(kind="principal.test", task_id=task["id"], key="same-key",
                         fingerprint=request_fingerprint, payload={"value": 1})
    assert created
    with identity_context(bob):
        bob_op, created = service.store.begin_operation(kind="principal.test", task_id=task["id"], key="same-key",
                       fingerprint=request_fingerprint, payload={"value": 1})
    assert created and bob_op["id"] != alice_op["id"]
    other_task = service.tasks.create(repo["id"], "Separate task for scoped status visibility", key="other-task")
    with identity_context(bob):
        other_op, created = service.store.begin_operation(kind="principal.test", task_id=other_task["id"],
                         key="other-task-key", fingerprint=request_fingerprint, payload={"value": 1})
    assert created
    with identity_context(alice):
        with pytest.raises(AuthorError) as status_denied:
            service.store.operation(other_op["id"])
        assert status_denied.value.code == "FORBIDDEN"
    with identity_context(alice):
        replay, created = service.store.begin_operation(kind="principal.test", task_id=task["id"], key="same-key",
                       fingerprint=request_fingerprint, payload={"value": 1})
        assert not created and replay["id"] == alice_op["id"]
        with pytest.raises(AuthorError) as conflict:
            service.store.begin_operation(kind="principal.test", task_id=task["id"], key="same-key",
                       fingerprint=fingerprint({"different": "request"}), payload={"value": 2})
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_lease_owner_is_principal_bound_and_operator_takeover_fences_old_writer(setup_author):
    service, _source, _repo, task, old_lease = setup_author
    access = Access(service.store)
    alice_token = _issue(access, "writer-alice", "author", key="issue-writer-alice")["token"]
    bob_token = _issue(access, "operator-bob", "operator", key="issue-operator-bob", task_ids=[task["id"]])["token"]
    alice = access.authenticate("Bearer " + alice_token)
    bob = access.authenticate("Bearer " + bob_token)

    # The initial fixture lease belongs to the trusted local operator. Let it
    # expire so Alice can claim normally, without simulating an implicit takeover.
    with service.store.transaction() as conn:
        conn.execute("UPDATE leases SET expires=0 WHERE task_id=?", (task["id"],))
    with identity_context(alice):
        first = service.leases.claim(task["id"], key="alice-claim")
        renewed = service.leases.claim(task["id"], token=first["token"], key="alice-renew")
        service.leases.assert_owned(task["id"], first["token"], first["epoch"])
    assert renewed["token"] == first["token"] and renewed["epoch"] == first["epoch"]

    with identity_context(bob):
        assert_operator(task["id"])
        with pytest.raises(AuthorError) as global_denied:
            assert_operator()
        assert global_denied.value.code == "FORBIDDEN"
        with pytest.raises(AuthorError) as cross_principal:
            service.leases.assert_owned(task["id"], first["token"], first["epoch"])
        assert cross_principal.value.code == "LEASE_LOST"
        replacement = service.leases.claim(task["id"], token=first["token"], takeover=True, key="operator-takeover")
        service.leases.assert_owned(task["id"], replacement["token"], replacement["epoch"])

    with identity_context(alice):
        with pytest.raises(AuthorError) as fenced:
            service.leases.assert_owned(task["id"], first["token"], first["epoch"])
        assert fenced.value.code == "LEASE_LOST"
        with pytest.raises(AuthorError) as denied_takeover:
            service.leases.claim(task["id"], token=replacement["token"], takeover=True, key="author-takeover")
        assert denied_takeover.value.code == "FORBIDDEN"
    assert old_lease["epoch"] < first["epoch"]


def test_request_id_is_durable_but_not_part_of_logical_replay(setup_author):
    from patchbay.authoring.authority import request_context

    service, _source, _repo, task, _lease = setup_author
    access = Access(service.store)
    principal = access.authenticate("Bearer " + _issue(access, "requester", "author", key="issue-requester")["token"])
    request_fp = fingerprint({"stable": "meaning"})
    with identity_context(principal), request_context("http:request-1"):
        first, created = service.store.begin_operation(kind="request.test", task_id=task["id"], key="logical-key",
                           fingerprint=request_fp, payload={})
    assert created
    with identity_context(principal), request_context("http:request-2"):
        replay, created = service.store.begin_operation(kind="request.test", task_id=task["id"], key="logical-key",
                           fingerprint=request_fp, payload={})
    assert not created and replay["id"] == first["id"]
    assert json.loads(replay["payload"])["_request_id"] == "http:request-1"
    assert service.store.operation(first["id"])["request_id"] == "http:request-1"


def test_credential_revoke_and_renewal_preserve_principal_lease_owner(setup_author):
    service, _source, _repo, task, _lease = setup_author
    access = Access(service.store)
    first = _issue(access, "steady-principal", "author", key="credential-first")
    renewed = _issue(access, "steady-principal", "author", key="credential-renewed")
    first_identity = access.authenticate("Bearer " + first["token"])
    renewed_identity = access.authenticate("Bearer " + renewed["token"])
    assert first_identity.principal_id == renewed_identity.principal_id == "steady-principal"
    assert first["credential_id"] != renewed["credential_id"]
    assert first["token"] != renewed["token"]

    with service.store.transaction() as conn:
        conn.execute("UPDATE leases SET expires=0 WHERE task_id=?", (task["id"],))
    with identity_context(renewed_identity):
        lease = service.leases.claim(task["id"], key="steady-claim")
    with identity_context(first_identity):
        service.leases.assert_owned(task["id"], lease["token"], lease["epoch"])

    revoked = access.revoke(first["credential_id"], key="revoke-first")
    assert revoked == access.revoke(first["credential_id"], key="revoke-first")
    assert revoked["revoked"] is True
    with pytest.raises(AuthorError) as old_token:
        access.authenticate("Bearer " + first["token"])
    assert old_token.value.code == "UNAUTHORIZED"
    with identity_context(renewed_identity):
        service.leases.assert_owned(task["id"], lease["token"], lease["epoch"])
