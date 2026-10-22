"""Composed actual Git/SQLite owners with inert execution and explicit negatives.

Synthetic process receipts are persistence inputs, never native/client evidence.
"""
import json
import sys
from types import SimpleNamespace
import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.execution.receipts import classify, record_receipt
from patchbay.authoring.execution.reports import evaluate
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import Check,TaskPolicy
from patchbay.authoring.policy import Policies
from patchbay.authoring.publication.git import Publisher
from patchbay.authoring.service import AuthorService
from test_fault_boundaries import _synthetic_receipt
from test_core import package_for,submit

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux admission guard; no native macOS qualification")


def admitted_receipt(service,task,lease,key='joined'):
    op=Checks(service.store).submit(task['id'],revision=0,key=key,dispatch=False,token=lease['token'],epoch=lease['epoch'])
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='running',cleanup='proven' WHERE id=?",(op['id'],))
        conn.execute("UPDATE operations SET state='running',phase='verifying' WHERE id=?",(op['id'],))
    run=service.store.one('SELECT * FROM runs WHERE id=?',(op['id'],))
    current=service.tasks.get(task['id'])
    return run,_synthetic_receipt(service,current,run,passed=True)


@pytest.mark.parametrize('contradictory,observed',[(False,True),(True,True),(False,False)])
def test_report_to_receipt_feedback_and_publication_gate(setup_author,tmp_path,contradictory,observed):
    service,_,_,task,lease=setup_author
    policy=TaskPolicy.model_validate(task['policy']);policy.publication.enabled=True
    policy.profile.checks=[Check(id='unit',argv=['synthetic-producer'],report='junit',report_path='.author-reports/unit.xml')]
    Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture',key='report-policy')
    run,receipt=admitted_receipt(service,task,lease)
    workspace=tmp_path/'report-workspace';(workspace/'.author-reports').mkdir(parents=True)
    raw=b'<testsuite tests="2"><testcase name="only-one"/></testsuite>' if contradictory else b'<testsuite tests="1"><testcase name="one"/></testsuite>'
    (workspace/'.author-reports/unit.xml').write_bytes(raw)
    receipt['checks']=[evaluate(service.store,policy.profile,policy.profile.checks[0],workspace,{'state':'completed','exit_code':0,'cleanup':{'state':'proven'}})]
    if not observed:receipt['source_observation'].update(state='not_started',boundaries=[])
    classify(receipt,{'unit'});record_receipt(service.store,run['id'],receipt)
    feedback=Feedback(service.store).get(task['id'])
    assert feedback['receipt']['checks'][0]['report_sha256']
    assert feedback['evidence_provenance']['independent_acceptance'] is False
    if not contradictory and observed:
        assert feedback['classification']=='READY_FOR_REVIEW'
        assert Publisher(service.store)._candidate(task['id'],0)[3]['receipt_sha']==feedback['receipt_sha']
    else:
        assert feedback['classification']!='READY_FOR_REVIEW'
        with pytest.raises(AuthorError) as error:Publisher(service.store)._candidate(task['id'],0)
        assert error.value.code=='CHECKS_INCOMPLETE'
    assert not service.store.rows('SELECT * FROM publications')


@pytest.mark.parametrize('advance_policy',[False,True])
def test_cancel_after_receipt_blob_preparation_cannot_persist_green(setup_author,monkeypatch,advance_policy):
    service,_,_,task,lease=setup_author
    run,receipt=admitted_receipt(service,task,lease)
    put=service.store.blobs.put;cancelled=[]
    def after_blob(data):
        sha=put(data)
        if not cancelled:
            cancelled.append(Checks(service.store).cancel(run['id'],token=lease['token'],epoch=lease['epoch'],key='cancel-after-blob'))
            if advance_policy:
                current=service.tasks.get(task['id']);changed=TaskPolicy.model_validate(current['policy']);changed.profile.id='policy-after-cancel'
                Policies(service.store).update(task['id'],changed,expected_digest=current['policy_digest'],actor='fixture',key='policy-after-cancel')
        return sha
    monkeypatch.setattr(service.store.blobs,'put',after_blob)
    record_receipt(service.store,run['id'],receipt)
    feedback=Feedback(service.store).get(task['id'])
    assert len(cancelled)==1 and feedback['receipt']['classification']=='CANCELLED'
    assert feedback['classification']!='READY_FOR_REVIEW'
    if advance_policy:assert feedback['evidence_current'] is False
    assert feedback['verification_receipt']['outcome']=='incomplete'
    assert service.store.operation(run['id'])['cancel_requested']==1
    successor=service.leases.claim(task['id'],takeover=True,key='current-owner')
    replay=Checks(service.store).cancel(run['id'],token=lease['token'],epoch=lease['epoch'],key='cancel-after-blob')
    assert replay==cancelled[0]
    fresh=Checks(service.store).submit(task['id'],revision=0,key='explicit-successor-check',dispatch=False,token=successor['token'],epoch=successor['epoch'])
    assert fresh['state']=='queued' and fresh['id']!=run['id']


