"""Queued checks expose cached blockers without turning polls into writes."""
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.util import fingerprint


def test_admission_blockers_survive_reopen_and_unchanged_observations_write_nothing(setup_author):
    s, _, _, task, _ = setup_author
    op, _ = s.store.begin_operation(kind='checks.run', task_id=task['id'], key='queued-contract',
        fingerprint=fingerprint({'fixture': 'queued, no execution'}), payload={'selected': ['unit']}, revision=0)
    checks = Checks(s.store)
    details = {'gates': {'observed_cpu': False}, 'reservation': {'cpu_cores': 1}, 'active_jobs': 2,
               'capacity': {'cpu_cores': 1, 'available_memory_bytes': 12345}}
    checks._record_wait(op['id'], 'resource_budget', details)
    count = s.store.one('SELECT COUNT(*) AS n FROM events')['n']
    page = s.recovery.operations(task['id'])
    checks._record_wait(op['id'], 'resource_budget', {**details, 'capacity': {'available_memory_bytes': 23456}})
    assert s.store.one('SELECT COUNT(*) AS n FROM events')['n'] == count
    assert s.recovery.operations(task['id'])['cursor'] == page['cursor']
    from patchbay.authoring.service import AuthorService
    restored = AuthorService(s.store.home)
    current = restored.store.operation(op['id'])
    assert current['phase'] == 'waiting_for_admission'
    assert current['admission']['reason'] == 'resource_budget'
    assert restored.recovery.get(task['id'])['state'] == 'awaiting_resources'
    checks._record_wait(op['id'], 'resource_budget', {**details, 'gates': {'observed_cpu': True, 'disk': False}})
    assert s.store.one('SELECT COUNT(*) AS n FROM events')['n'] == count + 1
