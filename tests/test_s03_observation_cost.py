"""Actual Hub SQLite/projection reads, counted beneath concurrent observers."""
import asyncio

import pytest

from patchbay.hub.app_v2 import HubAppV2, HubWorkerProjectionPortV2, WORKER_PROJECTION_ENTITY
from test_hub_app_v2 import FakeEdgeDelivery


@pytest.mark.asyncio
@pytest.mark.parametrize('clients',[1,5,10])
async def test_unrelated_churn_does_not_multiply_group_snapshot_builds(tmp_path,monkeypatch,clients):
    app=HubAppV2(tmp_path/'hub.sqlite3',edge_delivery=FakeEdgeDelivery())
    store=app.store
    store.put_entity(WORKER_PROJECTION_ENTITY,'fixture-worker',{'fleet_worker_ref':'fixture-worker',
        'work_group_id':'fixture-group','name':'Quiet','turn_state':'working','liveness':'quiet'},expected_revision=0)
    port=HubWorkerProjectionPortV2(app.runtime,minimum_poll_seconds=0,max_wait_seconds=.65)
    filters={'work_group_id':'fixture-group'}
    baseline=port.query(view='status',filters=filters,route={})['projection_revision']
    original=port._projection_snapshot
    builds=[]
    def counted(**kwargs):
        builds.append(True)
        return original(**kwargs)
    monkeypatch.setattr(port,'_projection_snapshot',counted)
    async def churn():
        for n in range(12):
            await asyncio.sleep(.04)
            store.put_entity('fixture.unrelated','other-'+str(n),{'unrelated':True},expected_revision=0)
    try:
        results=await asyncio.gather(*(port.wait(filters=filters,route={},since_revision=baseline,
            timeout_seconds=.65) for _ in range(clients)),churn())
        assert all(not result['changed'] for result in results[:-1])
        assert len(builds)<=2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_relevant_change_reaches_all_observers_and_cancel_only_removes_one(tmp_path,monkeypatch):
    app=HubAppV2(tmp_path/'hub.sqlite3',edge_delivery=FakeEdgeDelivery())
    store=app.store
    initial={'fleet_worker_ref':'fixture-worker','work_group_id':'fixture-group','name':'Quiet',
        'turn_state':'working','liveness':'quiet'}
    store.put_entity(WORKER_PROJECTION_ENTITY,'fixture-worker',initial,expected_revision=0)
    port=HubWorkerProjectionPortV2(app.runtime,minimum_poll_seconds=0,max_wait_seconds=1)
    filters={'work_group_id':'fixture-group'}
    baseline=port.query(view='status',filters=filters,route={})['projection_revision']
    pending=[asyncio.create_task(port.wait(filters=filters,route={},since_revision=baseline,timeout_seconds=1)) for _ in range(10)]
    await asyncio.sleep(.03)
    pending[0].cancel()
    await asyncio.gather(pending[0],return_exceptions=True)
    store.put_entity(WORKER_PROJECTION_ENTITY,'fixture-worker',{**initial,'turn_state':'failed','liveness':'terminal'},expected_revision=1)
    try:
        results=await asyncio.gather(*pending[1:])
        assert all(result['changed'] and result['workers'][0]['turn_state']=='failed' for result in results)
        assert store.get_entity(WORKER_PROJECTION_ENTITY,'fixture-worker')['revision']==2
    finally:
        store.close()


def test_scope_cache_rechecks_after_build_and_expires_without_background_observers():
    from patchbay.observation import ScopedObservationCache
    now=[0.0]
    revision=[1]
    builds=[]
    cache=ScopedObservationCache(max_entries=2,ttl=1,clock=lambda:now[0])
    def build():
        builds.append(revision[0])
        if len(builds)==1:revision[0]=2
        return revision[0]
    assert cache.get('scope',lambda:revision[0],build)==2
    assert len(builds)==2  # Mutation in the read/build window was rechecked.
    for _ in range(10):assert cache.get('scope',lambda:revision[0],build)==2
    assert len(builds)==2
    cache.get('other',lambda:1)
    cache.get('third',lambda:1)
    assert len(cache._entries)==2
    now[0]=2
    cache.get('fresh',lambda:3)
    assert list(cache._entries)==['fresh']


@pytest.mark.asyncio
async def test_future_wait_revision_returns_full_resynchronization(tmp_path):
    app=HubAppV2(tmp_path/'hub.sqlite3',edge_delivery=FakeEdgeDelivery())
    try:
        port=HubWorkerProjectionPortV2(app.runtime,minimum_poll_seconds=0,max_wait_seconds=1)
        result=await port.wait(filters={},route={},since_revision=1000,timeout_seconds=1)
        assert result['resync_required'] and result['changed'] and result['workers']==[]
        assert result['waited_seconds']==0
    finally:
        app.store.close()
