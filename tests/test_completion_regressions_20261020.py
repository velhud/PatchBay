"""Cross-component audit regressions using real bounded local fixtures."""
import asyncio
import io
import json
import queue
import threading
import time
from pathlib import Path

import pytest

from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver, MAX_SESSION_RECORD_BYTES
from patchbay.owner_identity import session_ref_salt
from patchbay.workers.model_options import worker_execution_policy
from patchbay.workers.runtime import WorkerRuntime
from patchbay.tools.power import PowerToolRunner
from patchbay.workspace.context import WorkspaceContext
from test_worker_runtime import make_config, RecordingExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize('model,effort', [('gpt-6-sol','high'), ('gpt-5.6-luna','high'), ('gpt-6-luna','ultra'), ('gpt-6-luna','low')])
async def test_disallowed_worker_settings_have_no_creation_effect(tmp_path, model, effort):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    with pytest.raises(ValueError, match='Luna-only'):
        await runtime.start_worker(name='Rejected', brief='Read source', repo_path=config['repositories']['default'],
                                   model=model, reasoning_effort=effort)
    assert not manager.jobs and not executor.scheduled
    assert not list(Path(config['workers']['worktree_root']).glob('*'))


@pytest.mark.asyncio
async def test_omitted_worker_settings_cannot_inherit_ambient_model(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    runtime = WorkerRuntime(config, manager, RecordingExecutor(manager))
    result = await runtime.start_worker(name='Explicit Luna', brief='Read source', workspace_mode='read_only',
                                       repo_path=config['repositories']['default'])
    assert (result['model'], result['reasoning_effort']) == ('gpt-6-luna', 'high')
    job = next(iter(manager.jobs.values()))
    assert 'nested helpers MUST explicitly use gpt-6-luna' in job.prompt


def test_owner_namespace_survives_new_process_and_rejects_corruption(tmp_path):
    import subprocess, sys
    config = {'logging': {'job_state_dir': str(tmp_path)}}
    value = session_ref_salt(config)
    code = 'import json,sys; from patchbay.owner_identity import session_ref_salt; print(session_ref_salt(json.loads(sys.argv[1])))'
    result = subprocess.run([sys.executable, '-c', code, json.dumps(config)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == value
    (tmp_path/'owner-namespace').write_text('broken')
    with pytest.raises(ValueError, match='recovery'):
        session_ref_salt(config)


def test_session_tail_bounds_partial_record_and_resynchronizes(tmp_path):
    session = 'fixture-session'
    path = tmp_path/'sessions'/f'{session}.jsonl'
    path.parent.mkdir()
    path.write_text(json.dumps({'type':'session_meta', 'payload':{'id':session}})+'\n')
    observer = CodexSessionTerminalObserver({'power_tools':{'codex_home':str(tmp_path)}}, session, not_before=0)
    observer.prime_to_end()
    with path.open('ab') as output:
        output.write(b'x'*(MAX_SESSION_RECORD_BYTES*3))
    for _ in range(14):
        assert not observer.poll().completed
        assert len(observer._pending) <= MAX_SESSION_RECORD_BYTES
    assert observer.oversized_records == 1
    with path.open('a') as output:
        output.write('\n'+json.dumps({'timestamp':'2026-10-20T00:00:00Z','type':'event_msg',
                     'payload':{'type':'task_complete','last_agent_message':'exact final'}})+'\n')
    snapshot = observer.poll()
    assert snapshot.completed and snapshot.final_message == 'exact final'


@pytest.mark.asyncio
async def test_safe_shell_rejects_original_newline_before_normalization(tmp_path):
    config = make_config(tmp_path)
    config['power_tools'] = {'bash_mode':'safe'}
    runner = PowerToolRunner(config, WorkspaceContext(config))
    with pytest.raises(ValueError, match='multiline'):
        await runner.run_command({'command': "pwd\npython3 -c \"open('unexpected','w').write('bad')\""})
    assert not (Path(config['repositories']['default'])/'unexpected').exists()


def test_search_literal_dash_and_fallback_containment(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    root = Path(config['repositories']['default'])
    (root/'literal.txt').write_text('a --files b\n')
    outside = tmp_path/'outside.txt'; outside.write_text('unique-outside-secret\n')
    (root/'escape.txt').symlink_to(outside)
    context = WorkspaceContext(config)
    workspace = context.open_workspace(str(root))
    actual = context._search_with_rg(workspace, root, '--files', False, True, 10, None, 5000)
    assert any('literal.txt' == hit['path'] for hit in actual['matches'])
    fallback = context._search_with_python(workspace, root, 'unique-outside-secret', False, True, 10, None, 5000)
    assert fallback['matches'] == []


@pytest.mark.asyncio
async def test_stdio_input_wait_does_not_starve_scheduled_work(tmp_path, monkeypatch):
    import patchbay.stdio as stdio
    import yaml
    observed = []
    incoming = queue.Queue(); incoming.put('start\n')
    fallback = threading.Timer(.5, lambda: incoming.put(None)); fallback.start()
    class FakeServer:
        def __init__(self, *args, **kwargs): pass
        async def handle_line(self, line):
            async def background():
                await asyncio.sleep(.01)
                observed.append('ran')
                incoming.put(None)
            asyncio.create_task(background())
            return {'accepted': True}
    config = tmp_path/'config.yaml'; config.write_text(yaml.safe_dump({}))
    monkeypatch.setattr(stdio, 'StdioMCPServer', FakeServer)
    started = time.monotonic()
    try:
        await stdio.run_stdio(config, input_stream=iter(incoming.get, None), output_stream=io.StringIO())
        assert observed == ['ran'] and time.monotonic()-started < .4
    finally:
        fallback.cancel(); fallback.join()


def test_hub_enrollment_is_atomic_retryable_and_preserves_checkout_identity(tmp_path, monkeypatch):
    from test_hub_runtime_v2 import make_runtime
    from patchbay.hub.runtime_v2 import MACHINE_ENTITY, ENROLLMENT_ENTITY
    from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH
    runtime, store, _ = make_runtime(tmp_path)
    code = runtime.create_enrollment_code(name='fixture')['code']
    with pytest.raises(ValueError):
        runtime.enroll_machine(code=code, machine_id='fixture', workspaces=[{}])
    assert store.get_entity(MACHINE_ENTITY, 'fixture') is None
    assert not store.get_entity(ENROLLMENT_ENTITY, code)['record'].get('used_at')
    workspaces = [{'path':'/srv/checkout-a', 'remote_url':'https://example.invalid/repo'},
                  {'path':'/srv/checkout-b', 'remote_url':'https://example.invalid/repo'}]
    put = store._put_entity_in_transaction
    def injected(connection, entity_type, *args, **kwargs):
        if entity_type == 'hub.workspace_projection':
            raise RuntimeError('injected transaction failure')
        return put(connection, entity_type, *args, **kwargs)
    monkeypatch.setattr(store, '_put_entity_in_transaction', injected)
    with pytest.raises(RuntimeError, match='injected'):
        runtime.enroll_machine(code=code, machine_id='fixture', workspaces=workspaces)
    assert store.get_entity(MACHINE_ENTITY, 'fixture') is None
    monkeypatch.setattr(store, '_put_entity_in_transaction', put)
    enrolled = runtime.enroll_machine(code=code, machine_id='fixture', workspaces=workspaces)
    with pytest.raises(ValueError, match="already used"):
        runtime.enroll_machine(code=code, machine_id="fixture", workspaces=workspaces)
    runtime.heartbeat(machine_id='fixture', token=enrolled['node_token'], edge_generation=enrolled['edge_generation'],
                      projection_revision=1, capabilities={'contract_hash': HUB_V2_CONTRACT_HASH}, workspaces=workspaces)
    projections = runtime._matching_workspace_projections(machine_ids=['fixture'])
    assert {p['local_path'] for p in projections} == {'/srv/checkout-a','/srv/checkout-b'}
    next_code = runtime.create_enrollment_code(name='replacement')['code']
    replacement = runtime.enroll_machine(code=next_code, machine_id='fixture')
    runtime.heartbeat(machine_id='fixture', token=replacement['node_token'], edge_generation=replacement['edge_generation'],
                      projection_revision=1, capabilities={'contract_hash': HUB_V2_CONTRACT_HASH})
    assert runtime._matching_workspace_projections(machine_ids=['fixture']) == []


def test_group_cannot_certify_unobserved_delivery():
    from patchbay.hub.groups_v2 import validate_close_dispositions
    worker = {'fleet_worker_ref':'worker', 'turn_state':'completed', 'liveness':'terminal',
              'integration_state':'not_integrated'}
    result = validate_close_dispositions([worker], {'worker':'integrated'}, outcome='complete')
    assert not result['accepted']
    assert result['blockers'][0]['reason'] == 'integrated_disposition_requires_delivery_evidence'
    worker['integration_state'] = 'applied_to_checkout'
    assert validate_close_dispositions([worker], {'worker':'integrated'}, outcome='complete')['accepted']


def test_stream_telemetry_coalesces_writes_but_terminal_evidence_is_durable(tmp_path, monkeypatch):
    from patchbay.jobs.executor import JobExecutor
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job = manager.create_job('plan','fixture',config['repositories']['default'],{})
    writes = []
    sync = manager._fsync_job_state_dir
    def record():
        writes.append(1); sync()
    monkeypatch.setattr(manager, '_fsync_job_state_dir', record)
    for _ in range(1000):
        executor._observe_stdout_event(job, b'{"type":"fixture.event"}\n', {})
    assert len(writes) < 20
    assert manager.get_job(job).event_count == 1000
    manager.update_job_state(job, JobState.COMPLETED, result={'summary':'complete'})
    persisted = json.loads(manager._job_record_path(job).read_text())
    assert persisted['state'] == 'completed' and persisted['event_count'] == 1000
