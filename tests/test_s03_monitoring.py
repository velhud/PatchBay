"""Real runtime/journal observation with recording executors; no inference."""
import time

import pytest

from patchbay.jobs.manager import JobManager, JobState
from patchbay.workers.runtime import WorkerRuntime
from test_worker_projection_v2 import make_config, ProjectionExecutor, add_worker
from test_hub_edge_client_v2 import FakeToolHandler, FakeHttpTransport, _capabilities, _service, _runner, DEFAULT_ENDPOINTS


@pytest.mark.asyncio
async def test_quiet_transitions_publish_through_real_runtime_and_edge_journal(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config['workers'].update(heartbeat_fresh_seconds=120, heartbeat_quiet_seconds=600)
    manager, executor = JobManager(config), ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    job_id = add_worker(manager, executor, worker_id='quiet-fixture', name='Quiet',
        state=JobState.RUNNING, heartbeat_age=0, live=True)
    stamp = manager.get_job(job_id).last_heartbeat_at
    now = [stamp]
    monkeypatch.setattr('patchbay.workers.runtime_legacy.time.time', lambda: now[0])
    handler = FakeToolHandler()
    handler.worker_runtime = runtime
    journal, execution = _service(tmp_path/'edge.sqlite3', handler, _capabilities())
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)
    try:
        for age, expected, publications in [(0,'active',1),(50,'active',1),(121,'quiet',2),
                                           (200,'quiet',2),(601,'stale',3),(1200,'stale',3)]:
            now[0] = stamp+age
            await runner.projection_once()
            assert runner._latest_projection['workers'][0]['liveness'] == expected
            assert len(transport.calls[DEFAULT_ENDPOINTS.projection]) == publications
        # Wall-clock rollback is an observation change, never cancellation.
        now[0] = stamp+1
        await runner.projection_once()
        assert runner._latest_projection['workers'][0]['liveness'] == 'active'
        # Changed display policy invalidates without a job event.
        config['workers'].update(heartbeat_fresh_seconds=0, heartbeat_quiet_seconds=0)
        await runner.projection_once()
        assert runner._latest_projection['workers'][0]['liveness'] == 'stale'
        # A real retained checkpoint resets freshness, then ages on its own.
        config['workers'].update(heartbeat_fresh_seconds=5, heartbeat_quiet_seconds=10)
        now[0] = stamp+50
        manager.update_job_state(job_id,JobState.RUNNING,checkpoints=[{'at':now[0],'summary':'Checkpoint'}])
        await runner.projection_once()
        assert runner._latest_projection['workers'][0]['liveness'] == 'active'
        now[0] += 6
        await runner.projection_once()
        assert runner._latest_projection['workers'][0]['liveness'] == 'stale'
        # Reopening a producer reconstructs state; no saved Chat handoff needed.
        handler.worker_runtime = WorkerRuntime(config,manager,executor)
        runner._last_published_projection_state_token = None
        await runner.projection_once()
        assert runner._latest_projection['workers'][0]['liveness'] == 'stale'
        assert manager.get_job(job_id).state == JobState.RUNNING
    finally:
        journal.close()


@pytest.mark.parametrize('field,value', [('liveness_status','quiet'),('phase','waiting_for_input'),
    ('cleanup_pending',True),('cleanup_unresolved',True)])
def test_decision_only_delta_is_changed_without_output(field, value):
    runtime = object.__new__(WorkerRuntime)
    previous = dict(state='working',liveness_status='active',phase='model_reasoning',last_event='',
        event_count=10,stdout_bytes_seen=0,stderr_bytes_seen=0,checkpoint_count=0,
        cleanup_pending=False,cleanup_unresolved=False)
    delta = runtime._status_delta({**previous,field:value},previous)
    assert delta['changed'] and delta['events_delta'] == 0
    assert delta['line'] != 'no new events or output'


def test_silence_deadline_crossed_during_snapshot_stays_due(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config['workers'].update(heartbeat_fresh_seconds=5, heartbeat_quiet_seconds=10)
    manager, executor = JobManager(config), ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    job_id = add_worker(manager, executor, worker_id='crossing-fixture', name='Crossing',
        state=JobState.RUNNING, heartbeat_age=0, live=True)
    stamp = manager.get_job(job_id).last_heartbeat_at
    now = [stamp]
    monkeypatch.setattr('patchbay.workers.runtime_legacy.time.time', lambda: now[0])
    original = runtime._worker_projection
    def crossing(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = stamp + 6
        return result
    monkeypatch.setattr(runtime, '_worker_projection', crossing)
    before = runtime.projection_state_token()
    runtime._build_projection_snapshot()
    assert runtime.projection_state_token() != before


@pytest.mark.asyncio
async def test_actual_handler_cache_hit_does_not_reconcile_again(tmp_path,monkeypatch):
    from patchbay.tools.handler import ToolHandler
    monkeypatch.setenv('PATCHBAY_HOME',str(tmp_path/'runtime'))
    config=make_config(tmp_path)
    manager,executor=JobManager(config),ProjectionExecutor()
    handler=ToolHandler(config,manager,executor)
    await handler.handle_tool_call('codex_worker_status',{'scope':'history'})
    count=executor.reconcile_calls
    cached=await handler.handle_tool_call('codex_worker_status',{'scope':'history'})
    assert cached['poll_too_early'] and executor.reconcile_calls==count


@pytest.mark.asyncio
async def test_wait_reads_actionable_state_first_and_observer_cancel_keeps_work(tmp_path):
    import asyncio
    config=make_config(tmp_path)
    manager,executor=JobManager(config),ProjectionExecutor()
    runtime=WorkerRuntime(config,manager,executor)
    job_id=add_worker(manager,executor,worker_id='wait-fixture',name='Wait',state=JobState.RUNNING,
        heartbeat_age=0,live=True)
    pending=asyncio.create_task(runtime.worker_wait(wait_seconds=30))
    await asyncio.sleep(.03)
    pending.cancel()
    await asyncio.gather(pending,return_exceptions=True)
    assert manager.get_job(job_id).state==JobState.RUNNING and job_id in executor.live_job_ids
    async def complete():
        await asyncio.sleep(.03)
        manager.update_job_state(job_id,JobState.COMPLETED,result={'summary':'Fixture complete'},completed_at=time.time(),
            wrapper_cleanup_outcome='cleanup_pending')
    started=time.monotonic()
    result,_=await asyncio.gather(runtime.worker_wait(wait_seconds=30),complete())
    assert time.monotonic()-started < 2
    assert result['wait_outcome']=='status_changed' and result['counts']['completed']==1
    assert result['workers'][0]['cleanup_pending']  # Report does not release live fixture ownership.
    again=await runtime.worker_wait(wait_seconds=30)
    assert again['wait_outcome']=='actionable' and again['waited_seconds']<2
