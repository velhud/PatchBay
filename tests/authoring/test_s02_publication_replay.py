"""Real disposable Git push/readback; verification input is explicitly synthetic.

This qualifies publication mechanics, never a real check backend or live client.
"""
import os

from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.execution.receipts import record_receipt
from patchbay.authoring.publication.git import Publisher
from patchbay.authoring.publication import git as publication
from conftest import git
from test_core import package_for, submit
from test_controller_contracts import qualified
from test_s01_interface_contracts import request
from test_fault_boundaries import _seed_check_run, _synthetic_receipt


def test_lost_local_push_response_recovers_exact_remote_without_second_push(setup_author, tmp_path, monkeypatch):
    service, source, original_repo, _, _ = setup_author
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    remote = tmp_path / 'only-disposable-remote.git'
    remote.mkdir()
    git(remote, 'init', '--bare', '--initial-branch=main')
    git(source, 'push', str(remote), 'main:main')
    policy = TaskPolicy.model_validate(original_repo['policy'])
    policy.publication.enabled = True
    policy.publication.remote_effects = "controlled_no_automation"
    repo = service.repositories.register('unit-publication', source, policy, remote=str(remote))
    task = service.tasks.create(repo['id'], 'Publication mechanics fixture', key='publication-task')
    lease = service.leases.claim(task['id'])
    accepted = submit(service, task, package_for(service, task, tmp_path,
        {'app.py': 'def add(a,b): return a+b\n'}), lease)
    run, current = _seed_check_run(service, task, key='synthetic-verification-only')
    record_receipt(service.store, run['id'], _synthetic_receipt(service, current, run, passed=True))
    authority = {'token': lease['token'], 'epoch': lease['epoch']}
    approved = Publisher(service.store).approve(task['id'], 1, actor='fixture-operator', key='approval', **authority)
    protocol = qualified(service, role='operator')
    original = publication.invoke
    pushes = []
    def lose_ack(root, argv, *values, **options):
        result = original(root, argv, *values, **options)
        if argv and argv[0] == 'push':
            pushes.append(list(argv))
            raise OSError('fixture lost push reply after actual local push')
        return result
    monkeypatch.setattr(publication, 'invoke', lose_ack)
    args = {'approval_id': approved['approval_id'], 'key': 'publication-intent', **authority}
    response = request(protocol, 'author_publish', args, role='operator')
    assert response['isError'] and response['structuredContent']['code'] == 'EFFECT_OUTCOME_UNAVAILABLE'
    details = response['structuredContent']['details']
    assert details['task_id'] == task['id'] and details['operation_id']
    assert details['original_key'] == args['key'] and len(pushes) == 1
    assert service.store.operation(details['operation_id'])['state'] == 'publication_unknown'
    assert service.recovery.get(task['id'])['state'] == 'awaiting_controller'
    monkeypatch.setattr(publication, 'invoke', original)
    replay = request(qualified(service, role='operator'), 'author_publish', args, role='operator')
    assert not replay['isError']
    result = replay['structuredContent']
    assert result['pushed'] and not result['merged'] and result['commit'] == accepted['commit']
    assert git(remote, 'rev-parse', 'refs/heads/'+result['branch']) == accepted['commit']
    assert git(remote, 'rev-parse', 'refs/heads/main') == task['origin_commit']
    assert git(source, 'rev-parse', 'HEAD') == task['origin_commit']
    assert len(pushes) == 1
    assert service.store.one("SELECT COUNT(*) AS n FROM operations WHERE task_id=? AND kind='publication.apply'", (task['id'],))['n'] == 1
