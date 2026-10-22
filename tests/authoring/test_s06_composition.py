"""Deterministic composition state; source eligibility is synthetic, not execution proof."""
import json
import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.publication.integration import Integration
from test_core import package_for, submit


def test_deterministic_author_conflict_has_a_durable_decision(
    setup_author, tmp_path, monkeypatch
):
    service, source_checkout, repo, first, first_lease = setup_author
    second = service.tasks.create(repo["id"], "second independent fixture", key="second-fixture")
    second_lease = service.leases.claim(second["id"])
    sources = []
    for task, lease, body in [
        (first, first_lease, "def add(a, b): return a + b\n"),
        (second, second_lease, "def add(a, b): return (a + b) * 2\n"),
    ]:
        accepted = submit(
            service, task,
            package_for(service, task, tmp_path, {"app.py": body}),
            lease, "source-" + task["id"])
        sources.append({"task_id": task["id"], "revision": accepted["revision"]})

    # This substitutes only the composer's eligibility precondition.
    # It MUST NOT be interpreted as canonical test execution or deployment proof.
    monkeypatch.setattr(
        Feedback, "get",
        lambda self, task_id, revision=None: {
            "classification": "READY_FOR_REVIEW",
            "fixture_only": True,
            "task_id": task_id,
            "revision": revision,
        },
    )
    before_checkout = (source_checkout / "app.py").read_bytes()
    with pytest.raises(AuthorError) as conflict:
        Integration(service.store).create(
            repo["id"], sources, base=first["origin_commit"],
            goal="Combine explicitly conflicting fixture source",
            key="one-conflict-intent", label="fixture-integrator")
    assert conflict.value.code == "INTEGRATION_CONFLICT"

    operations = service.store.rows(
        "SELECT * FROM operations WHERE kind='integration.create'")
    assert len(operations) == 1
    operation = operations[0]
    # Adapt this set to the approved durable decision contract, but never
    # replace it with an assertion that perpetual running/combining is correct.
    assert operation["state"] in {"failed", "blocked", "awaiting_controller"}
    assert operation["phase"] != "combining"
    evidence = (
        json.loads(operation["error"]) if operation["error"] else
        json.loads(operation["result"]) if operation["result"] else {}
    )
    assert "INTEGRATION_CONFLICT" in json.dumps(evidence)
    assert (source_checkout / "app.py").read_bytes() == before_checkout
    assert service.tasks.get(first["id"])["current_revision"] == 1
    assert service.tasks.get(second["id"])["current_revision"] == 1


def test_noop_is_terminal_and_exact_key_does_not_create_more_children(setup_author, monkeypatch):
    service, checkout, repo, task, lease=setup_author
    monkeypatch.setattr(Feedback,'get',lambda self, task_id, revision=None: {'classification':'READY_FOR_REVIEW'})
    integration=Integration(service.store)
    sources=[{'task_id':task['id'],'revision':0}]
    with pytest.raises(AuthorError) as error:
        integration.create(repo['id'],sources,base=task['origin_commit'],goal='No new source',key='noop',label='fixture')
    assert error.value.code=='NO_CHANGES'
    count=service.store.one('SELECT count(*) n FROM tasks')['n']
    with pytest.raises(AuthorError) as replay:
        integration.create(repo['id'],sources,base=task['origin_commit'],goal='No new source',key='noop',label='fixture')
    assert replay.value.code=='OPERATION_FAILED'
    assert replay.value.details['previous']['code']=='NO_CHANGES'
    assert service.store.one('SELECT count(*) n FROM tasks')['n']==count


def test_accepted_child_survives_lost_parent_completion(setup_author,tmp_path,monkeypatch):
    service,checkout,repo,task,lease=setup_author
    accepted=submit(service,task,package_for(service,task,tmp_path,{'app.py':'def add(a,b): return a+b\n'}),lease,'source')
    monkeypatch.setattr(Feedback,'get',lambda self, task_id, revision=None: {'classification':'READY_FOR_REVIEW'})
    real=service.store.finish
    def interrupt(operation_id,state,**kwargs):
        if service.store.operation(operation_id)['kind']=='integration.create' and state=='completed':
            raise RuntimeError('fixture lost parent completion')
        return real(operation_id,state,**kwargs)
    monkeypatch.setattr(service.store,'finish',interrupt)
    args=dict(base=task['origin_commit'],goal='Combined candidate',key='accepted-child',label='fixture')
    sources=[{'task_id':task['id'],'revision':accepted['revision']}]
    with pytest.raises(RuntimeError,match='lost parent'):
        Integration(service.store).create(repo['id'],sources,**args)
    children=service.store.rows("SELECT * FROM operations WHERE kind='package.submit' AND state='completed'")
    before={c['id'] for c in children}
    monkeypatch.setattr(service.store,'finish',real)
    recovered=Integration(service.store).create(repo['id'],sources,**args)
    assert recovered['classification']=='INTEGRATION_NEEDS_CHECKS'
    assert {c['id'] for c in service.store.rows("SELECT * FROM operations WHERE kind='package.submit' AND state='completed'")}==before
