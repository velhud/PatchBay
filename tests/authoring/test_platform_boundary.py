"""Unsupported hosts may prepare source but never dispatch a Linux supervisor."""
import sys
import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from test_core import package_for, submit


@pytest.mark.skipif(sys.platform == 'linux', reason='Unsupported-host boundary')
def test_direct_and_auto_queued_execution_fail_before_launch(setup_author, tmp_path):
    service, _, repository, task, lease = setup_author
    checks = Checks(service.store)
    with pytest.raises(AuthorError) as error:
        checks.submit(task['id'], revision=0, key='blocked', token=lease['token'], epoch=lease['epoch'])
    assert error.value.code == 'BLOCKED_ENVIRONMENT'
    assert service.store.rows('SELECT * FROM runs') == []
    from patchbay.authoring.util import canonical
    policy = repository['policy']; policy['auto_check'] = True
    with service.store.transaction() as conn:
        conn.execute('UPDATE repositories SET policy=? WHERE id=?',(canonical(policy).decode(), repository['id']))
    auto_task = service.tasks.create(repository['id'], 'Prepare on this host', key='prepared')
    auto_lease = service.leases.claim(auto_task['id'])
    package = package_for(service, auto_task, tmp_path, {'new.txt':'prepared exact source'})
    accepted = submit(service, auto_task, package, auto_lease, 'source')
    with pytest.raises(AuthorError) as error:
        checks.dispatch(operation_ids=[accepted['checks_operation_id']])
    assert error.value.code == 'BLOCKED_ENVIRONMENT'
    run = service.store.one('SELECT * FROM runs WHERE id=?',(accepted['checks_operation_id'],))
    assert run['state'] == 'queued'
    assert not (service.store.home/'runs'/accepted['checks_operation_id']).exists()
    assert service.tasks.get(auto_task['id'])['current_revision'] == 1
