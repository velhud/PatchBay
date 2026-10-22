"""Actual task locks/SQLite fences, with dispatch disabled; no Linux backend claim."""
import threading

import pytest
from types import SimpleNamespace

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution import runner
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.policy import Policies
from test_core import package_for,submit


def admission_only(service,monkeypatch):
    # Exercise portable admission mechanics through the real owner, never launch
    # a supervisor or claim that this fixture qualifies Linux execution.
    monkeypatch.setattr(runner,'sys',SimpleNamespace(platform='linux'))
    return Checks(service.store)


def test_policy_update_winning_before_admission_rejects_old_profile(setup_author,monkeypatch):
    service,_,_,task,lease=setup_author
    checks=admission_only(service,monkeypatch)
    captured,release=threading.Event(),threading.Event();errors=[]
    original=checks.tasks.get
    def pause(task_id):
        value=original(task_id);captured.set();assert release.wait(5);return value
    monkeypatch.setattr(checks.tasks,'get',pause)
    def submit_check():
        try:checks.submit(task['id'],revision=0,key='racing-check',dispatch=False,token=lease['token'],epoch=lease['epoch'])
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=submit_check);thread.start()
    try:
        assert captured.wait(5)
        policy=TaskPolicy.model_validate(task['policy']);policy.allow_background_checks=False
        Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture operator',key='disable')
    finally:release.set();thread.join(5)
    assert not thread.is_alive()
    assert len(errors)==1 and isinstance(errors[0],AuthorError) and errors[0].code=='STALE_POLICY'
    assert not service.store.rows('SELECT * FROM runs WHERE task_id=?',(task['id'],))


def test_takeover_before_cancel_transaction_cannot_commit_old_intent(setup_author,monkeypatch):
    service,_,_,task,lease=setup_author
    checks=admission_only(service,monkeypatch)
    run=checks.submit(task['id'],revision=0,key='run',dispatch=False,token=lease['token'],epoch=lease['epoch'])
    checked,release=threading.Event(),threading.Event();errors=[]
    original=checks.leases.assert_owned
    def pause(*args,**kwargs):
        original(*args,**kwargs)
        if kwargs.get('conn') is None:checked.set();assert release.wait(5)
    monkeypatch.setattr(checks.leases,'assert_owned',pause)
    def cancel():
        try:checks.cancel(run['id'],key='racing-cancel',token=lease['token'],epoch=lease['epoch'])
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=cancel);thread.start()
    try:
        assert checked.wait(5)
        service.leases.claim(task['id'],label='successor',takeover=True)
    finally:release.set();thread.join(5)
    assert not thread.is_alive()
    assert len(errors)==1 and isinstance(errors[0],AuthorError) and errors[0].code=='LEASE_LOST'
    assert service.store.operation(run['id'])['cancel_requested']==0
    assert service.store.one('SELECT state FROM runs WHERE id=?',(run['id'],))['state']=='queued'


def test_committed_cancel_replays_after_takeover_without_resurrection(setup_author,monkeypatch):
    service,_,_,task,lease=setup_author
    checks=admission_only(service,monkeypatch)
    authority={'token':lease['token'],'epoch':lease['epoch']}
    run=checks.submit(task['id'],revision=0,key='run',dispatch=False,**authority)
    first=checks.cancel(run['id'],key='stop',**authority)
    service.leases.claim(task['id'],label='successor',takeover=True)
    again=checks.cancel(run['id'],key='stop',**authority)
    assert first==again and again['state']=='cancelled'
    assert service.store.operation(run['id'])['cancel_requested']==1


def test_automatic_checks_use_policy_current_before_acceptance(setup_author,tmp_path):
    service,_,_,task,lease=setup_author
    package=package_for(service,task,tmp_path,{'app.py':'def add(a,b): return a+b\n'})
    policy=TaskPolicy.model_validate(task['policy']);policy.auto_check=True;policy.profile.id='new-approved-profile'
    updated=Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture operator',key='policy')
    accepted=submit(service,task,package,lease)
    operation=service.store.one('SELECT payload FROM operations WHERE id=?',(accepted['checks_operation_id'],))
    import json
    assert json.loads(operation['payload'])['policy']==updated['policy_digest']
    run=service.store.one('SELECT profile,state FROM runs WHERE id=?',(accepted['checks_operation_id'],))
    assert json.loads(run['profile'])['id']=='new-approved-profile' and run['state']=='queued'


def test_admitted_policy_retains_old_key_and_policy_update_blocks_queued_run(setup_author,monkeypatch):
    service,_,_,task,lease=setup_author;checks=admission_only(service,monkeypatch)
    arguments=dict(task_id=task['id'],revision=0,key='admitted-under-A',dispatch=False,
                   token=lease['token'],epoch=lease['epoch'])
    admitted=checks.submit(**arguments)
    policy=TaskPolicy.model_validate(task['policy']);policy.profile.id='B'
    updated=Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture',key='B')
    assert updated['queued_checks_blocked']==1
    with pytest.raises(AuthorError) as error:
        checks.submit(**arguments)
    assert error.value.code=='IDEMPOTENCY_CONFLICT'
    import json
    assert json.loads(service.store.one('SELECT payload FROM operations WHERE id=?',(admitted['id'],))['payload'])['policy']==task['policy_digest']
    assert service.store.one('SELECT state FROM runs WHERE id=?',(admitted['id'],))['state']=='blocked'
    checks.dispatch()
    assert service.store.one('SELECT state FROM runs WHERE id=?',(admitted['id'],))['state']=='blocked'
