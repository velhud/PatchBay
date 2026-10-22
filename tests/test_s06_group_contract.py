"""Exact generation/check binding; semantic completion stays controller owned."""
import pytest
from patchbay.hub.groups_v2 import validate_combined_verification, validate_source_contract, derive_completion_contract
from patchbay.hub.runtime_v2 import WORK_GROUP_ENTITY
from test_hub_runtime_v2 import make_runtime,enroll_online,context


def contract():
    return dict(input_generation='upstream-generation-A',lane_relationships='API lane precedes caller lane; docs are independent.',
        interface_owner='API author',join_condition='Integrate both approved source targets, then test the combined checkout.',
        required_combined_checks=['integration'])


@pytest.mark.parametrize('change',['tree','generation','failure','missing'])
def test_combined_check_must_match_declared_generation(change):
    evidence=dict(input_generation='upstream-generation-A',delivery_generation='c'*64,combined_tree='a'*40,
        checks=[dict(name='integration',tree='a'*40,outcome='passed',evidence_ref='retained-log')])
    assert validate_combined_verification(contract(),evidence)['satisfied']
    if change=='tree': evidence['checks'][0]['tree']='b'*40
    if change=='generation': evidence['input_generation']='stale-generation'
    if change=='failure': evidence['checks'][0]['outcome']='failed'
    if change=='missing': evidence['checks']=[]
    assert not validate_combined_verification(contract(),evidence)['satisfied']
    assert not derive_completion_contract({'source_contract':contract(),'status':'open'},[],[])['combined_verification']['satisfied']


def test_real_group_close_persists_controller_evidence_and_survives_restart(tmp_path):
    from patchbay.hub.runtime_v2 import HubRuntimeV2
    runtime,store,path=make_runtime(tmp_path);enroll_online(runtime,machine_id='machine')
    caller=context('owner');workspace=runtime.workspace_list()['result']['workspaces'][0]['workspace_ref']
    created=runtime.create_work_group(title='Joined task',goal='Implement then check the combined result.',workspace_ref=workspace,
        source_contract=contract(),idempotency_key='group-join',context=caller)
    gid=created['result']['work_group']['work_group_id']
    refused=runtime.close_work_group(work_group_id=gid,outcome='complete',summary='No evidence yet.',worker_dispositions={},idempotency_key='close-missing',context=caller)
    assert refused['status']=='blocked'
    evidence=dict(input_generation='upstream-generation-A',delivery_generation='c'*64,combined_tree='a'*40,
        checks=[dict(name='integration',tree='a'*40,outcome='passed',evidence_ref='retained-controller-check-log')])
    evidence['delivery_generation']=created['result']['completion_contract']['delivery_generation']
    closed=runtime.close_work_group(work_group_id=gid,outcome='complete',summary='Combined source checked.',worker_dispositions={},
        combined_verification=evidence,idempotency_key='close-joined',context=caller)
    assert closed['status']=='ok'
    retained=store.get_entity(WORK_GROUP_ENTITY,gid)['record']
    assert retained['combined_verification']['evidence']==evidence
    assert retained['combined_verification']['canonical_execution_receipt'] is False
    reopened=HubRuntimeV2(runtime.config,store)
    assert reopened.store.get_entity(WORK_GROUP_ENTITY,gid)['record']['combined_verification']['satisfied']


def test_new_worker_content_invalidates_previous_combined_evidence(tmp_path):
    from test_hub_runtime_v2 import heartbeat_workers,FLEET_WORKER_ENTITY
    runtime,store,path=make_runtime(tmp_path);enrolled=enroll_online(runtime,machine_id='machine')
    caller=context('owner');workspace=runtime.workspace_list()['result']['workspaces'][0]['workspace_ref']
    created=runtime.create_work_group(title='Joined task',goal='Check joined source.',workspace_ref=workspace,
        source_contract=contract(),idempotency_key='group-generation',context=caller)
    gid=created['result']['work_group']['work_group_id']
    old_generation=created['result']['completion_contract']['delivery_generation']
    heartbeat_workers(runtime,enrolled,2,[dict(edge_worker_id='worker',name='Implementation',work_group_id=gid,lane_id='main',
        worker_state='available',turn_state='completed',liveness='terminal',integration_state='no_changes',content_revision='new-source')])
    ref=store.list_entities(FLEET_WORKER_ENTITY)[0]['entity_id']
    evidence=dict(input_generation='upstream-generation-A',combined_tree='a'*40,delivery_generation=old_generation,
        checks=[dict(name='integration',tree='a'*40,outcome='passed',evidence_ref='old-check-log')])
    refused=runtime.close_work_group(work_group_id=gid,outcome='complete',summary='Stale checks.',worker_dispositions={ref:'no_changes'},
        combined_verification=evidence,idempotency_key='stale-generation',context=caller)
    assert refused['status']=='blocked'
    assert store.get_entity(WORK_GROUP_ENTITY,gid)['record']['status']=='open'


def test_late_generation_change_is_atomically_refused_without_phantom_close(tmp_path,monkeypatch):
    from test_hub_runtime_v2 import heartbeat_workers,FLEET_WORKER_ENTITY
    runtime,store,path=make_runtime(tmp_path);enrolled=enroll_online(runtime,machine_id='machine');caller=context('owner')
    workspace=runtime.workspace_list()['result']['workspaces'][0]['workspace_ref']
    created=runtime.create_work_group(title='Joined task',goal='Check source.',workspace_ref=workspace,source_contract=contract(),idempotency_key='late-group',context=caller)
    gid=created['result']['work_group']['work_group_id'];generation=created['result']['completion_contract']['delivery_generation']
    real=runtime._finish_group_close
    def drift(**kwargs):
        heartbeat_workers(runtime,enrolled,2,[dict(edge_worker_id='worker',name='Late source',work_group_id=gid,lane_id='main',
            worker_state='available',turn_state='completed',liveness='terminal',integration_state='no_changes',content_revision='late-source')])
        return real(**kwargs)
    monkeypatch.setattr(runtime,'_finish_group_close',drift)
    evidence=dict(input_generation='upstream-generation-A',delivery_generation=generation,combined_tree='a'*40,
        checks=[dict(name='integration',tree='a'*40,outcome='passed',evidence_ref='check-before-drift')])
    refused=runtime.close_work_group(work_group_id=gid,outcome='complete',summary='Old checks.',worker_dispositions={},
        combined_verification=evidence,idempotency_key='late-close',context=caller)
    assert refused['status']=='blocked' and refused['result']['reason']=='combined_source_generation_changed'
    assert refused['operation']['state']=='blocked'
    assert store.get_entity(WORK_GROUP_ENTITY,gid)['record']['status']=='open'
