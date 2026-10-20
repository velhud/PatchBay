"""Large historical details cannot inflate a small recovery/status page."""
import json
import tracemalloc

from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.util import fingerprint


def test_recovery_summarizes_large_result_but_preserves_exact_detail_read(setup_author):
    s, _, _, task, _ = setup_author
    operation, _ = s.store.begin_operation(kind='fixture.result', task_id=task['id'], key='large-result',
        fingerprint=fingerprint({'fixture': 'large result'}), payload={})
    result = {'task_id': task['id'], 'revision': 7, 'blob_sha': 'a' * 64, 'large_detail': 'x' * (5 * 1024**2)}
    s.store.finish(operation['id'], 'completed', result=result)
    before = s.store.one('SELECT COUNT(*) AS n FROM events')['n']
    tracemalloc.start()
    try:
        recovery = s.recovery.get(task['id'])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024**2
    page = s.recovery.operations(task['id'])
    for output in (recovery['operations'], page['operations']):
        observed = next(row for row in output if row['id'] == operation['id'])
        assert observed['result_partial'] and observed['result_bytes'] > 100000
        assert observed['result']['blob_sha'] == result['blob_sha']
        assert observed['result']['revision'] == 7
        assert observed['detail_read']['arguments']['operation_id'] == operation['id']
    assert len(json.dumps(recovery)) < 20000
    assert s.store.operation(operation['id'])['result'] == result
    assert s.store.one('SELECT COUNT(*) AS n FROM events')['n'] == before


def test_task_list_marks_goal_snippets_and_exact_task_read_is_complete(setup_author):
    s, _, repo, _, _ = setup_author
    goal = 'Unicode source intent λ ' * 5000
    task = s.tasks.create(repo['id'], goal, key='large-goal')
    row = next(row for row in s.tasks.list()['tasks'] if row['id'] == task['id'])
    assert row['goal'] == goal[:2048] and row['goal_truncated']
    assert row['goal_bytes'] == len(goal.encode())
    assert s.tasks.get(task['id'])['goal'] == goal


def test_mcp_wire_budget_is_explicit_instead_of_returning_an_oversized_frame(setup_author):
    s, _, _, task, _ = setup_author
    op, _ = s.store.begin_operation(kind='fixture.result', task_id=task['id'], key='wire-budget',
        fingerprint=fingerprint({'fixture': 'wire'}), payload={})
    s.store.finish(op['id'], 'completed', result={'text': 'x' * (5 * 1024**2)})
    response = MCP(s).handle('reader', {'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'author_operation_status','arguments':{'operation_id':op['id']}}})['result']
    assert response['isError'] and response['structuredContent']['code'] == 'RESOURCE_LIMIT'
    assert response['structuredContent']['details']['result_identity']['id'] == op['id']
    assert response['structuredContent']['details']['effect_returned'] is False
    assert len(json.dumps(response)) < 2000
