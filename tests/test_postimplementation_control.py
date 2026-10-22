"""Shipped Manager protocol/runtime with inert execution, owned I/O barriers."""
import asyncio
import hashlib
import threading
import pytest
from patchbay.jobs.manager import JobManager, JobState
from patchbay.tools.handler import ToolHandler
from patchbay.protocol.mcp import MCPProtocol
from patchbay.protocol.context import RequestContext
from test_worker_projection_v2 import make_config, ProjectionExecutor, add_worker


def manager_route(tmp_path, monkeypatch, **artifacts):
    monkeypatch.setenv('PATCHBAY_HOME',str(tmp_path/'runtime'))
    config=make_config(tmp_path);config['artifacts']={'root':str(tmp_path/'inbox'),**artifacts}
    config['workers']['status_max_wait_seconds']=1
    manager=JobManager(config);executor=ProjectionExecutor();handler=ToolHandler(config,manager,executor)
    return manager,executor,handler,MCPProtocol(config,handler)


async def call(protocol,name,args,context=None):
    result=await protocol.handle_message({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},context=context)
    assert 'error' not in result,result
    return result['result']['structuredContent']


@pytest.mark.asyncio
async def test_import_actual_protocol_keeps_control_responsive(tmp_path,monkeypatch):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    entered,release,control_done=threading.Event(),threading.Event(),threading.Event();observed=[]
    def slow(url,path):
        entered.set();assert release.wait(3)
        path.write_bytes(b'exact');return 5,hashlib.sha256(b'exact').hexdigest()
    monkeypatch.setattr(handler.artifact_store,'_download',slow)
    loop=asyncio.get_running_loop()
    async def status():
        await call(protocol,'codex_worker_status',{'scope':'history'})
        control_done.set();release.set()
    def controller():
        assert entered.wait(3)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(status()))
        observed.append(control_done.wait(.5));release.set()
    thread=threading.Thread(target=controller);thread.start()
    try:
        result=await call(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/input','file_name':'input.txt'}})
        assert result['sha256']==hashlib.sha256(b'exact').hexdigest()
        await asyncio.to_thread(thread.join,3)
        assert observed==[True], 'Protocol loop was blocked while synchronous importer held the barrier'
    finally:release.set();await asyncio.to_thread(thread.join,3)


@pytest.mark.asyncio
async def test_default_wait_does_not_repeat_old_completion_in_mixed_team(tmp_path,monkeypatch):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    add_worker(manager,executor,worker_id='done',name='Done',state=JobState.COMPLETED)
    live=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    context=RequestContext(client_ref='manager-a',work_run_ref='run-a')
    for job in manager.jobs.values():job.options['_worker_work_run_ref']='run-a'
    args={}  # Actual public defaults, including current scope and active_only=false.
    first=await call(protocol,'codex_worker_wait',args,context);assert first['wait_outcome']=='actionable'
    for _ in range(2):
        second=await call(protocol,'codex_worker_wait',args,context)
        assert second['wait_outcome']=='timeout' and second['waited_seconds']>=1
    assert manager.get_job(live).state==JobState.RUNNING


async def rpc_error(protocol, name, args, context=None):
    result = await protocol.handle_message({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}},context=context)
    assert 'error' in result, result
    return result['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['download','extraction','metadata'])
async def test_cancelled_import_retains_capacity_and_owned_stage_until_writer_exits(tmp_path,monkeypatch,boundary):
    import io, zipfile
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch,max_concurrent_imports=1)
    live=add_worker(manager,executor,worker_id='independent',name='Independent',state=JobState.RUNNING,heartbeat_age=0,live=True)
    store=handler.artifact_store;entered=threading.Event();release=threading.Event()
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:archive.writestr('exact.txt',b'controlled bytes')
    raw=data.getvalue()
    def barrier():
        entered.set();assert release.wait(5)
    def download(url,path):
        path.write_bytes(raw)
        if boundary=='download':barrier()
        return len(raw),hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(store,'_download',download)
    if boundary=='extraction':
        original=zipfile.ZipExtFile.read
        def read(self,*a,**kw):
            result=original(self,*a,**kw);barrier();return result
        monkeypatch.setattr(zipfile.ZipExtFile,'read',read)
    if boundary=='metadata':
        original=store._write_json
        def write(path,value):
            original(path,value);barrier()
        monkeypatch.setattr(store,'_write_json',write)
    args={'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/archive','file_name':'input.zip'}}
    task=asyncio.create_task(call(protocol,'codex_worker_inbox',args,RequestContext(client_ref='owner-a')))
    try:
        assert await asyncio.to_thread(entered.wait,3)
        stage=list(store.root.glob('*/.import-*'));assert len(stage)==1
        assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert stage[0].is_dir(), 'Observer cancellation must not delete a live writer stage'
        assert 'capacity busy' in str(await rpc_error(protocol,'codex_worker_inbox',args))
        assert manager.get_job(live).state==JobState.RUNNING
    finally:
        release.set()
        # Queued after the writer, this is a deterministic writer-exit barrier.
        await asyncio.wrap_future(store._import_pool.submit(lambda:None))
    assert not list(store.root.glob('*/.import-*'))
    assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
    assert store._import_slots.acquire(blocking=False);store._import_slots.release()


