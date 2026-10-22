"""Authenticated actual Author HTTP ingress; no scheduler or check execution."""
import asyncio
import json
import threading
import time

import httpx
from fastapi.testclient import TestClient

from patchbay.authoring.interfaces.http import create_app
from patchbay.authoring.interfaces.ingress import IngressAdmission
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.snapshots import Snapshots
from patchbay.authoring.tasks import Tasks
from test_controller_contracts import qualified
from test_fault_boundaries import _seed_check_run


def gate_for(app):
    gate = app.middleware_stack
    while not isinstance(gate, IngressAdmission):
        gate = gate.app
    return gate


def envelope(name, arguments):
    return dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=arguments))


def test_recovery_survives_routine_read_rate_exhaustion_on_real_mcp(setup_author):
    service, _, _, task, _ = setup_author
    issued = Access(service.store).issue('observation-reader','reader',[task['id']],key='reader')
    app = create_app(service.store.home)
    headers = {'Authorization':'Bearer '+issued['token']}
    with TestClient(app) as client:
        gate = gate_for(app)
        gate.clock = lambda: 100.0
        for _ in range(20):
            assert client.post('/mcp',headers=headers,json=envelope('author_get_task',{'task_id':task['id']})).status_code == 200
        ordinary = client.post('/mcp',headers=headers,json=envelope('author_get_task',{'task_id':task['id']}))
        assert ordinary.status_code == 429
        recovery = client.post('/mcp',headers=headers,json=envelope('author_recover_task',{'task_id':task['id']}))
        assert recovery.status_code == 200
        assert not recovery.json()['result']['isError']
        assert gate._active == 0 and not gate._waiters


def test_actual_mcp_cancel_is_admitted_while_same_principal_writes_are_held(setup_author, monkeypatch):
    service, _, _, task, lease = setup_author
    qualified(service)
    run, _ = _seed_check_run(service,task,key='queued-cancellation-only')
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='queued' WHERE id=?",(run['id'],))
        conn.execute("UPDATE operations SET state='queued' WHERE id=?",(run['id'],))
    original = Snapshots.export
    release, lock = threading.Event(), threading.Lock()
    entered = []
    def slow_export(self, *args, **kwargs):
        with lock: entered.append(True)
        assert release.wait(5)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Snapshots,'export',slow_export)
    tokens = json.loads((service.store.home/'tokens.json').read_text())
    headers = {'Authorization':'Bearer '+tokens['author']}
    app = create_app(service.store.home,output_profile='structured-summary-v2')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            writes = [asyncio.create_task(client.post('/mcp',headers=headers,json=envelope(
                'author_export_context',{'task_id':task['id'],'key':'held-'+str(n)}))) for n in range(2)]
            try:
                for _ in range(300):
                    if len(entered)==2: break
                    await asyncio.sleep(.005)
                assert len(entered)==2
                result = await client.post('/mcp',headers=headers,json=envelope('author_cancel_operation',{
                    'operation_id':run['id'],'token':lease['token'],'epoch':lease['epoch'],'key':'cancel'}))
                assert result.status_code == 200
                assert not result.json()['result']['isError']
                assert service.store.operation(run['id'])['state'] == 'cancelled'
            finally:
                release.set()
                await asyncio.gather(*writes)
            assert gate_for(app)._active == 0
    asyncio.run(scenario())


def test_urgent_cancel_keeps_a_global_slot_when_seven_readers_are_held(setup_author, monkeypatch):
    service, _, _, task, lease = setup_author
    qualified(service)
    run, _ = _seed_check_run(service,task,key='queued-for-saturated-read-cancel')
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='queued' WHERE id=?",(run['id'],))
        conn.execute("UPDATE operations SET state='queued' WHERE id=?",(run['id'],))
    readers = [Access(service.store).issue('reader-'+str(n),'reader',[task['id']],key='reader-'+str(n)) for n in range(7)]
    original = Tasks.get
    entered, lock, release = [], threading.Lock(), threading.Event()
    def hold(self, *args, **kwargs):
        with lock: entered.append(True)
        assert release.wait(10)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Tasks,'get',hold)
    tokens = json.loads((service.store.home/'tokens.json').read_text())
    app = create_app(service.store.home,output_profile='structured-summary-v2')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            reads = [asyncio.create_task(client.post('/mcp',headers={'Authorization':'Bearer '+r['token']},
                json=envelope('author_get_task',{'task_id':task['id']}))) for r in readers]
            try:
                for _ in range(300):
                    if len(entered)==7:break
                    await asyncio.sleep(.005)
                assert len(entered)==7
                start = time.monotonic()
                response = await asyncio.wait_for(client.post('/mcp',headers={'Authorization':'Bearer '+tokens['author']},
                    json=envelope('author_cancel_operation',{'operation_id':run['id'],'token':lease['token'],
                        'epoch':lease['epoch'],'key':'saturation-cancel'})),2)
                assert response.status_code == 200 and not response.json()['result']['isError']
                assert time.monotonic()-start < 2
            finally:
                release.set()
                await asyncio.gather(*reads)
            assert service.store.operation(run['id'])['state']=='cancelled'
            assert gate_for(app)._active==0 and not gate_for(app)._waiters
    asyncio.run(scenario())


