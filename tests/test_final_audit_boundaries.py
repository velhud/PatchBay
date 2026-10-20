"""Failure-path and scale checks for the consolidated audit repairs."""
import asyncio
import json

import pytest


def test_source_page_drains_oversized_line_with_bounded_memory_and_exact_cursor(tmp_path):
    import tracemalloc
    from test_audit_regressions_20261019 import configuration
    from patchbay.workspace.context import WorkspaceContext
    settings = configuration(tmp_path)
    path = tmp_path/'repo'/'huge.txt'
    with path.open('wb') as output:
        for _ in range(12):
            output.write(b'x'*1_000_000)
        output.write(b'\nnext line\n')
    context = WorkspaceContext(settings)
    tracemalloc.start()
    try:
        page = context.read_file({'file_path':'huge.txt', 'max_bytes':100})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000
    assert page['text'] == '' and page['next_start_line'] == 1
    assert page['required_bytes'] > 12_000_000
    following = context.read_file({'file_path':'huge.txt', 'start_line':2})
    assert 'next line' in following['text']
    path.write_text('changed\n')
    refreshed = context.read_file({'file_path':'huge.txt'})
    assert refreshed['sha256'] != page['sha256']


def test_secondary_projection_replays_accepted_metadata_after_restart(tmp_path):
    from test_hub_runtime_v2 import make_runtime
    from patchbay.hub.runtime_v2 import HubRuntimeV2, EDGE_PROJECTION_ENTITY
    from patchbay.hub.store_v2 import HubStoreV2
    runtime, store, path = make_runtime(tmp_path)
    code = runtime.create_enrollment_code(name='edge')['code']
    enrolled = runtime.enroll_machine(code=code, machine_id='edge')
    calls = []
    class Adapter:
        fail = True
        def handle_tool_call(self, *args, **kwargs):
            raise AssertionError('Replay must ingest metadata, never dispatch a tool')
        def ingest_projection(self, machine, projection, **kwargs):
            calls.append((projection, kwargs['projection_revision']))
            if self.fail:
                raise RuntimeError('injected secondary failure')
    adapter = Adapter()
    runtime.register_adapter('pro_requests', adapter)
    args = dict(machine_id='edge', token=enrolled['node_token'],
                edge_generation=enrolled['edge_generation'], projection_revision=1,
                worker_projection={'snapshot_kind':'full', 'workers':[], 'pro_requests':[
                    {'request_id':'proreq_fixture', 'revision':3, 'status':'open',
                     'report_markdown':'private evidence must not enter projection journal'}]})
    with pytest.raises(RuntimeError, match='secondary failure'):
        runtime.heartbeat(**args)
    pending = store.list_entities(EDGE_PROJECTION_ENTITY)[0]['record']
    assert pending['secondary_pending']
    assert 'private evidence' not in json.dumps(pending)
    store.close()
    store = HubStoreV2(path)
    runtime = HubRuntimeV2({}, store)
    adapter.fail = False
    runtime.register_adapter('pro_requests', adapter)
    repeated = runtime.heartbeat(**args)
    assert not repeated['projection_accepted']
    assert len(calls) == 2 and calls[0] == calls[1]
    assert not store.list_entities(EDGE_PROJECTION_ENTITY)[0]['record']['secondary_pending']
    store.close()


def test_pro_mirror_is_locked_sanitized_and_private_response_remains_complete(tmp_path):
    from test_pro_requests_store import init_repo, config
    from patchbay.pro_requests import ProRequestStore
    repo = init_repo(tmp_path/'repo')
    settings = config(tmp_path, repo)
    settings['pro_requests']['max_response_bytes'] = 40_000
    store = ProRequestStore(settings)
    report = tmp_path/'report.md'; report.write_text('Ordinary report')
    created = store.create_request(repo_path=str(repo), title='Mirror', report_path=str(report))
    response = 'GITHUB_TOKEN=fixture_private_value\n' + 'a'*16_000
    worker_message = 'b'*16_000
    with store._mirror_locks.hold_sync(str(repo), operation='exclusive_test'):
        result = store.respond_request(request_id=created['id'], response_kind='architecture_plan',
                                       response_markdown=response, worker_message_markdown=worker_message)
    assert result['mirror_effect']['state'] == 'deferred'
    private = store.dispatch_response_text(created['id'])
    assert private['response_markdown'] == response
    manifest, request_dir = store._load_request(created['id'])
    assert manifest['response']['worker_message_markdown'] == worker_message
    store._refresh_mirror(manifest, request_dir)
    mirrored = repo/'.ai-bridge'/'pro-requests'/created['id']/'response.md'
    assert 'fixture_private_value' not in mirrored.read_text()
    assert 'REDACTED' in mirrored.read_text()
    outside = tmp_path/'outside'; outside.write_text('unchanged')
    mirrored.unlink(); mirrored.symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        store._refresh_mirror(manifest, request_dir)
    assert outside.read_text() == 'unchanged'


