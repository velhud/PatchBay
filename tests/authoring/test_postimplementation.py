"""Actual Author core/transport fixtures; all client labels are synthetic."""
import xml.etree.ElementTree as ET
import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.mcp import MCP
from conftest import qualification_profile


def evidence(principal=None,role='author',profile='compat-v1'):
    from patchbay.authoring.interfaces.catalog import OUTPUT_PROFILE
    value={**qualification_profile(),'role':role,'output_profile':profile if profile!='compat-v1' else OUTPUT_PROFILE,
        'model':'synthetic','client':'controlled-in-process','ordinary_chat':True,'write_supported':True,
        'probe_description':'Synthetic fixture, no real client or account','observed_at':'fixture'}
    if principal:value['principal_id']=principal
    return value


@pytest.mark.parametrize('container',['properties','system-out','system-err'])
@pytest.mark.parametrize('outcome',['failure','error','skipped','testcase','{urn:fixture}failure'])
def test_testcase_informational_outcomes_are_incomplete(container,outcome):
    from patchbay.authoring.execution.reports import junit_counts
    root=ET.Element('testsuite',tests='1');case=ET.SubElement(root,'testcase');info=ET.SubElement(case,container);ET.SubElement(info,outcome)
    with pytest.raises(AuthorError):junit_counts(root)


def test_qualification_coexists_for_two_admissible_principals(setup_author):
    s,_,_,task,_=setup_author;access=Access(s.store)
    principals=[]
    for name,role in [('alice','author'),('bob','operator')]:
        issued=access.issue(name,role,[task['id']],key='issue-'+name)
        identity=access.authenticate('Bearer '+issued['token']);principals.append(identity)
        # Scoped operators/authors cannot perform the global operator action.
        with access.as_identity(identity),pytest.raises(AuthorError):
            access.qualify(evidence(name,role),actor='forbidden',key='denied-'+name)
        # Current local/global operator records the disposable synthetic observation.
        access.qualify(evidence(name,role),actor='fixture-operator',key='qualified-'+name)
    for identity in principals:
        with access.as_identity(identity):
            assert access.qualified(identity.role) is not None
            assert any(t['name']=='author_export_context' for t in MCP(s).tools(identity.role))


@pytest.mark.parametrize('exit_code',[0,7])
@pytest.mark.parametrize('shape',['properties','system-out','system-err','positive-utf16'])
def test_junit_parser_canonical_receipt_feedback_gate(setup_author,tmp_path,exit_code,shape):
    import sys, hashlib
    if sys.platform!='linux':pytest.skip('Linux guard; synthetic process input, no native macOS claim')
    from patchbay.authoring.execution.reports import evaluate
    from patchbay.authoring.execution.receipts import classify,record_receipt
    from patchbay.authoring.feedback.export import Feedback
    from patchbay.authoring.models import TaskPolicy,Check
    from patchbay.authoring.policy import Policies
    from patchbay.authoring.publication.git import Publisher
    from test_s10_composition import admitted_receipt
    s,_,_,task,lease=setup_author
    policy=TaskPolicy.model_validate(task['policy']);policy.publication.enabled=True
    policy.profile.checks=[Check(id='unit',argv=['synthetic-process-input'],report='junit',report_path='.author-reports/unit.xml')]
    Policies(s.store).update(task['id'],policy,expected_digest=task['policy_digest'],actor='fixture',key='hidden-outcome-policy')
    run,receipt=admitted_receipt(s,task,lease)
    root=tmp_path/'reports';(root/'.author-reports').mkdir(parents=True)
    positive=shape=='positive-utf16'
    xml='<testsuites tests="1"><testsuite tests="1"><testcase name="one"><properties><property name="x" value="&lt;failure/&gt;"/></properties><system-out>&lt;error/&gt;</system-out></testcase></testsuite></testsuites>' if positive else f'<testsuite tests="1"><testcase><{shape}><property><failure>hidden</failure></property></{shape}></testcase></testsuite>'
    raw=xml.encode('utf-16' if positive else 'utf-8');(root/'.author-reports/unit.xml').write_bytes(raw)
    check=evaluate(s.store,policy.profile,policy.profile.checks[0],root,{'state':'completed','exit_code':exit_code,'cleanup':{'state':'proven'}})
    assert check['verification']==('failed' if exit_code else 'passed' if positive else 'incomplete')
    assert check['report_sha256']==hashlib.sha256(raw).hexdigest()
    assert s.store.blobs.read(check['report_sha256'])==raw
    receipt['checks']=[check];classify(receipt,{'unit'});record_receipt(s.store,run['id'],receipt)
    feedback=Feedback(s.store).get(task['id'])
    assert feedback['evidence_provenance']['independent_acceptance'] is False
    assert (feedback['classification']=='READY_FOR_REVIEW')==(positive and exit_code==0)
    if not (positive and exit_code==0):
        with pytest.raises(AuthorError):Publisher(s.store)._candidate(task['id'],0)
    assert not s.store.rows('SELECT * FROM publications')


