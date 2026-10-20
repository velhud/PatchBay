"""Authenticated production HTTP/MCP boundaries with disposable local state."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.http import create_app
from conftest import qualification_profile


def test_task_scoped_reader_cannot_cross_task_or_artifact_boundaries(setup_author):
    s, _, repo, task, _ = setup_author
    other = s.tasks.create(repo['id'], 'Separate private task', key='separate')
    own_context = s.snapshots.export(task['id'])
    other_context = s.snapshots.export(other['id'])
    issued = Access(s.store).issue('scoped-reader', 'reader', [task['id']], key='reader-token')
    headers = {'Authorization': 'Bearer ' + issued['token']}
    with TestClient(create_app(s.store.home)) as client:
        tasks = client.get('/api/tasks', headers=headers)
        assert tasks.status_code == 200
        assert [row['id'] for row in tasks.json()['tasks']] == [task['id']]
        assert client.get('/api/tasks/' + other['id'], headers=headers).status_code == 403
        assert client.get('/api/tasks/' + other['id'] + '/recovery', headers=headers).status_code == 403
        assert client.get('/api/blobs/' + own_context['bundle_sha'], headers=headers).status_code == 200
        assert client.get('/api/blobs/' + other_context['bundle_sha'], headers=headers).status_code == 403
        assert client.get('/api/operations/' + other_context['request_operation_id'], headers=headers).status_code == 403
        result = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'author_read_file', 'arguments': {'context_id': other_context['id'], 'path': 'app.py'}}}).json()['result']
        assert result['isError'] and result['structuredContent']['code'] == 'FORBIDDEN'
        events = client.get('/api/events', headers=headers).json()['events']
        assert all(event['task_id'] == task['id'] for event in events)
        assert client.post('/api/context', headers=headers, json={'task_id': task['id'], 'key': 'reader-write'}).status_code == 403
    author = Access(s.store).issue('scoped-write-denial', 'author', [task['id']], key='denial-author')
    before = s.store.one('SELECT COUNT(*) AS n FROM operations')['n']
    with TestClient(create_app(s.store.home)) as client:
        denied = client.post('/api/context', headers={'Authorization': 'Bearer ' + author['token']},
                             json={'task_id': other['id'], 'key': 'forbidden-context'})
        assert denied.status_code == 403
    assert s.store.one('SELECT COUNT(*) AS n FROM operations')['n'] == before


def test_named_author_mcp_draft_loop_and_lost_response_keys_cross_http_boundary(setup_author):
    s, _, _, task, _ = setup_author
    issued = Access(s.store).issue('scoped-author', 'author', [task['id']], key='author-token')
    headers = {'Authorization': 'Bearer ' + issued['token'], 'X-Request-ID': 'portable-author-request-1'}
    Access(s.store).qualify({**qualification_profile(), 'principal_id': 'scoped-author',
        'model': 'synthetic', 'client': 'in-process-fixture', 'ordinary_chat': True, 'write_supported': True,
        'probe_description': 'Synthetic contract fixture, no actual Chat observation', 'observed_at': 'fixture'},
        actor='fixture-operator', key='qualified-fixture')
    with s.store.transaction() as conn:
        conn.execute('UPDATE leases SET expires=0 WHERE task_id=?', (task['id'],))
    with TestClient(create_app(s.store.home)) as client:
        def call(name, arguments):
            response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}})
            assert response.headers['X-Request-ID'] == headers['X-Request-ID']
            result = response.json()['result']
            assert not result['isError'], result
            return result['structuredContent']
        lease = call('author_claim_task', {'task_id': task['id'], 'key': 'claim'})
        assert call('author_claim_task', {'task_id': task['id'], 'key': 'claim'}) == lease
        operation = call('author_operation_status', {'operation_id': lease['request_operation_id']})
        assert operation['request_id'] == headers['X-Request-ID']
        assert issued['token'] not in json.dumps(operation) and lease['token'] not in json.dumps(operation)
        authority = {'token': lease['token'], 'epoch': lease['epoch']}
        context = call('author_export_context', {'task_id': task['id'], 'key': 'context'})
        source = call('author_read_file', {'context_id': context['context_id'], 'path': 'app.py'})
        draft = call('author_apply_edits', {'task_id': task['id'], **authority, 'key': 'draft', 'expected_revision': 0,
            'expected_version': 0, 'edits': [{'op': 'replace', 'path': 'app.py', 'before_sha256': source['sha256'],
            'content': 'def add(a,b): return a+b\n'}]})
        sealed = call('author_seal_draft', {'draft_id': draft['draft_id'], 'expected_version': 1, **authority, 'key': 'seal'})
        assert sealed['revision'] == 1
        assert call('author_seal_draft', {'draft_id': draft['draft_id'], 'expected_version': 1, **authority, 'key': 'seal'}) == sealed
        assert call('author_recover_task', {'task_id': task['id']})['candidate']['commit'] == sealed['commit']


def test_maintenance_and_registration_request_replays_are_stable(setup_author, tmp_path):
    from patchbay.authoring.persistence.maintenance import Maintenance
    s, source, repo, _, _ = setup_author
    first = s.repositories.register('fixture', source, repo['policy'], key='registration')
    assert s.repositories.register('fixture', source, repo['policy'], key='registration') == first
    maintenance = Maintenance(s.store)
    backup = maintenance.backup(tmp_path / 'backup.zip', key='backup')
    archive = (tmp_path / 'backup.zip').read_bytes()
    assert maintenance.backup(tmp_path / 'backup.zip', key='backup') == backup
    assert (tmp_path / 'backup.zip').read_bytes() == archive
    gc = maintenance.gc(minimum_age_seconds=60, key='gc')
    assert maintenance.gc(minimum_age_seconds=60, key='gc') == gc


def test_operator_can_recover_original_principal_intent_after_explicit_takeover(setup_author, tmp_path, monkeypatch):
    from patchbay.authoring.authority import identity_context, request_context
    from test_core import package_for
    s, _, _, task, _ = setup_author
    package = package_for(s, task, tmp_path, {'recovered.txt': 'frozen intended bytes'})
    access = Access(s.store)
    alice = access.authenticate('Bearer ' + access.issue('author-a', 'author', [task['id']], key='alice')['token'])
    bob = access.authenticate('Bearer ' + access.issue('operator-b', 'operator', [task['id']], key='bob')['token'])
    with s.store.transaction() as conn:
        conn.execute('UPDATE leases SET expires=0 WHERE task_id=?', (task['id'],))
    accept = s.materializer._accept
    def interrupted(*args, **kwargs):
        raise OSError('fixture interrupted after retained candidate')
    monkeypatch.setattr(s.materializer, '_accept', interrupted)
    import pytest
    with identity_context(alice), request_context('original-package-request'):
        lease = s.leases.claim(task['id'], key='lease')
        with pytest.raises(OSError):
            s.materializer.submit(task['id'], package, key='package', token=lease['token'], epoch=lease['epoch'])
    operation = s.store.one("SELECT id FROM operations WHERE task_id=? AND kind='package.submit'", (task['id'],))['id']
    monkeypatch.setattr(s.materializer, '_accept', accept)
    with identity_context(bob):
        successor = s.leases.claim(task['id'], takeover=True, key='takeover')
        result = s.materializer.recover(operation, token=successor['token'], epoch=successor['epoch'])
        assert result['revision'] == 1
        assert s.store.operation(operation)['request_id'] == 'original-package-request'
    assert s.store.one("SELECT COUNT(*) AS n FROM operations WHERE kind='package.submit'")['n'] == 1


def test_folder_scanner_uses_only_the_previously_granted_principal_capability(setup_author, tmp_path):
    from patchbay.authoring.authority import identity_context
    from test_core import package_for
    s, _, _, task, _ = setup_author
    package = package_for(s, task, tmp_path, {'autoload.txt': 'explicit grant'})
    access = Access(s.store)
    author = access.authenticate('Bearer ' + access.issue('inbox-author', 'author', [task['id']], key='inbox-author')['token'])
    with s.store.transaction() as conn:
        conn.execute('UPDATE leases SET expires=0 WHERE task_id=?', (task['id'],))
    with identity_context(author):
        lease = s.leases.claim(task['id'], key='grant-lease')
        s.inbox.grant(task['id'], lease['token'], lease['epoch'], auto_validate=True, key='auto-grant')
    result = s.inbox.ingest(package)
    assert result['state'] == 'completed' and result['result']['revision'] == 1
    assert s.tasks.get(task['id'])['current_revision'] == 1
