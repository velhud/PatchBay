"""Post-admission response faults on real disposable Author state."""
import json

import pytest

from test_controller_contracts import qualified
from test_s01_interface_contracts import request
from patchbay.authoring.revisions.drafts import Drafts
from patchbay.authoring.revisions.git_objects import Git
from patchbay.authoring.errors import AuthorError
from test_core import package_for, submit


def test_encoding_failure_preserves_trusted_admission_and_original_key(setup_author, monkeypatch):
    service, _, repo, _, _ = setup_author
    protocol = qualified(service)
    original = service.tasks.create
    admitted = []
    def broken_presentation(*args, **kwargs):
        task = original(*args, **kwargs)
        admitted.append(task['id'])
        return {**task, 'goal': '\ud800'}
    monkeypatch.setattr(service.tasks, 'create', broken_presentation)
    args = {'repository': repo['id'], 'goal': 'Encoding fixture', 'key': 'encoding-key'}
    before = service.store.one('SELECT COUNT(*) AS n FROM tasks')['n']
    response = request(protocol, 'author_create_task', args)
    assert response['isError']
    fault = response['structuredContent']
    assert fault['code'] == 'INVALID_OUTPUT'
    assert fault['details']['effect_may_exist'] and not fault['details']['success_asserted']
    assert fault['details']['task_id'] == admitted[0]
    assert fault['details']['original_key'] == args['key']
    json.dumps(response, ensure_ascii=False).encode('utf-8')
    monkeypatch.setattr(service.tasks, 'create', original)
    replay = request(qualified(service), 'author_create_task', args)
    assert not replay['isError'] and replay['structuredContent']['id'] == admitted[0]
    assert service.store.one('SELECT COUNT(*) AS n FROM tasks')['n'] == before + 1


def test_domain_exception_after_task_commit_keeps_original_admission_discoverable(setup_author, monkeypatch):
    service, _, repo, _, _ = setup_author
    protocol = qualified(service)
    original = service.tasks.create
    admitted = []
    def lost_response(*args, **kwargs):
        admitted.append(original(*args, **kwargs)['id'])
        raise RuntimeError('private-exception-not-controller-data')
    monkeypatch.setattr(service.tasks, 'create', lost_response)
    args = {'repository': repo['id'], 'goal': 'Durable before reply', 'key': 'lost-domain-response'}
    response = request(protocol, 'author_create_task', args)
    assert response['isError'] and response['structuredContent']['code'] == 'EFFECT_OUTCOME_UNAVAILABLE'
    details = response['structuredContent']['details']
    assert details['task_id'] == admitted[0] and details['effect_may_exist']
    assert not details['effect_returned'] and not details['success_asserted']
    assert 'private-exception-not-controller-data' not in json.dumps(response)
    monkeypatch.setattr(service.tasks, 'create', original)
    replay = request(qualified(service), 'author_create_task', args)
    assert replay['structuredContent']['id'] == admitted[0]


@pytest.mark.parametrize('request_id', [None, True, [], {}])
def test_invalid_mcp_ids_cannot_admit_author_effect(setup_author, request_id):
    service, _, repo, _, _ = setup_author
    protocol = qualified(service)
    before = service.store.one('SELECT COUNT(*) AS n FROM tasks')['n']
    response = protocol.handle('author', {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
        'params': {'name': 'author_create_task', 'arguments': {'repository': repo['id'],
        'goal': 'Must not admit', 'key': 'invalid-id'}}})
    assert response['error']['code'] == -32600
    assert service.store.one('SELECT COUNT(*) AS n FROM tasks')['n'] == before


def test_seal_output_fault_then_fresh_controller_replays_exact_candidate(setup_author, monkeypatch):
    service, _, _, task, lease = setup_author
    protocol = qualified(service)
    authority = {'token': lease['token'], 'epoch': lease['epoch']}
    draft = Drafts(service.store).apply(task['id'], expected_revision=0, expected_version=0,
        edits=[{'op': 'create', 'path': 'delivered.txt', 'content': 'retained source'}], key='draft', **authority)
    original = Drafts.seal
    def bad_result(self, *values, **options):
        return {**original(self, *values, **options), 'revision': 'invalid'}
    monkeypatch.setattr(Drafts, 'seal', bad_result)
    args = {'draft_id': draft['draft_id'], 'expected_version': 1, 'key': 'sealed-intent', **authority}
    response = request(protocol, 'author_seal_draft', args)
    details = response['structuredContent']['details']
    assert response['isError'] and details['task_id'] == task['id']
    assert details['operation_id'] and details['original_key'] == args['key']
    assert service.tasks.get(task['id'])['current_revision'] == 1
    monkeypatch.setattr(Drafts, 'seal', original)
    replay = request(qualified(service), 'author_seal_draft', args)
    assert not replay['isError'] and replay['structuredContent']['revision'] == 1
    assert service.tasks.get(task['id'])['current_revision'] == 1
    changed = request(protocol, 'author_seal_draft', {**args, 'expected_version': 2})
    assert changed['isError'] and changed['structuredContent']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_lease_expiry_at_final_git_acceptance_rejects_old_writer_and_allows_explicit_successor(setup_author, tmp_path, monkeypatch):
    service, _, _, task, old = setup_author
    package = package_for(service, task, tmp_path, {'delayed.txt': 'exact retained intent'})
    original = Git.create_candidate
    def expires_after_git(self, *values, **options):
        result = original(self, *values, **options)
        with service.store.transaction() as conn:
            conn.execute('UPDATE leases SET expires=0 WHERE task_id=?', (task['id'],))
        return result
    monkeypatch.setattr(Git, 'create_candidate', expires_after_git)
    with pytest.raises(AuthorError) as expired:
        submit(service, task, package, old, 'expired-writer')
    assert expired.value.code == 'LEASE_LOST'
    assert service.tasks.get(task['id'])['current_revision'] == 0
    monkeypatch.setattr(Git, 'create_candidate', original)
    successor = service.leases.claim(task['id'], takeover=True, key='explicit-operator-successor')
    accepted = submit(service, task, package, successor, 'deliberate-new-admission')
    assert accepted['revision'] == 1
    with pytest.raises(AuthorError) as old_writer:
        submit(service, task, package, old, 'old-delayed-write')
    assert old_writer.value.code == 'LEASE_LOST'
    capsule = service.recovery.get(task['id'])
    assert old['token'] not in json.dumps(capsule) and successor['token'] not in json.dumps(capsule)
