"""Actual job persistence/recovery and fake-native launch; no model invocation."""
import json
import sys
import time
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver, MAX_POLL_BYTES
from test_session_terminal import make_config, write_session


def terminal(message):
    return {'timestamp': time.time(), 'type': 'event_msg',
            'payload': {'type': 'task_complete', 'last_agent_message': message}}


def test_recovery_advances_across_restart_and_bounded_reads(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job('resume', 'recover', config['repositories']['default'],
                                {'resume_session_id': 'recovery-fixture'})
    manager.update_job_state(job_id, JobState.RUNNING, session_id='recovery-fixture',
                             process_started_at=time.time()-5)
    source = write_session(Path(config['power_tools']['codex_home']), 'recovery-fixture', [])
    with source.open('a') as handle:
        for _ in range(3500):
            handle.write(json.dumps({'type': 'diagnostic', 'text': 'x'*1000})+'\n')
        handle.write(json.dumps(terminal('Reached distant terminal'))+'\n')
    assert source.stat().st_size > MAX_POLL_BYTES*3
    for _ in range(6):
        # The actual manager snapshot, not only an observer object, survives.
        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        job = manager.get_job(job_id)
        executor._recover_completed_session(job)
        if job.state == JobState.COMPLETED:
            break
    assert job.state == JobState.COMPLETED
    assert job.result['summary'] == 'Reached distant terminal'


def test_typed_identity_and_resume_conflict_are_durable(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job('resume', 'identity', config['repositories']['default'],
                                {'resume_session_id': 'expected-fixture'})
    manager.update_job_state(job_id, JobState.RUNNING, session_id='expected-fixture')
    assert executor._session_id_from_event({'type': 'item.started', 'payload': {'id': 'item-id'}}) is None
    assert executor._extract_session_id('session id 01234567-0123-0123-0123-012345678901') is None
    state = {'session_id': 'expected-fixture'}
    executor._observe_stdout_event(job_id, json.dumps({'type': 'thread.started',
        'thread_id': 'other-fixture'}).encode(), state)
    recovered = JobManager(config).get_job(job_id)
    assert recovered.native_outcome['kind'] == 'session_identity_conflict'
    assert recovered.native_outcome['expected_session_id'] == 'expected-fixture'
    assert recovered.native_outcome['observed_session_id'] == 'other-fixture'
    assert recovered.session_id == 'expected-fixture'


def test_large_report_has_explicit_completeness_and_complete_source(tmp_path):
    config = make_config(tmp_path)
    message = 'Verification FAILED; do not accept.\n' + 'α🙂'*120000 + '\nFinal caveat'
    write_session(Path(config['power_tools']['codex_home']), 'report-fixture', [terminal(message)])
    observer = CodexSessionTerminalObserver(config, 'report-fixture', not_before=0)
    for _ in range(4):
        snapshot = observer.poll()
        if snapshot.completed:
            break
    assert snapshot.completed and snapshot.report_truncated
    assert snapshot.original_characters == len(message)
    assert snapshot.original_bytes == len(message.encode())
    assert snapshot.final_message.startswith('Verification FAILED')
    assert snapshot.final_message.endswith('Final caveat')
    assert observer.complete_report() == message


@pytest.mark.asyncio
async def test_native_failure_survives_tail_eviction_and_generic_completion(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config['logging'].update(job_log_max_bytes=4096, process_capture_max_bytes=4096)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job('plan', 'failure evidence', config['repositories']['default'], {})
    write_session(Path(config['power_tools']['codex_home']), 'failure-fixture',
                  [terminal('Generic transcript completion')])
    script = (
        'import json\n'
        "print(json.dumps({'type':'thread.started','thread_id':'failure-fixture'}),flush=True)\n"
        "print(json.dumps({'type':'turn.failed','error':{'message':'Permanent failure'}}),flush=True)\n"
        "for _ in range(100):print(json.dumps({'type':'noise','text':'x'*1000}),flush=True)\n"
    )
    monkeypatch.setattr(executor, '_build_codex_command', lambda *a: [sys.executable, '-u', '-c', script])
    await executor.execute_job(job_id)
    job = manager.get_job(job_id)
    assert job.state == JobState.FAILED
    assert job.native_outcome['kind'] == 'turn.failed'
    restored = JobManager(config).get_job(job_id)
    assert restored.native_outcome == job.native_outcome


@pytest.mark.asyncio
async def test_complete_report_is_paged_through_actual_worker_inspect(tmp_path):
    from patchbay.tools.handler import ToolHandler
    from test_worker_projection_v2 import make_config as worker_config, add_worker, ProjectionExecutor
    config = worker_config(tmp_path)
    config['power_tools'] = {'codex_home': str(tmp_path/'home')}
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = add_worker(manager, ProjectionExecutor(), worker_id='source-fixture', name='Source',
                       state=JobState.COMPLETED, live=False)
    message = 'Leading qualification\n'+'α🙂'*400+'\nTrailing qualification'
    write_session(tmp_path/'home', 'source-session', [terminal(message)])
    observer = CodexSessionTerminalObserver(config, 'source-session', not_before=0, max_final_message_chars=200)
    snapshot = observer.poll()
    result = executor._result_from_session_message(snapshot.final_message,
        manager.job_logs_dir/f'{job_id}_result.json', snapshot=snapshot, observer=observer)
    manager.update_job_state(job_id, JobState.COMPLETED, result=result)
    handler = ToolHandler(config, manager, executor)
    sha = result['report_source']['sha256']
    offset = 0
    parts = []
    while True:
        page = await handler.handle_tool_call('codex_worker_inspect',
            {'worker':'source-fixture','view':'report_source','report_source_sha256':sha,
             'report_offset':offset,'max_bytes':513})
        assert page['source_sha256'] == sha
        parts.append(page['text'])
        if not page['has_more']:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert ''.join(parts) == message
    report = await handler.worker_runtime.inspect_worker(worker='source-fixture', view='report')
    assert report['report_truncated'] and report['report_source']['sha256'] == sha
    with pytest.raises(ValueError):
        await handler.worker_runtime.inspect_worker(worker='source-fixture',view='report_source',report_source_sha256='0'*64)
    with pytest.raises(ValueError):
        await handler.worker_runtime.inspect_worker(worker='source-fixture',view='report_source',report_source_sha256=sha,report_offset=-1)
    (manager.job_logs_dir/f'{job_id}_report_{sha}.txt').write_text('changed')
    with pytest.raises(ValueError,match='changed'):
        await handler.worker_runtime.inspect_worker(worker='source-fixture',view='report_source',report_source_sha256=sha)


def test_restart_reconstructs_split_utf8_record_and_latest_message(tmp_path):
    config=make_config(tmp_path)
    message='α🙂'*(MAX_POLL_BYTES//5)
    source=write_session(Path(config['power_tools']['codex_home']),'split-fixture',[])
    record=dict(timestamp=time.time(),type='event_msg',payload=dict(type='agent_message',message=message))
    with source.open('ab') as handle:
        handle.write((json.dumps(record,ensure_ascii=False)+'\n').encode())
        handle.write((json.dumps(terminal(''))+'\n').encode())
    observer=CodexSessionTerminalObserver(config,'split-fixture',not_before=0)
    assert not observer.poll().completed
    checkpoint=observer.checkpoint()
    assert checkpoint['offset']>checkpoint['record_boundary']
    observer=CodexSessionTerminalObserver(config,'split-fixture',not_before=0,checkpoint=checkpoint)
    snapshot=observer.poll()
    assert snapshot.completed and observer.complete_report()==message


@pytest.mark.parametrize('change',['replace','truncate','same_size'])
def test_checkpoint_refuses_changed_source_generation(tmp_path,change):
    config=make_config(tmp_path)
    source=write_session(Path(config['power_tools']['codex_home']),'generation-fixture',[])
    observer=CodexSessionTerminalObserver(config,'generation-fixture',not_before=0)
    observer.poll();checkpoint=observer.checkpoint()
    original=source.read_bytes()
    if change=='replace':
        source.unlink();source.write_bytes(original)
    elif change=='truncate':source.write_bytes(original[:10])
    else:source.write_bytes(original.replace(b'/fixture',b'/altered'))
    resumed=CodexSessionTerminalObserver(config,'generation-fixture',not_before=0,checkpoint=checkpoint)
    snapshot=resumed.poll()
    assert not snapshot.completed and snapshot.source_status!='current'


def test_missing_resume_baseline_cannot_use_old_completion_but_stdout_receipt_can(tmp_path):
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('resume','baseline',config['repositories']['default'],
        {'resume_session_id':'baseline-fixture'})
    manager.update_job_state(job_id,JobState.RUNNING,session_id='baseline-fixture')
    executor._prepare_session_observation(job_id,manager.get_job(job_id))
    write_session(Path(config['power_tools']['codex_home']),'baseline-fixture',[terminal('old')])
    job=manager.get_job(job_id)
    assert not executor._recover_completed_session(job)
    assert job.state==JobState.RUNNING
    state={'session_id':'baseline-fixture'}
    executor._observe_stdout_event(job_id,json.dumps({'type':'turn.completed'}).encode(),state)
    assert executor._transition_from_completion_evidence(job)
    assert job.state==JobState.COMPLETED


def test_recovery_home_binding_drift_cannot_select_other_home_transcript(tmp_path):
    from patchbay.native_profile import digest
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('plan','home',config['repositories']['default'],{})
    manager.update_job_state(job_id,JobState.RUNNING,session_id='home-fixture',
        native_execution_provenance={'home_id':digest(str(tmp_path/'prior-home'))})
    write_session(Path(config['power_tools']['codex_home']),'home-fixture',[terminal('wrong home')])
    job=manager.get_job(job_id)
    assert not executor._recover_completed_session(job)
    assert job.options['_session_scan_status']=='native_home_binding_changed'


@pytest.mark.parametrize('kind',['turn.failed','error'])
def test_typed_negative_survives_restart_and_overrides_positive(tmp_path,kind):
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('plan','negative',config['repositories']['default'],{})
    manager.update_job_state(job_id,JobState.RUNNING,session_id='negative-fixture')
    state={'session_id':'negative-fixture'}
    executor._observe_stdout_event(job_id,json.dumps({'type':kind,'error':{'message':'fatal fixture'},'message':'fatal fixture'}).encode(),state)
    executor._observe_stdout_event(job_id,json.dumps({'type':'turn.completed'}).encode(),state)
    manager=JobManager(config);executor=JobExecutor(config,manager);job=manager.get_job(job_id)
    assert executor._recover_native_outcome(job)
    assert job.state==JobState.FAILED
    assert job.native_outcome['conflicting_positive_source']=='stdout_turn_completed'
    assert not executor._transition_from_completion_evidence(job)


def test_item_error_and_report_prose_are_not_top_level_fatal_evidence(tmp_path):
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('plan','prose',config['repositories']['default'],{})
    for record in [{'type':'item.completed','item':{'type':'error','message':'a tool failed'}},
                   {'type':'item.completed','item':{'type':'agent_message','text':'An error was repaired.'}}]:
        executor._observe_stdout_event(job_id,json.dumps(record).encode(),{})
    assert manager.get_job(job_id).native_outcome is None


def test_oversized_record_is_visible_and_later_terminal_survives(tmp_path):
    from patchbay.jobs.session_terminal import MAX_SESSION_RECORD_BYTES
    config=make_config(tmp_path)
    source=write_session(Path(config['power_tools']['codex_home']),'oversized-fixture',[])
    with source.open('ab') as handle:
        handle.write(b'x'*(MAX_SESSION_RECORD_BYTES+10)+b'\n')
        handle.write(json.dumps(terminal('Valid later report')).encode())
    observer=CodexSessionTerminalObserver(config,'oversized-fixture',not_before=0)
    for _ in range(7):
        snapshot=observer.poll()
        if snapshot.completed:break
    assert snapshot.completed and snapshot.oversized_records==1
    assert observer.complete_report()=='Valid later report'


def test_structured_report_clipping_is_explicit_without_claimed_schema_failure(tmp_path):
    from test_session_terminal import full_result
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('plan','structured',config['repositories']['default'],{})
    report=full_result('x'*1000)
    write_session(Path(config['power_tools']['codex_home']),'structured-fixture',[terminal(json.dumps(report))])
    observer=CodexSessionTerminalObserver(config,'structured-fixture',not_before=0,max_final_message_chars=200)
    snapshot=observer.poll()
    result=executor._result_from_session_message(snapshot.final_message,manager.job_logs_dir/f'{job_id}_result.json',snapshot=snapshot,observer=observer)
    assert result['report_truncated'] and result['report_completeness']=='truncated'
    assert result['report_source']['bytes']==len(json.dumps(report).encode())
    assert result.get('failure_diagnostic') is None


def test_identity_conflict_cannot_be_hidden_by_prior_native_failure(tmp_path):
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('resume','conflict',config['repositories']['default'],{'resume_session_id':'expected'})
    manager.update_job_state(job_id,JobState.RUNNING,session_id='expected')
    state={'session_id':'expected'}
    executor._observe_stdout_event(job_id,b'{"type":"turn.failed","error":{"message":"failure"}}',state)
    executor._observe_stdout_event(job_id,b'{"type":"thread.started","thread_id":"other"}',state)
    receipt=JobManager(config).get_job(job_id).native_outcome
    assert receipt['kind']=='session_identity_conflict'
    assert receipt['prior_negative']['kind']=='turn.failed'


@pytest.mark.asyncio
async def test_live_resume_without_baseline_cannot_promote_recent_old_transcript(tmp_path,monkeypatch):
    config=make_config(tmp_path);manager=JobManager(config);executor=JobExecutor(config,manager)
    job_id=manager.create_job('resume','missing baseline',config['repositories']['default'],
        {'resume_session_id':'late-source'})
    source=Path(config['power_tools']['codex_home'])/'sessions'/'late-source.jsonl'
    content=json.dumps({'type':'session_meta','payload':{'id':'late-source'}})+'\n'+json.dumps(terminal('OLD REPORT'))+'\n'
    script=('from pathlib import Path\nimport time\n'+
        f'p=Path({str(source)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_text({content!r})\n'+
        'time.sleep(1.2)\n')
    monkeypatch.setattr(executor,'_build_codex_command',lambda *a:[sys.executable,'-u','-c',script])
    await executor.execute_job(job_id)
    job=manager.get_job(job_id)
    assert job.options['_session_baseline_proven'] is False
    assert job.terminal_source=='process_exit'
    assert job.result['native_finality']=='process_exit_only'
    assert 'OLD REPORT' not in job.result.get('summary','')


@pytest.mark.asyncio
async def test_hub_routes_exact_report_source_selector_and_returned_representation():
    from test_hub_worker_adapter_v2 import RecordingRuntime,make_adapter
    sha='a'*64;returned='b'*64
    runtime=RecordingRuntime(read_result={'view':'report_source','source_sha256':sha,
        'returned_source_sha256':returned,'redacted':True,'offset_basis':'returned_utf8',
        'report_offset':513,'next_offset':1026,'has_more':True,'text':'fixture page'})
    adapter,runtime,_,_=make_adapter(runtime=runtime)
    result=await adapter.handle_tool_call('patchbay_worker_inspect',{
        'work_group_id':'group_alpha','worker':'Implementer','view':'report_source',
        'report_source_sha256':sha,'report_offset':513,'max_bytes':513})
    arguments=runtime.read_calls[0]['payload']['arguments']
    assert arguments['report_source_sha256']==sha and arguments['report_offset']==513
    assert result['result']['returned_source_sha256']==returned
    assert result['result']['redacted'] and result['result']['next_offset']==1026
    from patchbay.protocol.result_text import decision_metadata
    compact=decision_metadata(result['result'])
    assert compact['source_sha256']==sha and compact['returned_source_sha256']==returned
    assert compact['redacted'] and compact['offset_basis']=='returned_utf8'
    report_summary=decision_metadata({'report_truncated':True,'report_source':{'sha256':sha}})
    assert report_summary['report_truncated'] and report_summary['report_source_sha256']==sha
