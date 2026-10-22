"""Durable worker admissions without any model execution."""
import asyncio
import subprocess
import sys

import pytest

from patchbay.jobs.manager import JobManager, JobState
from patchbay.protocol.context import RequestContext
from patchbay.workers.runtime import WorkerRuntime
from patchbay.protocol.mcp import MCPProtocol
from patchbay.tools.handler import ToolHandler
from test_worker_runtime import RecordingExecutor, make_config


def runtime_for(config):
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    return WorkerRuntime(config, manager, executor), executor


@pytest.mark.asyncio
async def test_keyed_start_replays_after_loss_and_restart_but_new_intent_can_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    config['server']['max_concurrent_jobs'] = 0
    runtime, executor = runtime_for(config)
    context = RequestContext(owner_ref='principal_fixture', owner_scope='server', client_ref='transport_one')
    args = dict(name='reviewer', brief='Intentionally identical brief', repo_path=config['repositories']['default'],
        workspace_mode='read_only', auto_suffix=True, request_context=context, idempotency_key='start-intent')
    first = await runtime.start_worker(**args)
    assert first['accepted'] and len(executor.started) == 1
    replay = await runtime.start_worker(**args)
    assert replay['request_replayed'] and replay['worker_id'] == first['worker_id']
    assert len(executor.started) == 1
    fresh, fresh_executor = runtime_for(config)
    args['request_context'] = RequestContext(owner_ref='principal_fixture', owner_scope='server', client_ref='transport_two')
    recovered = await fresh.start_worker(**args)
    assert recovered['worker_id'] == first['worker_id'] and recovered['request_replayed']
    assert not fresh_executor.started
    with pytest.raises(ValueError, match='different worker request'):
        await fresh.start_worker(**{**args, 'brief': 'Changed semantics'})
    independent = await runtime.start_worker(**{**args, 'idempotency_key': 'independent-review'})
    assert independent['worker_id'] != first['worker_id']
    assert independent['name'] != first['name'] and len(executor.started) == 2
    unicode_request = await runtime.start_worker(**{**args, 'idempotency_key': 'é' * 200})
    unicode_replay = await runtime.start_worker(**{**args, 'idempotency_key': 'é' * 200})
    assert unicode_request['worker_id'] == unicode_replay['worker_id'] and unicode_replay['request_replayed']
    assert len(executor.started) == 3


@pytest.mark.asyncio
async def test_same_key_concurrency_admits_once_and_message_retry_never_repeats_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    runtime, executor = runtime_for(config)
    args = dict(name='author', brief='Fixture', repo_path=config['repositories']['default'],
        workspace_mode='read_only', auto_suffix=True, idempotency_key='start')
    first, replay = await asyncio.gather(runtime.start_worker(**args), runtime.start_worker(**args))
    assert first['worker_id'] == replay['worker_id'] and len(executor.started) == 1
    job = next(iter(runtime.job_manager.jobs.values()))
    # A pending/running turn refuses new follow-up work; there is no hidden queue.
    refused = await runtime.message_worker(worker='author', message='Next', idempotency_key='follow-up')
    assert not refused['accepted'] and len(executor.started) == 1
    runtime.job_manager.update_job_state(job.job_id, JobState.COMPLETED, session_id='fixture-session')
    follow = dict(worker='author', message='Next', idempotency_key='follow-up')
    accepted = await runtime.message_worker(**follow)
    assert accepted['accepted'] and len(executor.started) == 2
    assert (await runtime.message_worker(**follow))['request_replayed']
    newest = runtime.job_manager.jobs[executor.started[-1]]
    runtime.job_manager.update_job_state(newest.job_id, JobState.COMPLETED, session_id='fixture-session')
    fresh, fresh_executor = runtime_for(config)
    assert (await fresh.message_worker(**follow))['request_replayed']
    assert not fresh_executor.started
    assert (await runtime.message_worker(**{**follow, 'idempotency_key': 'intentional-next'}))['accepted']
    assert len(executor.started) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['encoding', 'after_schedule'])
async def test_local_post_effect_fault_recovers_exact_worker_without_duplicate(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    runtime, executor = runtime_for(config)
    handler = ToolHandler(config, runtime.job_manager, executor)
    handler.worker_runtime = runtime
    protocol = MCPProtocol(config, handler)
    args = {'name': 'fault fixture', 'brief': 'No inference', 'workspace_mode': 'read_only',
        'auto_suffix': True, 'idempotency_key': 'fault-key'}
    if failure == 'encoding':
        original = runtime._public_view
        def broken(*values, **options):
            return {**original(*values, **options), 'unencodable_fixture': '\ud800'}
        monkeypatch.setattr(runtime, '_public_view', broken)
    else:
        original = runtime._schedule_job
        def broken(job_id):
            original(job_id)
            raise RuntimeError('private-exception-must-not-escape')
        monkeypatch.setattr(runtime, '_schedule_job', broken)
    response = await protocol.handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'codex_worker_start', 'arguments': args}})
    assert response['error']['code'] == -32603
    data = response['error']['data']
    assert data['effect_may_exist'] and not data['success_asserted']
    assert data['original_key'] == args['idempotency_key']
    assert data['worker_id'] == runtime._worker_id(next(iter(runtime.job_manager.jobs.values())))
    assert len(executor.started) == 1 and len(runtime.job_manager.jobs) == 1
    import json
    rendered = json.dumps(response, ensure_ascii=False)
    rendered.encode('utf-8')
    assert 'private-exception-must-not-escape' not in rendered
    if failure == 'encoding':
        monkeypatch.setattr(runtime, '_public_view', original)
    else:
        monkeypatch.setattr(runtime, '_schedule_job', original)
    replay = await protocol.handle_message({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'codex_worker_start', 'arguments': args}})
    assert replay['result']['structuredContent']['request_replayed']
    assert len(executor.started) == 1 and len(runtime.job_manager.jobs) == 1


@pytest.mark.asyncio
async def test_crashed_admission_lock_owner_does_not_block_legitimate_new_work(tmp_path, monkeypatch):
    monkeypatch.setenv('PATCHBAY_HOME', str(tmp_path / 'private-runtime'))
    config = make_config(tmp_path)
    runtime, executor = runtime_for(config)
    lock = runtime.job_manager.job_state_dir / 'worker-admission.lock'
    child = subprocess.Popen([sys.executable, '-u', '-c',
        "import fcntl,sys,time; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)",
        str(lock)], stdout=subprocess.PIPE, text=True)
    pending = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=3) == 'locked\n'
        pending = asyncio.create_task(runtime.start_worker(name='after lock owner', brief='Fixture',
            repo_path=config['repositories']['default'], workspace_mode='read_only', idempotency_key='after-crash'))
        await asyncio.sleep(.05)
        assert not pending.done() and not runtime.job_manager.jobs
        child.terminate()
        await asyncio.to_thread(child.wait, timeout=3)
        assert (await asyncio.wait_for(pending, timeout=5))['accepted']
        assert len(executor.started) == 1
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=3)
        child.stdout.close()
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
