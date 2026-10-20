"""Real portable controller transitions, without pretending to run Linux checks."""
from __future__ import annotations

import json
import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.service import AuthorService
from patchbay.authoring.persistence.control import Control
from conftest import qualification_profile
from test_core import package_for, submit


def qualified(s, role='author'):
    Access(s.store).qualify({**qualification_profile(), 'role': role, 'output_profile': 'structured-summary-v2',
        'model': 'fixture-model', 'client': 'fixture-only', 'ordinary_chat': True, 'write_supported': True,
        'probe_description': 'Synthetic in-process test; no actual Chat trial', 'observed_at': 'fixture'},
        actor='fixture-operator', key='qualification-'+role)
    return MCP(s, output_profile='structured-summary-v2')


def call(protocol, name, values, role='author'):
    result = protocol.handle(role, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': values}})['result']
    assert not result['isError'], result
    assert len(result['content'][0]['text']) < 1500
    return result['structuredContent']


def test_complete_portable_mcp_authoring_and_fresh_controller_recovery(setup_author):
    s, _, repo, _, _ = setup_author
    p = qualified(s)
    task = call(p, 'author_create_task', {'repository': repo['id'], 'goal': 'Implement actual code', 'key': 'mcp-task'})
    task_id = task['id']
    lease = call(p, 'author_claim_task', {'task_id': task_id, 'key': 'claim'})
    authority = {'token': lease['token'], 'epoch': lease['epoch']}
    context = call(p, 'author_export_context', {'task_id': task_id, 'key': 'context'})
    source = call(p, 'author_read_file', {'context_id': context['context_id'], 'path': 'app.py'})
    assert 'a - b' in source['text']
    draft = call(p, 'author_apply_edits', {'task_id': task_id, **authority, 'key': 'draft',
        'expected_revision': 0, 'expected_version': 0, 'edits': [{'op': 'replace', 'path': 'app.py',
        'before_sha256': source['sha256'], 'content': 'def add(a,b):\n    return a+b\n'}]})
    read = call(p, 'author_read_file', {'draft_id': draft['draft_id'], 'draft_version': 1, 'path': 'app.py'})
    assert 'a+b' in read['text']
    accepted = call(p, 'author_seal_draft', {'draft_id': draft['draft_id'], 'expected_version': 1, 'key': 'seal', **authority})
    assert accepted['revision'] == 1
    feedback = call(p, 'author_get_feedback', {'task_id': task_id})
    assert feedback['classification'] == 'CHECKS_INCOMPLETE'
    exported = call(p, 'author_export_feedback', {'task_id': task_id, 'key': 'feedback'})
    assert s.store.blobs.verify(exported['blob_sha'])
    fresh = MCP(AuthorService(s.store.home), output_profile='structured-summary-v2')
    before = s.store.one('SELECT COUNT(*) AS n FROM events')['n']
    recovered = call(fresh, 'author_recover_task', {'task_id': task_id}, role='reader')
    recent = call(fresh, 'author_recent_operations', {'task_id': task_id}, role='reader')
    assert recovered['candidate']['commit'] == accepted['commit']
    assert recovered['state'] == 'awaiting_input'
    assert recent['total'] >= 5 and recovered['freshness']['consistent_snapshot']
    assert s.store.one('SELECT COUNT(*) AS n FROM events')['n'] == before
    assert lease['token'] not in json.dumps(recovered) + json.dumps(recent)


def test_catalog_has_explicit_validated_output_schemas_and_role_boundary(setup_author):
    s, _, _, task, lease = setup_author
    p = qualified(s)
    for tool in p.tools('reader') + p.tools('author'):
        schema = tool['outputSchema']
        assert schema['type'] == 'object' and schema['properties'] and schema['required']
        assert schema['additionalProperties'] is False
        assert tool['_meta']['patchbay/catalog']['version'] == 'author-v2'
    assert 'author_publish' not in {t['name'] for t in p.tools('author')}
    with pytest.raises(AuthorError) as error:
        p.call('author', 'author_approve_publication', {'task_id': task['id'], 'revision': 0,
            'actor': 'forged', 'key': 'forged', 'token': lease['token'], 'epoch': lease['epoch']})
    assert error.value.code == 'FORBIDDEN'


def test_stale_catalog_qualification_cannot_enable_writes(setup_author, monkeypatch):
    s, _, _, _, _ = setup_author
    p = qualified(s)
    record = Access(s.store).qualified()
    assert record and record['binding']['runtime_sha256']
    from patchbay.authoring.interfaces import auth
    changed = {**record['binding'], 'sha256': '0'*64}
    monkeypatch.setattr(auth, 'catalog_identity', lambda *args: changed)
    assert Access(s.store).qualified() is None
    assert all(t['annotations']['readOnlyHint'] for t in p.tools('author'))


def test_lease_and_context_response_loss_replay_are_stable_and_private(setup_author, tmp_path):
    s, _, _, task, old = setup_author
    lease = s.leases.claim(task['id'], token=old['token'], takeover=True, key='takeover')
    again = s.leases.claim(task['id'], token=old['token'], takeover=True, key='takeover')
    assert again == lease
    assert 'token' not in s.store.operation(lease['request_operation_id'])['result']
    context = s.snapshots.export(task['id'], key='pinned-context')
    accepted = submit(s, task, package_for(s, task, tmp_path, {'new.txt': 'source'}), lease)
    replay = AuthorService(s.store.home).snapshots.export(task['id'], key='pinned-context')
    assert replay == context and replay['revision'] == 0 and accepted['revision'] == 1
    with pytest.raises(AuthorError) as error:
        s.snapshots.export(task['id'], paths=['app.py'], key='pinned-context')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


def test_new_admissions_and_revoked_successors_fail_but_recovery_reads_remain(setup_author, tmp_path):
    s, _, repo, task, lease = setup_author
    package = package_for(s, task, tmp_path, {'safe.txt': 'source'})
    control = Control(s.store)
    control.pause(key='pause', actor='operator')
    with pytest.raises(AuthorError) as error:
        s.tasks.create(repo['id'], 'New admission blocked', key='denied')
    assert error.value.code == 'CONTROL_PAUSED'
    assert s.recovery.get(task['id'])['candidate']['revision'] == 0
    control.resume(key='resume', actor='operator')
    control.revoke(task['id'], key='revoke', actor='operator')
    with pytest.raises(AuthorError) as error:
        submit(s, task, package, lease)
    assert error.value.code == 'TASK_REVOKED'
    assert s.tasks.get(task['id'])['current_revision'] == 0
    assert s.recovery.get(task['id'])['state'] == 'awaiting_approval'


def test_versioned_task_page_rejects_state_changes(setup_author):
    s, _, repo, _, _ = setup_author
    page = s.tasks.list(limit=1)
    s.tasks.create(repo['id'], 'New task between pages', key='between')
    with pytest.raises(AuthorError) as error:
        s.tasks.list(offset=1, cursor=page['cursor'])
    assert error.value.code == 'STALE_CURSOR'