def test_late_old_policy_receipt_is_historical_after_current_policy_wins(setup_author,monkeypatch):
    service,_,_,task,lease=setup_author
    policy=TaskPolicy.model_validate(task['policy']);policy.publication.enabled=True
    Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture',key='publication-gate')
    run,receipt=admitted_receipt(service,task,lease)
    old_digest=receipt['policy_digest'];put=service.store.blobs.put;updated=[]
    def after_blob(data):
        sha=put(data)
        if not updated:
            current=service.tasks.get(task['id']);changed=TaskPolicy.model_validate(current['policy']);changed.profile.id='current-policy-B'
            updated.append(Policies(service.store).update(task['id'],changed,expected_digest=current['policy_digest'],actor='fixture',key='late-policy'))
        return sha
    monkeypatch.setattr(service.store.blobs,'put',after_blob)
    record_receipt(service.store,run['id'],receipt)
    feedback=Feedback(service.store).get(task['id'])
    assert feedback['receipt']['policy_digest']==old_digest
    assert feedback['historical_classification']=='READY_FOR_REVIEW' and feedback['evidence_current'] is False
    assert feedback['classification']!='READY_FOR_REVIEW'
    assert service.tasks.get(task['id'])['policy_digest']==updated[0]['policy_digest']!=old_digest
    with pytest.raises(AuthorError) as error:Publisher(service.store)._candidate(task['id'],0)
    assert error.value.code=='CHECKS_INCOMPLETE'


def test_lost_accepted_revision_autocheck_then_lease_takeover_and_progress(setup_author,tmp_path,monkeypatch):
    service,_,_,task,old=setup_author
    policy=TaskPolicy.model_validate(task['policy']);policy.auto_check=True
    Policies(service.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture',key='auto-check-policy')
    package=package_for(service,task,tmp_path,{'accepted.txt':'durable once\n'})
    original=service.materializer.submit;accepted=[]
    def lost(*args,**kwargs):
        accepted.append(original(*args,**kwargs));raise ConnectionError('injected loss after commit')
    monkeypatch.setattr(service.materializer,'submit',lost)
    with pytest.raises(ConnectionError):submit(service,task,package,old,'original-effect')
    first=accepted[0];assert first['revision']==1 and first['checks_operation_id']
    with service.store.transaction() as conn:conn.execute('UPDATE leases SET expires=0 WHERE task_id=?',(task['id'],))
    reopened=AuthorService(service.store.home)
    assert reopened.recovery.get(task['id'])['candidate']['commit']==first['commit']
    with pytest.raises(AuthorError) as expired:submit(reopened,task,package,old,'original-effect')
    assert expired.value.code=='LEASE_LOST'
    new=reopened.leases.claim(task['id'],takeover=True,key='explicit-controller-takeover')
    replay=submit(reopened,task,package,new,'original-effect')
    assert replay==first
    assert len(reopened.store.rows('SELECT * FROM runs'))==1
    assert len(reopened.store.rows('SELECT * FROM revisions WHERE task_id=?',(task['id'],)))==2
    following=package_for(reopened,task,tmp_path,{'next.txt':'authorized new source\n'})
    next_result=submit(reopened,task,following,new,'explicit-next-effect')
    assert next_result['revision']==2 and next_result['checks_operation_id']!=first['checks_operation_id']
    assert len(reopened.store.rows('SELECT * FROM runs'))==2
    assert all(r['state']=='queued' for r in reopened.store.rows('SELECT state FROM runs'))


def test_admission_refreshes_capacity_selection_and_corruption_evidence(tmp_path,monkeypatch):
    from patchbay.authoring.execution import admission
    from patchbay.authoring.persistence.blobs import Blobs
    from test_resource_requirements import _Store,_profile,_capacity
    profile=_profile();store=_Store(profile);store.home=tmp_path;store.blobs=Blobs(tmp_path/'blobs')
    raw=json.dumps({'resource_usage':{'samples':1,'cpu_cores_peak':.6}}).encode()
    sha=store.blobs.put(raw);store._receipts={sha:raw}
    monkeypatch.setattr(admission,'host_capacity',lambda:_capacity(cpu=2))
    monkeypatch.setattr(admission.shutil,'disk_usage',lambda _:SimpleNamespace(free=10**12))
    fits,first=admission.can_admit(store,profile,[]);assert fits and first['reservation']['cpu_cores']==.75
    monkeypatch.setattr(admission,'host_capacity',lambda:_capacity(cpu=.25))
    assert admission.can_admit(store,profile,[])[0] is False
    monkeypatch.setattr(admission,'host_capacity',lambda:_capacity(cpu=2))
    newer=json.dumps({'resource_usage':{'samples':1,'cpu_cores_peak':1.2}}).encode()
    new_sha=store.blobs.put(newer);store._receipts={new_sha:newer}
    assert admission.can_admit(store,profile,[])[1]['reservation']['cpu_cores']==1.5
    store.blobs.path(new_sha).write_bytes(b'corrupted same identity')
    with pytest.raises(AuthorError) as corrupt:admission.can_admit(store,profile,[])
    assert corrupt.value.code=='CORRUPT_STATE'
