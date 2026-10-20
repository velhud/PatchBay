"""Selected dirty inputs, policy CAS and unsupported Git content boundaries."""
from __future__ import annotations

import io
import json

import pytest

from patchbay.authoring.dirty import DirtySnapshots
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.policy import Policies
from patchbay.authoring.source_windows import lines
from conftest import git
from test_core import package_for, submit


def test_crlf_split_at_chunk_boundary_is_lossless():
    first = 'x' * 16383 + '\r\n'
    payload = first + 'next\rfinal\n'
    result = list(lines(io.BytesIO(payload.encode()), preview_bytes=20000, query='next'))
    assert [row[:3] for row in result] == [(1, first, len(first)), (2, 'next\r', 5), (3, 'final\n', 6)]
    assert [row[3] for row in result] == [False, True, False]
    assert list(lines(io.BytesIO(b'one\r'), preview_bytes=20))[0][:3] == (1, 'one\r', 4)


def test_dirty_capture_pins_selected_bytes_and_preserves_operator_checkout(setup_author):
    s, source, repo, _, _ = setup_author
    head = git(source, 'rev-parse', 'HEAD')
    (source / 'app.py').write_text('dirty selected source\n')
    (source / 'unselected.txt').write_text('private unselected source\n')
    status = git(source, 'status', '--porcelain=v1')
    captured = DirtySnapshots(s.store).capture(repo['id'], ['app.py'], key='capture')
    task = s.tasks.create(repo['id'], 'Use selected dirty code', base=captured['commit'], key='dirty-task')
    mirror = s.repositories.git(repo['id'])
    assert mirror.read_file(task['current_commit'], 'app.py') == b'dirty selected source\n'
    assert 'unselected.txt' not in mirror.entries(task['current_commit'])
    assert task['origin_commit'] == head and task['current_commit'] != head
    assert git(source, 'rev-parse', 'HEAD') == head and git(source, 'status', '--porcelain=v1') == status
    (source / 'app.py').write_text('later unrelated edits\n')
    assert DirtySnapshots(s.store).capture(repo['id'], ['app.py'], key='capture') == captured


def test_dirty_capture_recovers_retained_ref_after_lost_database_ack(setup_author, monkeypatch):
    s, source, repo, _, _ = setup_author
    (source / 'app.py').write_text('frozen before lost acknowledgement\n')
    capture = DirtySnapshots(s.store)
    record = capture._record
    monkeypatch.setattr(capture, '_record', lambda *args: (_ for _ in ()).throw(OSError('fixture DB outage')))
    with pytest.raises(OSError):
        capture.capture(repo['id'], ['app.py'], key='lost')
    (source / 'app.py').write_text('changed after interrupted capture\n')
    monkeypatch.setattr(capture, '_record', record)
    result = capture.capture(repo['id'], ['app.py'], key='lost')
    assert s.repositories.git(repo['id']).read_file(result['commit'], 'app.py') == b'frozen before lost acknowledgement\n'


def test_policy_change_is_explicit_cas_and_replay_safe(setup_author):
    s, _, _, task, _ = setup_author
    policy = TaskPolicy.model_validate(task['policy'])
    policy.blocked_paths.append('private/**')
    values = dict(expected_digest=task['policy_digest'], actor='fixture-operator', key='change')
    changed = Policies(s.store).update(task['id'], policy, **values)
    assert changed['new_verification_required'] and s.tasks.get(task['id'])['status'] == 'policy_changed'
    assert Policies(s.store).update(task['id'], policy, **values) == changed
    with pytest.raises(AuthorError) as error:
        Policies(s.store).update(task['id'], policy, **{**values, 'key': 'stale'})
    assert error.value.code == 'STALE_POLICY'
    assert s.recovery.get(task['id'])['policy_digest'] == changed['policy_digest']


@pytest.mark.parametrize('kind', ['pointer', 'attributes', 'remove-attributes'])
def test_lfs_mutation_is_explicitly_unsupported_without_new_revision(setup_author, tmp_path, kind):
    s, source, repo, _, _ = setup_author
    pointer = b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'a' * 64 + b'\nsize 100\n'
    (source / 'asset.dat').write_bytes(pointer if kind == 'pointer' else b'baseline object bytes')
    if kind != 'pointer':
        (source / '.gitattributes').write_text('asset.dat filter=lfs diff=lfs merge=lfs -text\n')
    git(source, 'add', 'asset.dat')
    if kind != 'pointer':
        git(source, 'add', '.gitattributes')
    git(source, 'commit', '-m', 'LFS fixture')
    s.repositories.refresh(repo['id'], key='lfs-refresh')
    task = s.tasks.create(repo['id'], 'Explicit unsupported content', key='lfs-task')
    lease = s.leases.claim(task['id'])
    changes = {'asset.dat': 'new source bytes'}
    if kind == 'remove-attributes':
        changes['.gitattributes'] = ''
    package = package_for(s, task, tmp_path, changes)
    with pytest.raises(AuthorError) as error:
        submit(s, task, package, lease)
    assert error.value.code == 'UNSUPPORTED_FILE'
    assert s.tasks.get(task['id'])['current_revision'] == 0
