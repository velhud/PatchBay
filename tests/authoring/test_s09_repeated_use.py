"""Offline fairness, complete streaming and retained-evidence inverses."""
import hashlib
import io
import json
import sys
import zipfile
import pytest

from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.persistence.admission_indexes import install
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.revisions.git_objects import Git


@pytest.mark.parametrize('equal_created', [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux/WSL2 execution admission")
def test_automatic_queue_passes_old_blockers_without_targeted_retry(setup_author, monkeypatch, equal_created):
    service, source, repo, task, lease = setup_author
    checks = Checks(service.store)
    ids = [checks.submit(task['id'], revision=0, key=f'pending-{i}', token=lease['token'],
                         epoch=lease['epoch'], dispatch=False)['id'] for i in range(101)]
    with service.store.transaction() as conn:
        for i, op in enumerate(ids):
            conn.execute('UPDATE runs SET created=? WHERE id=?', (0 if equal_created else i, op))
    ordered = service.store.rows('SELECT id FROM runs ORDER BY created,id')
    younger = ordered[-1]['id']
    calls = []
    monkeypatch.setattr('patchbay.authoring.execution.runner.can_admit', lambda *a: (False, {'gates':{'fixture':False}}))
    first = checks.dispatch()
    assert len(first['waiting']) == 100 and younger not in {r['operation_id'] for r in first['waiting']}
    # Reopening the scheduler keeps its order hint; it does not need resubmission.
    reopened = Checks(Store(service.store.home))
    def launch(*args, **kw):
        calls.append(args[0]); raise OSError('recording launch refusal; no process started')
    monkeypatch.setattr('patchbay.authoring.execution.runner.can_admit', lambda *a: (True, {}))
    monkeypatch.setattr('patchbay.authoring.execution.runner.subprocess.Popen', launch)
    reopened.dispatch()
    assert len(calls) == 1 and younger in calls[0]
    assert service.store.operation(younger)['state'] == 'blocked'
    monkeypatch.setattr('patchbay.authoring.execution.runner.can_admit', lambda *a: (False, {'gates':{'fixture':False}}))
    # Cursor wraps and cancellation remains authoritative.
    cancelled = ordered[0]['id']
    checks.cancel(cancelled, token=lease['token'], epoch=lease['epoch'])
    wrapped = reopened.dispatch()
    assert len(wrapped['waiting']) == 99 and cancelled not in {r['operation_id'] for r in wrapped['waiting']}


def test_index_upgrade_is_explicit_and_preserves_old_schema_state(setup_author):
    service, _, _, task, _ = setup_author
    with service.store.transaction() as conn:
        conn.execute('DROP INDEX runs_queue_window')
        conn.execute('DROP INDEX runs_profile_receipts')
    reopened = Store(service.store.home)
    assert not reopened.rows("SELECT name FROM sqlite_master WHERE name='runs_queue_window'")
    before = reopened.one('SELECT * FROM tasks WHERE id=?', (task['id'],))
    assert install(reopened)['installed'] and install(reopened)['installed']
    assert reopened.one('SELECT * FROM tasks WHERE id=?', (task['id'],)) == before
    plans = reopened.rows("EXPLAIN QUERY PLAN SELECT id FROM runs WHERE state='queued' AND (created,id)>(?,?) ORDER BY created,id LIMIT 100", (0,''))
    assert any('runs_queue_window' in r['detail'] for r in plans)
    plans = reopened.rows('EXPLAIN QUERY PLAN SELECT receipt_sha FROM runs WHERE profile_digest=? AND receipt_sha IS NOT NULL ORDER BY updated DESC LIMIT 20', ('p',))
    assert any('runs_profile_receipts' in r['detail'] for r in plans)
    assert not any('TEMP B-TREE' in r['detail'] for r in plans)


def test_context_and_feedback_stream_complete_verified_members(setup_author, monkeypatch):
    service, _, _, task, _ = setup_author
    def whole_read_forbidden(*a, **kw):
        raise AssertionError('Whole source/blob materialization is forbidden in this export')
    monkeypatch.setattr(Git, 'blob', whole_read_forbidden)
    context = service.snapshots.export(task['id'])
    with service.store.blobs.open_verified(context['bundle_sha']) as stream:
        raw = stream.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        inventory = json.loads(archive.read('INVENTORY.json'))
        for name, item in inventory['files'].items():
            if item['included']:
                data = archive.read('source/'+name)
                assert len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256']
    monkeypatch.setattr(service.store.blobs, 'read', whole_read_forbidden)
    from patchbay.authoring.feedback.export import Feedback
    feedback = Feedback(service.store).export(task['id'])
    with service.store.blobs.open_verified(feedback['blob_sha']) as stream, zipfile.ZipFile(stream) as archive:
        assert archive.read('task-context.zip') == raw
    assert service.snapshots.export(task['id'])['bundle_sha'] == context['bundle_sha']


def test_failed_stream_publishes_no_context_or_partial_zip(setup_author, monkeypatch):
    from contextlib import contextmanager
    service, _, _, task, _ = setup_author
    @contextmanager
    def failed(*a, **kw):
        class Broken:
            def read(self, size):
                assert size == 65536
                raise OSError('injected producer failure')
        yield Broken()
    monkeypatch.setattr(Git, 'open_blob', failed)
    with pytest.raises(OSError, match='injected'):
        service.snapshots.export(task['id'])
    assert not service.store.rows('SELECT * FROM contexts')
    assert not list((service.store.home/'staging').iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux/WSL2 execution admission")
def test_noop_gc_skips_receipt_but_existing_workspace_requires_integrity(setup_author,monkeypatch):
    from patchbay.authoring.errors import AuthorError
    service, _, _, task, lease = setup_author
    op=Checks(service.store).submit(task['id'],revision=0,key='gc-run',token=lease['token'],epoch=lease['epoch'],dispatch=False)
    sha=service.store.blobs.put(b'retained opaque evidence')
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='completed',cleanup='proven',receipt_sha=?,updated=0 WHERE id=?",(sha,op['id']))
        conn.execute("UPDATE operations SET state='completed' WHERE id=?",(op['id'],))
        service.store.reference(conn,'run:'+op['id'],sha)
    seen=[]
    def corrupt(sha):seen.append(sha);return False
    monkeypatch.setattr(service.store.blobs,'verify',corrupt)
    Maintenance(service.store).gc(minimum_age_seconds=60,key='absent-run')
    assert seen==[]
    root=service.store.home/'runs'/op['id'];root.mkdir()
    with pytest.raises(AuthorError,match='retained'):
        Maintenance(service.store).gc(minimum_age_seconds=60,key='present-run')
    assert seen==[sha] and root.exists()