@pytest.mark.asyncio
async def test_import_slow_drip_http_has_overall_deadline(tmp_path,monkeypatch):
    import http.server, time
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch,import_timeout_seconds=.18,download_timeout_seconds=2,max_concurrent_imports=1)
    sent=threading.Event();stop=threading.Event()
    class Drip(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','100');self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b'x');self.wfile.flush();sent.set()
                    if stop.wait(.02):break
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,*args):pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Drip)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        started=time.monotonic()
        err=await rpc_error(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':f'http://127.0.0.1:{server.server_port}/slow','file_name':'slow.txt'}})
        assert sent.is_set() and 'deadline' in str(err).lower()
        assert time.monotonic()-started<1.5
        await asyncio.wrap_future(handler.artifact_store._import_pool.submit(lambda:None))
        assert not list(handler.artifact_store.root.glob('*/.import-*'))
        assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
    finally:
        stop.set();await asyncio.to_thread(server.shutdown);server.server_close();thread.join(2)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['publication','cleanup'])
async def test_import_failure_truthful_and_preserves_foreign_data(tmp_path,monkeypatch,fault):
    import patchbay.artifacts as artifacts
    _,_,handler,protocol=manager_route(tmp_path,monkeypatch)
    store=handler.artifact_store;foreign=store.root/'foreign';foreign.mkdir();(foreign/'keep').write_text('keep')
    def download(url,path):path.write_bytes(b'exact');return 5,hashlib.sha256(b'exact').hexdigest()
    monkeypatch.setattr(store,'_download',download)
    if fault=='publication':
        original=store._write_json
        def write(path,value):
            if path.name=='manifest.json':raise OSError('controlled publication failure')
            original(path,value)
        monkeypatch.setattr(store,'_write_json',write)
    else:
        original=artifacts.shutil.rmtree
        def remove(path,*a,**kw):
            if path.name.startswith('.import-'):raise PermissionError('controlled cleanup failure')
            return original(path,*a,**kw)
        monkeypatch.setattr(artifacts.shutil,'rmtree',remove)
    error=await rpc_error(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/file','file_name':'exact.txt'}})
    assert error=={'code':-32603,'message':'Internal processing error'} if fault=='publication' else 'incomplete owned staging cleanup' in str(error)
    records=(await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
    assert len(records)==(fault=='cleanup')
    assert (foreign/'keep').read_text()=='keep'
    if records:assert records[0]['sha256']==hashlib.sha256(b'exact').hexdigest()


@pytest.mark.asyncio
async def test_wait_callers_scopes_reconnect_and_real_transition(tmp_path,monkeypatch):
    _,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    runtime=handler.worker_runtime
    manager=runtime.job_manager
    add_worker(manager,executor,worker_id='done',name='Done',state=JobState.COMPLETED)
    running=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    contexts=[RequestContext(client_ref=c,work_run_ref='shared') for c in ['a','b','reconnected-a']]
    args={'scope':'history'}
    for context in contexts:assert (await call(protocol,'codex_worker_wait',args,context))['wait_outcome']=='actionable'
    # Read/list caches and unrelated cache invalidation cannot erase wait observations.
    await call(protocol,'codex_worker_status',args,contexts[0]);runtime._clear_monitoring_caches()
    real_sleep=asyncio.sleep;clock=[0.0];runtime._monotonic_clock=lambda:clock[0]
    async def advance(seconds):clock[0]+=seconds;await real_sleep(0)
    monkeypatch.setattr(asyncio,'sleep',advance)
    assert (await call(protocol,'codex_worker_wait',args,contexts[0]))['wait_outcome']=='timeout'
    assert (await call(protocol,'codex_worker_wait',{'scope':'history','active_only':True},contexts[0]))['wait_outcome']=='timeout'
    changed=[]
    async def complete(seconds):
        clock[0]+=seconds
        if not changed:manager.get_job(running).state=JobState.COMPLETED;changed.append(True)
        await real_sleep(0)
    monkeypatch.setattr(asyncio,'sleep',complete)
    assert (await call(protocol,'codex_worker_wait',args,contexts[0]))['wait_outcome']=='status_changed'
    assert (await call(protocol,'codex_worker_wait',args,contexts[0]))['wait_outcome']=='actionable'
    empty=await call(protocol,'codex_worker_wait',{'scope':'history','active_only':True},contexts[0])
    assert empty['wait_outcome']=='actionable' and not empty['workers']


@pytest.mark.asyncio
async def test_wait_new_work_urgent_cleanup_and_observer_cancellation(tmp_path,monkeypatch):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    add_worker(manager,executor,worker_id='done',name='Done',state=JobState.COMPLETED)
    running=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    context=RequestContext(client_ref='manager')
    args={'scope':'history'}
    await call(protocol,'codex_worker_wait',args,context)
    entered=asyncio.Event();release=asyncio.Event();real_sleep=asyncio.sleep
    async def barrier(seconds):entered.set();await release.wait();await real_sleep(0)
    monkeypatch.setattr(asyncio,'sleep',barrier)
    wait=asyncio.create_task(call(protocol,'codex_worker_wait',args,context));await entered.wait();wait.cancel()
    with pytest.raises(asyncio.CancelledError):await wait
    assert manager.get_job(running).state==JobState.RUNNING
    entered.clear();wait=asyncio.create_task(call(protocol,'codex_worker_wait',args,context));await entered.wait()
    # A new turn sharing the logical worker must be selected, not an obsolete job object.
    manager.get_job(running).state=JobState.COMPLETED
    add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    release.set()
    assert (await wait)['wait_outcome']=='status_changed'
    manager.get_job(running).wrapper_cleanup_outcome='cleanup_pending'
    # An older terminal turn still owns cleanup while the new turn is active.
    for _ in range(2):assert (await call(protocol,'codex_worker_wait',args,context))['wait_outcome']=='actionable'


@pytest.mark.asyncio
async def test_concurrent_imports_capture_request_identity_and_exact_archive(tmp_path,monkeypatch):
    import io,zipfile,json
    _,_,handler,protocol=manager_route(tmp_path,monkeypatch,max_concurrent_imports=2)
    store=handler.artifact_store;entered=threading.Barrier(3);release=threading.Event()
    payloads={}
    for owner in ['alice','bob']:
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:archive.writestr(owner+'.txt',(owner+' exact').encode())
        payloads[owner]=buf.getvalue()
    def download(url,path):
        owner=url.rsplit('/',1)[1];entered.wait(3);assert release.wait(3)
        raw=payloads[owner];path.write_bytes(raw);return len(raw),hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(store,'_download',download)
    async def import_as(owner):
        return await call(protocol,'codex_worker_inbox',{'action':'import_file','label':owner,'artifact_file':{'download_url':'https://fixture.invalid/'+owner,'file_name':owner+'.zip'}},RequestContext(transport_session_id='private-'+owner,client_ref=owner,owner_ref='owner-'+owner,owner_scope='token'))
    pending=[asyncio.create_task(import_as(owner)) for owner in payloads]
    try:
        await asyncio.to_thread(entered.wait,3)
        error=await rpc_error(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/third','file_name':'third'}})
        assert 'capacity busy' in str(error)
        release.set();records=await asyncio.gather(*pending)
    finally:release.set()
    for owner,record in zip(payloads,records):
        assert record['sha256']==hashlib.sha256(payloads[owner]).hexdigest()
        directory=next(store.root.glob('*/'+record['artifact_id']))
        assert (directory/'raw'/f'{owner}.zip').read_bytes()==payloads[owner]
        assert (directory/'unpacked'/f'{owner}.txt').read_bytes()==(owner+' exact').encode()
        metadata=json.loads((directory/'artifact.json').read_text())
        # Verify private stored owner attribution, not merely a public label.
        assert metadata['_mcp_owner_client_ref']==owner
        assert metadata['_mcp_owner_session_hash']=='owner-'+owner
        assert metadata['_mcp_owner_scope']=='token'
        assert 'private-'+owner not in json.dumps(metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize('member',['../escape','large.txt'])
async def test_protocol_import_archive_refusal_is_owned_and_bounded(tmp_path,monkeypatch,member):
    import io,zipfile
    _,_,handler,protocol=manager_route(tmp_path,monkeypatch,max_unpacked_bytes=4,max_single_file_bytes=4)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:archive.writestr(member,b'oversized')
    raw=buf.getvalue()
    def download(url,path):path.write_bytes(raw);return len(raw),hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(handler.artifact_store,'_download',download)
    await rpc_error(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/archive','file_name':'input.zip'}})
    assert not list(handler.artifact_store.root.glob('*/.import-*'))
    assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
    assert not (tmp_path/'escape').exists()


@pytest.mark.asyncio
async def test_optional_pro_dispatch_lost_response_is_never_automatically_retried(tmp_path,monkeypatch):
    from patchbay.pro_requests.tool_surface import PRO_REQUEST_TOOLS,PRO_REQUEST_NON_IDEMPOTENT_TOOLS
    manager,_,handler,protocol=manager_route(tmp_path,monkeypatch)
    report=tmp_path/'report.md';report.write_text('synthetic request')
    request=handler.pro_request_store.create_request(repo_path=manager.config['repositories']['default'],title='Fixture',origin_kind='patchbay_worker',origin_worker='Origin',report_path=str(report))
    await call(protocol,'codex_pro_request_respond',{'request_id':request['id'],'response_markdown':'synthetic answer'})
    admitted=[]
    async def lost(**kwargs):admitted.append(kwargs);raise ConnectionError('synthetic lost reply after inert admission')
    monkeypatch.setattr(handler.worker_runtime,'message_worker',lost)
    await rpc_error(protocol,'codex_pro_request_dispatch',{'request_id':request['id']})
    assert len(admitted)==1 and not manager.jobs
    assert 'codex_pro_request_dispatch' in PRO_REQUEST_NON_IDEMPOTENT_TOOLS
    descriptor=next(t for t in PRO_REQUEST_TOOLS if t['name']=='codex_pro_request_dispatch')
    assert 'never automatically retry' in descriptor['description']
    assert 'key' not in descriptor['inputSchema']['properties']


@pytest.mark.asyncio
async def test_deadline_does_not_release_a_still_blocked_writer(tmp_path,monkeypatch):
    _,_,handler,protocol=manager_route(tmp_path,monkeypatch,max_concurrent_imports=1,import_timeout_seconds=.1)
    store=handler.artifact_store;entered=threading.Event();release=threading.Event()
    def blocked(url,path):
        path.write_bytes(b'owned');entered.set();assert release.wait(3)
        return 5,hashlib.sha256(b'owned').hexdigest()
    monkeypatch.setattr(store,'_download',blocked)
    args={'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/blocked','file_name':'owned'}}
    try:
        error=await rpc_error(protocol,'codex_worker_inbox',args)
        assert entered.is_set() and 'writer cleanup may still be pending' in str(error)
        assert list(store.root.glob('*/.import-*'))
        assert 'capacity busy' in str(await rpc_error(protocol,'codex_worker_inbox',args))
    finally:
        release.set();await asyncio.wrap_future(store._import_pool.submit(lambda:None))
    assert not list(store.root.glob('*/.import-*'))
    assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['final-marker','existing-destination'])
async def test_import_atomic_visibility_and_no_clobber(tmp_path,monkeypatch,fault):
    from pathlib import Path
    from types import SimpleNamespace
    import patchbay.artifacts as artifacts
    manager,_,handler,protocol=manager_route(tmp_path,monkeypatch)
    store=handler.artifact_store
    def download(url,path):path.write_bytes(b'exact');return 5,hashlib.sha256(b'exact').hexdigest()
    monkeypatch.setattr(store,'_download',download)
    foreign=None
    if fault=='final-marker':
        original=Path.rename
        def rename(path,target):
            if path.name=='artifact.json':
                assert not store.list_artifacts(repo_path=manager.config['repositories']['default'])['artifacts']
                raise OSError('controlled final marker failure')
            return original(path,target)
        monkeypatch.setattr(Path,'rename',rename)
    else:
        ids=iter(['a'*32,'b'*32]);monkeypatch.setattr(artifacts.uuid,'uuid4',lambda:SimpleNamespace(hex=next(ids)))
        workspace=store.workspace_id(manager.config['repositories']['default'])
        foreign=store.root/workspace/('art_'+'a'*20);foreign.mkdir(parents=True)
        (foreign/'keep').write_bytes(b'foreign')
    await rpc_error(protocol,'codex_worker_inbox',{'action':'import_file','artifact_file':{'download_url':'https://fixture.invalid/file','file_name':'file.txt'}})
    assert not list(store.root.glob('*/.import-*'))
    assert not (await call(protocol,'codex_worker_inbox',{'action':'list'}))['artifacts']
    if foreign:assert (foreign/'keep').read_bytes()==b'foreign'
    else:assert not list(store.root.glob('*/art_*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('signal',['lost','stale','terminal-runtime-alive'])
async def test_wait_preserves_persistent_urgent_liveness(tmp_path,monkeypatch,signal):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    done=add_worker(manager,executor,worker_id='done',name='Done',state=JobState.COMPLETED)
    live=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    if signal=='terminal-runtime-alive':executor.live_job_ids.add(done)
    runtime=handler.worker_runtime;original=runtime._liveness_for_job
    def liveness(job,*a,**kw):
        result=original(job,*a,**kw)
        if job.job_id==live and signal!='terminal-runtime-alive':result={**result,'status':signal}
        return result
    monkeypatch.setattr(runtime,'_liveness_for_job',liveness)
    for _ in range(2):assert (await call(protocol,'codex_worker_wait',{'scope':'history'},RequestContext(client_ref='urgent')))['wait_outcome']=='actionable'
    assert manager.get_job(live).state==JobState.RUNNING


@pytest.mark.asyncio
async def test_wait_detects_new_work_when_team_count_is_unchanged(tmp_path,monkeypatch):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    done=add_worker(manager,executor,worker_id='done',name='Done',state=JobState.COMPLETED)
    live=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    context=RequestContext(client_ref='manager');args={'scope':'history'}
    await call(protocol,'codex_worker_wait',args,context)
    real_sleep=asyncio.sleep;changed=[];clock=[0.0];handler.worker_runtime._monotonic_clock=lambda:clock[0]
    async def replace(seconds):
        clock[0]+=seconds
        if not changed:
            manager.jobs.pop(done)
            changed.append(add_worker(manager,executor,worker_id='new',name='New',state=JobState.RUNNING,heartbeat_age=0,live=True))
        await real_sleep(0)
    monkeypatch.setattr(asyncio,'sleep',replace)
    result=await call(protocol,'codex_worker_wait',args,context)
    assert result['wait_outcome']=='status_changed'
    assert {w['worker_id'] for w in result['workers']}=={'live','new'}
    assert manager.get_job(live).state==JobState.RUNNING


@pytest.mark.asyncio
async def test_wait_observes_meaningful_event_between_calls_once(tmp_path,monkeypatch):
    manager,executor,handler,protocol=manager_route(tmp_path,monkeypatch)
    live=add_worker(manager,executor,worker_id='live',name='Live',state=JobState.RUNNING,heartbeat_age=0,live=True)
    context=RequestContext(client_ref='manager');real_sleep=asyncio.sleep;clock=[0.0]
    handler.worker_runtime._monotonic_clock=lambda:clock[0]
    async def advance(seconds):clock[0]+=seconds;await real_sleep(0)
    monkeypatch.setattr(asyncio,'sleep',advance)
    args={'scope':'history'}
    assert (await call(protocol,'codex_worker_wait',args,context))['wait_outcome']=='timeout'
    manager.get_job(live).event_count+=1
    fresh=await call(protocol,'codex_worker_wait',args,context)
    assert fresh['wait_outcome']=='actionable' and fresh['waited_seconds']==0
    assert (await call(protocol,'codex_worker_wait',args,context))['wait_outcome']=='timeout'
