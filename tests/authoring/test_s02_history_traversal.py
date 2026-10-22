"""Stable history identities without freezing current execution authority."""
import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.recovery import Recovery
from patchbay.authoring.service import AuthorService


def test_strict_history_restarts_on_same_task_changes_but_not_unrelated_activity(setup_author):
    s, _, repo, task, _ = setup_author
    unrelated = s.tasks.create(repo['id'], 'Unrelated', key='other-task')
    first = s.recovery.operations(task['id'], limit=1)
    s.store.begin_operation(kind='fixture', task_id=unrelated['id'], key='other', fingerprint='other', payload={})
    assert s.recovery.operations(task['id'], limit=1, cursor=first['cursor'])['cursor'] == first['cursor']
    restarts = 0
    for number in range(3):
        page = s.recovery.operations(task['id'], limit=1)
        s.store.begin_operation(kind='fixture', task_id=task['id'], key=str(number), fingerprint=str(number), payload={})
        with pytest.raises(AuthorError) as stale:
            s.recovery.operations(task['id'], limit=1, offset=1, cursor=page['cursor'])
        assert stale.value.code == 'STALE_CURSOR'
        restarts += 1
    assert restarts == 3


def test_snapshot_traverses_every_initial_identity_under_same_and_unrelated_activity(setup_author):
    s, _, repo, task, _ = setup_author
    unrelated = s.tasks.create(repo['id'], 'Other history', key='other-history')
    for number in range(245):
        op, _ = s.store.begin_operation(kind='fixture', task_id=task['id'], key='seed-'+str(number),
            fingerprint=str(number), payload={})
        with s.store.transaction() as conn:
            conn.execute("UPDATE operations SET state='unknown_execution',phase='fixture' WHERE id=?", (op['id'],))
    recovered = s.recovery.get(task['id'])
    assert recovered['active_operations_partial'] and recovered['state'] == 'awaiting_controller'
    expected = {row['id'] for row in s.store.rows('SELECT id FROM operations WHERE task_id=?', (task['id'],))}
    page = s.recovery.operations(task['id'], limit=47, traversal='snapshot')
    seen, calls = [], 0
    while True:
        calls += 1
        seen.extend(row['id'] for row in page['operations'])
        assert page['traversal'] == 'snapshot' and page['total'] == len(expected)
        if page['next_offset'] is None:
            break
        new, _ = s.store.begin_operation(kind='fixture', task_id=task['id'], key='during-'+str(calls),
            fingerprint=str(calls), payload={})
        # A backdated insertion must not enter the initial membership either.
        with s.store.transaction() as conn:
            conn.execute('UPDATE operations SET created=0 WHERE id=?', (new['id'],))
            conn.execute("UPDATE operations SET phase='changed' WHERE task_id=? AND state='unknown_execution'", (task['id'],))
        s.store.begin_operation(kind='fixture', task_id=unrelated['id'], key=str(calls), fingerprint=str(calls), payload={})
        page = AuthorService(s.store.home).recovery.operations(task['id'], limit=47, offset=page['next_offset'],
            cursor=page['cursor'], traversal='snapshot')
    assert set(seen) == expected and len(seen) == len(set(seen)) and calls == 6
    with pytest.raises(AuthorError):
        s.recovery.operations(unrelated['id'], cursor=page['cursor'], traversal='snapshot')
    assert s.recovery.get(task['id'])['state'] == 'awaiting_controller'
