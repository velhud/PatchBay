"""Counterexamples from the complete package audit, asserted as intended behavior."""
from __future__ import annotations

import json
import tracemalloc
import zipfile

from fastapi.testclient import TestClient

from patchbay.authoring.interfaces.http import create_app
from patchbay.authoring.packages.builder import build, prepare
from conftest import git


def context_tree(s, task, tmp_path):
    context = s.snapshots.export(task['id'])
    root = tmp_path / 'context'
    with zipfile.ZipFile(s.store.blobs.path(context['bundle_sha'])) as z:
        z.extractall(root)
    return root


def test_tracked_cache_named_source_is_preserved(setup_author, tmp_path):
    s, source, repo, _, _ = setup_author
    (source / '__pycache__').mkdir()
    (source / '__pycache__/keep.txt').write_text('tracked bytes\n')
    git(source, 'add', '__pycache__/keep.txt')
    git(source, 'commit', '-m', 'track unusual source')
    s.repositories.refresh(repo['id'], key='refresh')
    task = s.tasks.create(repo['id'], 'Change only app', key='fresh')
    context = context_tree(s, task, tmp_path)
    edited = tmp_path / 'edited'
    prepare(context, edited)
    (edited / 'app.py').write_text('def add(a,b): return a+b\n')
    (edited / '__pycache__/untracked.pyc').write_bytes(b'ignored runtime output')
    manifest = build(context, edited, tmp_path / 'changes.zip')
    assert [o['path'] for o in manifest['operations']] == ['app.py']


def test_author_auth_uses_routed_path_despite_malformed_host(setup_author):
    s, _, _, _, _ = setup_author
    with TestClient(create_app(s.store.home), raise_server_exceptions=False) as client:
        for host in ('example.com', 'example.com/abc?bar=', 'example.com#fragment', 'example.com?query'):
            response = client.get('/api/repositories', headers={'host': host})
            assert response.status_code == 401
            assert 'repositories' not in response.json()


def test_search_paginates_inside_file_and_past_large_object(setup_author):
    s, source, repo, _, _ = setup_author
    (source / '000_large.txt').write_bytes(b'x' * (17 * 1024**2))
    (source / 'zzz.txt').write_text('needle one\nneedle two\n')
    git(source, 'add', '000_large.txt', 'zzz.txt')
    git(source, 'commit', '-m', 'search source')
    s.repositories.refresh(repo['id'], key='refresh')
    task = s.tasks.create(repo['id'], 'Search exact context', key='search')
    context = s.snapshots.export(task['id'], paths=['zzz.txt'])
    offset, found = 0, []
    for _ in range(8):
        result = s.snapshots.search(context['id'], 'needle', limit=1, offset=offset)
        found.extend((m['path'], m['line']) for m in result['matches'])
        if result['next_offset'] is None:
            break
        assert result['next_offset'] > offset
        offset = result['next_offset']
    else:
        raise AssertionError('Search cursor did not terminate')
    assert found == [('zzz.txt', 1), ('zzz.txt', 2)]


def test_small_source_window_does_not_allocate_whole_large_blob(setup_author):
    s, source, repo, _, _ = setup_author
    (source / 'big.txt').write_bytes(b'short line\r\n' * 1000000)
    git(source, 'add', 'big.txt')
    git(source, 'commit', '-m', 'large line inventory')
    s.repositories.refresh(repo['id'], key='refresh')
    task = s.tasks.create(repo['id'], 'Read tiny window', key='window')
    context = s.snapshots.export(task['id'], paths=['app.py'])
    tracemalloc.start()
    try:
        result = s.snapshots.read(context['id'], 'big.txt', max_lines=1, max_bytes=100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result['text'] == 'short line\r\n'
    assert result['total_lines'] == 1000000 and result['next_start_line'] == 2
    assert peak < 3 * 1024**2