def identities(access,task):
    return [access.issue(name,role,[task['id']],key='issue-'+name) for name,role in [('alice','author'),('bob','operator')]]


def test_qualification_profiles_http_catalog_and_mutation_gates(setup_author):
    from fastapi.testclient import TestClient
    from patchbay.authoring.interfaces.http import create_app
    from patchbay.authoring.interfaces.catalog import OUTPUT_PROFILES
    s,_,_,task,_=setup_author;access=Access(s.store);issued=identities(access,task)
    profiles=list(OUTPUT_PROFILES)
    for index,credential in enumerate(issued):
        for profile in profiles:
            access.qualify(evidence(credential['principal_id'],credential['role'],profile),actor='synthetic-global-operator',key=profile+str(index))
    for profile in profiles:
        with TestClient(create_app(s.store.home,output_profile=profile)) as client:
            for credential in issued:
                headers={'Authorization':'Bearer '+credential['token']}
                response=client.get('/api/capabilities',headers=headers)
                assert response.status_code==200,response.text
                assert response.json()['mcp_write_qualified'] is True
                actual=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'author_export_context','arguments':{'task_id':task['id'],'key':'context-'+profile}}}).json()
                assert not actual['result']['isError'],actual
                assert 'context_id' in actual['result']['structuredContent']
                identity=access.authenticate(headers['Authorization'])
                with access.as_identity(identity):
                    access.require_mutation(identity.role,mcp=True,output_profile=profile)
                    names={t['name'] for t in MCP(s,output_profile=profile).tools(identity.role)}
                    assert 'author_export_context' in names
                assert s.store.identity['instance_id'] not in response.text
                other='bob' if credential['principal_id']=='alice' else 'alice'
                assert other not in response.text
    access.revoke(issued[0]['credential_id'],key='revoke-alice')
    with pytest.raises(AuthorError):access.authenticate('Bearer '+issued[0]['token'])
    with access.as_identity(access.authenticate('Bearer '+issued[1]['token'])):assert access.qualified('operator')


def test_qualification_replacement_restart_replay_and_legacy_migration(setup_author,tmp_path):
    from patchbay.authoring.util import canonical
    from patchbay.authoring.service import AuthorService
    from patchbay.authoring.persistence.maintenance import Maintenance
    s,_,_,task,_=setup_author;access=Access(s.store)
    old=access.qualify(evidence(),actor='synthetic',key='old-observation')
    # Reconstruct a pre-registry home without changing the old observation bytes.
    legacy=s.store.home/'client-qualification.json';legacy.write_bytes(canonical(old))
    with s.store.transaction() as conn:conn.execute("DELETE FROM metadata WHERE key LIKE 'author.client.qualif%'")
    assert access.qualified('author')==old
    assert not s.store.one('SELECT value FROM metadata WHERE key=?',(access._QUALIFICATION_REGISTRY,),required=False)
    bob=access.qualify(evidence('bob','operator'),actor='synthetic',key='bob-observation')
    assert legacy.read_bytes()==canonical(old) and access.qualified('author')==old
    fresh=access.qualify(evidence(),actor='synthetic-new',key='new-observation')
    reopened=Access(AuthorService(s.store.home).store)
    # A lost reply can be retried after restart: old completion is historical only.
    assert reopened.qualify(evidence(),actor='synthetic',key='old-observation')==old
    assert reopened.qualified('author')==fresh
    from patchbay.authoring.authority import AuthIdentity
    with reopened.as_identity(AuthIdentity('bob','operator',None)):assert reopened.qualified('operator')==bob
    archive=tmp_path/'registry.zip';Maintenance(s.store).backup(archive,key='registry-backup')
    restored=Access(Maintenance.restore(archive,tmp_path/'restored'))
    assert restored.qualified('author')==fresh
    with restored.as_identity(AuthIdentity('bob','operator',None)):assert restored.qualified('operator')==bob


def test_qualification_concurrent_writers_and_readers(setup_author):
    from concurrent.futures import ThreadPoolExecutor
    from patchbay.authoring.service import AuthorService
    from patchbay.authoring.authority import AuthIdentity
    s,_,_,_,_=setup_author
    def write(index):
        local=Access(AuthorService(s.store.home).store)
        record=local.qualify(evidence('principal-'+str(index),'author'),actor='synthetic',key='parallel-'+str(index))
        with local.as_identity(AuthIdentity('principal-'+str(index),'author',None)):
            assert local.qualified('author')==record
        return record
    with ThreadPoolExecutor(max_workers=4) as pool:records=list(pool.map(write,range(8)))
    access=Access(s.store)
    for index,record in enumerate(records):
        with access.as_identity(AuthIdentity('principal-'+str(index),'author',None)):assert access.qualified('author')==record


