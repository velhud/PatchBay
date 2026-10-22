"""Regression coverage for the read-only trial findings and fresh author loop."""
import json
import sys
import zipfile

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.receipts import record_receipt
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.policy import Policies
from patchbay.authoring.publication.git import Publisher
from patchbay.authoring.service import AuthorService
from test_controller_contracts import qualified
from test_core import package_for, submit
from test_execution import wait_for
from test_fault_boundaries import _seed_check_run, _synthetic_receipt


def call(protocol, name, arguments, role='author'):
    result = protocol.handle(role, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments}})['result']
    assert not result['isError'], result
    return result['structuredContent']


def update_policy(service, task_id, policy, key):
    return Policies(service.store).update(task_id, policy,
        expected_digest=service.tasks.get(task_id)['policy_digest'], actor='fixture-operator', key=key)


def test_recovery_distinguishes_source_from_candidate_without_creating_context(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    baseline = s.snapshots.export(task['id'])
    accepted = submit(s, task, package_for(s, task, tmp_path, {'app.py': 'def add(a,b):\n    return a+b\n'}), lease)
    fresh = AuthorService(s.store.home)
    before = s.store.one('SELECT COUNT(*) AS n FROM events')['n']
    candidate = call(MCP(fresh), 'author_recover_task', {'task_id': task['id']}, role='reader')['candidate']
    assert candidate['commit'] == accepted['commit']
    assert candidate['source_context_id'] == candidate['context_id']
    assert candidate['source_context']['revision'] == 0
    assert candidate['source_context']['commit_oid'] == baseline['commit_oid']
    assert candidate['candidate_context_id'] is None and candidate['candidate_context'] is None
    assert s.store.one('SELECT COUNT(*) AS n FROM events')['n'] == before
    current = s.snapshots.export(task['id'], paths=['app.py'])
    candidate = fresh.recovery.get(task['id'])['candidate']
    assert candidate['source_context_id'] != candidate['candidate_context_id']
    assert candidate['candidate_context_id'] == current['id']
    assert candidate['candidate_context']['revision'] == 1
    assert candidate['candidate_context']['commit_oid'] == accepted['commit']
    for field, expected in [('source_context_id', 'a - b'), ('candidate_context_id', 'a+b')]:
        read = call(MCP(fresh), 'author_read_file', {'context_id': candidate[field], 'path': 'app.py'}, role='reader')
        assert expected in read['text']


@pytest.mark.parametrize('publication', [False, True])
def test_passed_feedback_review_is_policy_aware_on_all_exports(setup_author, publication):
    s, _, _, task, _ = setup_author
    if publication:
        policy = s.tasks.get(task['id'])['policy']
        policy['publication']['enabled'] = True
        policy['publication']['remote_effects'] = 'controlled_no_automation'
        update_policy(s, task['id'], policy, 'enable-publication')
    run, current = _seed_check_run(s, task, key='synthetic-review')
    # Persistence contract fixture only, never execution qualification.
    record_receipt(s.store, run['id'], _synthetic_receipt(s, current, run, passed=True))
    feedback = Feedback(s.store).get(task['id'])
    recovered = call(MCP(s), 'author_recover_task', {'task_id': task['id']}, role='reader')
    assert recovered['state'] == ('awaiting_approval' if publication else 'awaiting_review')
    assert ('publication is disabled' in feedback['next_action']) is (not publication)
    assert ('publication is disabled' in recovered['required_judgment']) is (not publication)
    exported = Feedback(s.store).export(task['id'])
    with zipfile.ZipFile(s.store.blobs.path(exported['blob_sha'])) as archive:
        text = archive.read('CONTINUE.md').decode()
        assert 'Prepare an edited copy' not in text
        assert 'Implement the correction yourself' not in archive.read('FEEDBACK.md').decode()
        assert feedback['next_action'] in text
    if not publication:
        with pytest.raises(AuthorError) as error:
            Publisher(s.store).preview(task['id'], revision=0)
        assert error.value.code == 'POLICY_DENIED'
    # A current passing receipt is required, regardless of review policy.
    policy = s.tasks.get(task['id'])['policy']
    policy['blocked_paths'].append('newly-blocked')
    update_policy(s, task['id'], policy, 'invalidate-evidence')
    assert s.recovery.get(task['id'])['state'] == 'awaiting_controller'


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux supervisor; no platform bypass')
def test_fresh_mcp_failure_draft_repair_checks_and_restart(setup_author):
    s, source, _, task, _ = setup_author
    p = qualified(s)
    lease = call(p, 'author_claim_task', {'task_id': task['id'], 'key': 'fresh-claim', 'takeover': True})
    authority = {'token': lease['token'], 'epoch': lease['epoch']}
    first = call(p, 'author_run_checks', {'task_id': task['id'], 'revision': 0, 'key': 'fresh-baseline', **authority})
    assert wait_for(s, first)['state'] == 'failed'
    assert call(p, 'author_get_feedback', {'task_id': task['id']})['classification'] == 'CHECKS_FAILED'
    context = call(p, 'author_export_context', {'task_id': task['id'], 'key': 'baseline-context'})
    original = call(p, 'author_read_file', {'context_id': context['context_id'], 'path': 'app.py'})
    draft = call(p, 'author_apply_edits', {'task_id': task['id'], 'expected_revision': 0,
        'expected_version': 0, 'key': 'repair', **authority, 'edits': [{'op': 'replace', 'path': 'app.py',
        'before_sha256': original['sha256'], 'content': 'def add(a,b):\n    return a+b\n'}]})
    request = {'draft_id': draft['draft_id'], 'expected_version': 1, 'key': 'seal-replay', **authority}
    accepted = call(p, 'author_seal_draft', request)
    # Lost response: fresh controller reuses the exact key and obtains the same candidate.
    fresh = qualified(AuthorService(s.store.home))
    assert call(fresh, 'author_seal_draft', request) == accepted
    run = call(fresh, 'author_run_checks', {'task_id': task['id'], 'revision': 1, 'key': 'fresh-repaired', **authority})
    assert wait_for(s, run)['state'] == 'completed'
    feedback = call(fresh, 'author_get_feedback', {'task_id': task['id']})
    assert feedback['classification'] == 'READY_FOR_REVIEW'
    assert feedback['receipt']['commit'] == accepted['commit']
    assert feedback['receipt']['delegated_inference_by_author_service'] is False
    call(fresh, 'author_export_context', {'task_id': task['id'], 'key': 'candidate-context'})
    recovered = call(MCP(AuthorService(s.store.home)), 'author_recover_task', {'task_id': task['id']}, role='reader')
    assert recovered['state'] == 'awaiting_review'
    assert recovered['candidate']['candidate_context']['commit_oid'] == accepted['commit']
    assert 'a - b' in (source / 'app.py').read_text()
    assert lease['token'] not in json.dumps(recovered)