def test_priority_is_validated_not_caller_asserted_and_prefix_is_lossless(setup_author):
    service, _, repo, task, lease = setup_author
    other=service.tasks.create(repo['id'],'Outside reader scope',key='other-reader-scope')
    issued = Access(service.store).issue('priority-reader','reader',[task['id']],key='priority-reader')
    app = create_app(service.store.home)
    headers={'Authorization':'Bearer '+issued['token']}
    kinds=[]
    before=service.store.one('SELECT COUNT(*) AS n FROM operations')['n']
    with TestClient(app) as client:
        gate=gate_for(app)
        gate.rate_per_minute=None
        original=gate._acquire
        async def record(kind, principal):
            kinds.append(kind)
            return await original(kind,principal)
        gate._acquire=record
        forged=envelope('author_cancel_operation',{'operation_id':lease.get('operation_id','op_'+'0'*32),
            'token':lease['token'],'epoch':lease['epoch'],'key':'forged'})
        forged['priority']='control'
        client.post('/mcp',headers=headers,json=forged)
        assert kinds[-1]=='write'
        denied=client.post('/mcp',headers=headers,json=envelope('author_recover_task',{'task_id':other['id']}))
        assert kinds[-1]=='write' and denied.json()['result']['structuredContent']['code']=='FORBIDDEN'
        assert client.post('/api/control/pause',headers=headers,json={'actor':'fixture','reason':'fixture',
            'key':'denied-control','priority':'control'}).status_code==403
        assert kinds[-1]=='write'
        invalid=envelope('author_recover_task',{'task_id':task['id']});invalid['id']=None
        client.post('/mcp',headers=headers,json=invalid)
        assert kinds[-1]=='write'
        client.post('/mcp',headers={**headers,'MCP-Protocol-Version':'unsupported'},
            json=envelope('author_recover_task',{'task_id':task['id']}))
        assert kinds[-1]=='write'
        client.post('/mcp',headers=headers,content=b'{broken')
        assert kinds[-1]=='write'
        oversized=envelope('author_recover_task',{'task_id':task['id']});oversized['padding']='x'*70000
        response=client.post('/mcp',headers=headers,json=oversized)
        assert response.status_code==200 and not response.json()['result']['isError'] and kinds[-1]=='write'
        oversized['padding']='x'*(2*1024**2)
        assert client.post('/mcp',headers=headers,json=oversized).status_code==413
        assert gate._active==0 and not gate._waiters and gate._classifying==0
    assert service.store.one('SELECT COUNT(*) AS n FROM operations')['n']==before
    async def fragmented():
        raw=json.dumps(envelope('author_get_task',{'task_id':task['id']})).encode()
        async def chunks():
            yield raw[:11]
            await asyncio.sleep(.001)
            yield raw[11:]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service.store.home)),base_url='http://test') as client:
            response=await client.post('/mcp',headers=headers,content=chunks())
            assert response.status_code==200 and not response.json()['result']['isError']
    asyncio.run(fragmented())


def test_urgent_rate_and_pending_capacity_are_reserved_but_bounded():
    async def scenario():
        async def unused(*args): raise AssertionError('No downstream effects in limiter fixture')
        gate=IngressAdmission(unused,clock=lambda:100.0)
        for principal in ('fixture-a','fixture-b'):
            for _ in range(5):
                assert (await gate._acquire('control',principal))[0]
                await gate._release('control',principal)
            assert not (await gate._acquire('control',principal))[0]
        assert not (await gate._acquire('control','fixture-c'))[0]
        assert (await gate._acquire('read','fixture-c'))[0]
        await gate._release('read','fixture-c')
        gate=IngressAdmission(unused,rate_per_minute=None,queue_timeout=1)
        assert (await gate._acquire('write','fixture'))[0]
        assert (await gate._acquire('write','fixture'))[0]
        pending=[asyncio.create_task(gate._acquire('write','fixture')) for _ in range(4)]
        try:
            for _ in range(50):
                if len(gate._waiters)==4:break
                await asyncio.sleep(.001)
            assert len(gate._waiters)==4
            assert (await gate._acquire('control','fixture'))[0]
            await gate._release('control','fixture')
        finally:
            for task in pending:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            await gate._release('write','fixture');await gate._release('write','fixture')
        assert gate._active==gate._active_controls==gate._active_writes==0
        assert not gate._waiters and not gate._waiting_by_principal
    asyncio.run(scenario())


def test_cancelled_classification_retains_its_bounded_owner_until_thread_exits():
    from test_ingress import _request
    async def scenario():
        entered,release=threading.Event(),threading.Event()
        async def unused(*args):raise AssertionError('Cancelled observer must not dispatch')
        def classify(scope,raw):
            entered.set()
            assert release.wait(2)
            return 'read'
        gate=IngressAdmission(unused,semantic_kind=classify,rate_per_minute=None)
        observer=asyncio.create_task(_request(gate))
        try:
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.001)
            assert entered.is_set()
            observer.cancel()
            await asyncio.gather(observer,return_exceptions=True)
            assert gate._classifying==1 and len(gate._classification_tasks)==1 and gate._active==0
        finally:
            release.set()
            await asyncio.gather(observer,return_exceptions=True)
        for _ in range(100):
            if gate._classifying==0 and not gate._classification_tasks:break
            await asyncio.sleep(.001)
        assert gate._classifying==0 and not gate._classification_tasks and not gate._waiters
    asyncio.run(scenario())