@pytest.mark.asyncio
async def test_wait_reuses_snapshot_and_ignores_retained_terminal_history(tmp_path, monkeypatch):
    from test_hub_app_v2 import FakeEdgeDelivery, HubAppV2, HubWorkerProjectionPortV2
    app = HubAppV2(tmp_path/'hub.sqlite3', edge_delivery=FakeEdgeDelivery())
    port = HubWorkerProjectionPortV2(app.runtime, max_wait_seconds=.55, minimum_poll_seconds=.55)
    for index in range(500):
        operation = app.runtime.broker.create_operation(tool='fixture', logical_target='group_fixture',
                     idempotency_key=str(index), payload={})
        app.runtime._complete_hub_operation(operation, {'status':'ok', 'result':{}})
    assert app.store.active_operation_ids_for_work_group('group_fixture') == []
    calls = []
    original = port._projection_snapshot
    def snapshot(**kwargs):
        calls.append(True)
        return original(**kwargs)
    monkeypatch.setattr(port, '_projection_snapshot', snapshot)
    revision = app.store.work_group_status_revision('group_fixture')
    result = await port.wait(filters={'work_group_id':'group_fixture'}, route={}, since_revision=revision, timeout_seconds=.55)
    assert len(calls) == 1 and not result['changed']
    # The exact migration adds indexes used by the compact compatibility token.
    plan = app.store.connection.execute('EXPLAIN QUERY PLAN SELECT revision FROM operations WHERE logical_target=?', ('group_fixture',)).fetchall()
    assert 'operations_logical_target_idx' in str([tuple(row) for row in plan])
    app.store.close()


def test_filtered_worker_page_cannot_hide_active_group_work_or_mask_other_edge_changes(tmp_path):
    from test_hub_app_v2 import FakeEdgeDelivery, HubAppV2, HubWorkerProjectionPortV2
    from patchbay.hub.runtime_v2 import WORKER_PROJECTION_ENTITY
    app = HubAppV2(tmp_path/'hub.sqlite3', edge_delivery=FakeEdgeDelivery())
    port = HubWorkerProjectionPortV2(app.runtime)
    for ref, revision, state in [('large-edge',100,'completed'), ('small-edge',2,'working')]:
        app.store.put_entity(WORKER_PROJECTION_ENTITY, ref,
            {'fleet_worker_ref':ref, 'edge_projection_revision':revision, 'turn_state':state,
             'lane_id':ref, 'integration_state':'not_applicable'}, expected_revision=0)
    snapshot = port._projection_snapshot(filters={}, route={})
    snapshot['group'] = {'work_group_id':'group_fixture', 'status':'open', 'execution_mode':'end_to_end'}
    full = port._query_result(filters={}, route={}, snapshot=snapshot)
    filtered = port._query_result(filters={'lane':'large-edge'}, route={}, snapshot=snapshot)
    assert filtered['counts']['active'] == 0
    assert filtered['completion_contract'] == full['completion_contract']
    assert filtered['completion_contract']['reason'] == 'workers_or_operations_active'
    assert filtered['projection_revision'] == full['projection_revision']
    entity = app.store.get_entity(WORKER_PROJECTION_ENTITY, 'small-edge')
    app.store.put_entity(WORKER_PROJECTION_ENTITY, 'small-edge',
                        {**entity['record'], 'edge_projection_revision':3}, expected_revision=entity['revision'])
    advanced = port._projection_snapshot(filters={}, route={})
    assert advanced['snapshot_revision'] > snapshot['snapshot_revision']
    app.store.close()