@pytest.mark.parametrize('fault',['expiry','source','runtime','instance','catalog','role','profile','corrupt','key','marker'])
def test_qualification_stale_binding_and_corruption_fail_closed(setup_author,monkeypatch,fault):
    from patchbay.authoring.util import canonical
    from patchbay.authoring.interfaces import catalog
    s,_,_,_,_=setup_author;access=Access(s.store)
    record=access.qualify(evidence(),actor='synthetic',key='qualification')
    key=access._qualification_key(record)
    if fault=='source':monkeypatch.setattr(catalog,'observed_source_digest',lambda:'f'*64)
    elif fault in {'runtime','instance','catalog','role','profile','expiry'}:
        if fault=='expiry':
            record['created']=1;record['expires']=2
        elif fault=='instance':record['instance_id']='different'
        elif fault=='role':record['evidence']['role']='operator'
        elif fault=='profile':
            profile=next(p for p in catalog.OUTPUT_PROFILES if p!=record['binding']['output_profile'])
            record['binding']=catalog.identity(profile);record['evidence']['output_profile']=profile
        else:record['binding'][('runtime_sha256' if fault=='runtime' else 'sha256')]='f'*64
        with s.store.transaction() as conn:
            conn.execute('DELETE FROM metadata WHERE key=?',(key,))
            conn.execute('INSERT INTO metadata(key,value) VALUES(?,?)',(access._qualification_key(record),canonical(record).decode()))
    else:
        with s.store.transaction() as conn:
            if fault=='corrupt':conn.execute('UPDATE metadata SET value=? WHERE key=?',('{broken',key))
            elif fault=='key':
                record['evidence']['principal_id']='other'
                conn.execute('UPDATE metadata SET value=? WHERE key=?',(canonical(record).decode(),key))
            else:conn.execute('UPDATE metadata SET value=? WHERE key=?',('{}',access._QUALIFICATION_REGISTRY))
    if fault in {'corrupt','key','marker'}:
        with pytest.raises(AuthorError):access.require_mutation('author',mcp=True)
    else:
        assert access.qualified('author',output_profile=catalog.OUTPUT_PROFILE) is None
        with pytest.raises(AuthorError):access.require_mutation('author',mcp=True,output_profile=catalog.OUTPUT_PROFILE)


def test_qualification_lost_reply_after_commit_does_not_reactivate_on_retry(setup_author,monkeypatch):
    from patchbay.authoring.service import AuthorService
    s,_,_,_,_=setup_author;access=Access(s.store);saved=[]
    qualify=access.qualify
    def lose(*args,**kwargs):
        saved.append(qualify(*args,**kwargs));raise ConnectionError('controlled loss after committed qualification')
    monkeypatch.setattr(access,'qualify',lose)
    with pytest.raises(ConnectionError):access.qualify(evidence('alice'),actor='synthetic',key='lost-reply')
    newer=qualify(evidence('alice'),actor='newer-synthetic',key='newer-reply')
    bob=qualify(evidence('bob','operator'),actor='synthetic',key='independent-bob')
    reopened=Access(AuthorService(s.store.home).store)
    assert reopened.qualify(evidence('alice'),actor='synthetic',key='lost-reply')==saved[0]
    from patchbay.authoring.authority import AuthIdentity
    with reopened.as_identity(AuthIdentity('alice','author',None)):assert reopened.qualified('author')==newer
    with reopened.as_identity(AuthIdentity('bob','operator',None)):assert reopened.qualified('operator')==bob


def test_qualification_invalid_legacy_never_silently_migrates(setup_author):
    from patchbay.authoring.util import canonical
    s,_,_,_,_=setup_author;access=Access(s.store)
    record=access.qualify(evidence(),actor='synthetic',key='original')
    with s.store.transaction() as conn:conn.execute("DELETE FROM metadata WHERE key LIKE 'author.client.qualif%'")
    record['evidence']['ordinary_chat']=False
    (s.store.home/'client-qualification.json').write_bytes(canonical(record))
    with pytest.raises(AuthorError):access.qualified('author')
    with pytest.raises(AuthorError):access.qualify(evidence('other'),actor='synthetic',key='new')
    assert not s.store.one('SELECT value FROM metadata WHERE key=?',(access._QUALIFICATION_REGISTRY,),required=False)
