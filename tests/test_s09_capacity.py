"""Bounded actual Git producers and disposable Manager inbox controls."""
import io
import os
import zipfile
import pytest
from patchbay.artifacts import ArtifactStore, DEFAULT_IMPORT_LIMITS
from patchbay.workers.integration_state import build_candidate_snapshot, _run_git, CandidateSnapshotError
from test_audit_regressions_20261019 import git


def repository(tmp_path):
    root = tmp_path/'repo'; root.mkdir()
    git(root,'init','-b','main');git(root,'config','user.name','Fixture');git(root,'config','user.email','fixture@localhost')
    (root/'base').write_text('baseline\n');git(root,'add','.');git(root,'commit','-m','baseline')
    return root, git(root,'rev-parse','HEAD').strip()


def test_large_binary_candidate_is_bounded_and_metadata_skips_diff(tmp_path,monkeypatch):
    root, base = repository(tmp_path)
    (root/'binary').write_bytes(os.urandom(2_100_000))
    index = (root/'.git/index').read_bytes()
    snap = build_candidate_snapshot(root,base,max_patch_bytes=1000)
    assert snap.changed_files == ('binary',) and snap.patch == b'' and not snap.patch_complete
    import patchbay.workers.integration_state as module
    original = module._run_git
    def recording(root,args,**kw):
        assert '--binary' not in args
        return original(root,args,**kw)
    monkeypatch.setattr(module,'_run_git',recording)
    metadata = build_candidate_snapshot(root,base,include_patch=False)
    assert metadata.tree_sha == snap.tree_sha and metadata.entries == snap.entries
    assert metadata.changed_files == snap.changed_files and not metadata.patch_complete
    assert (root/'.git/index').read_bytes() == index and git(root,'rev-parse','HEAD').strip() == base


def test_capture_checks_complete_bytes_and_exit_even_after_overflow(tmp_path,monkeypatch):
    root, base = repository(tmp_path)
    (root/'big.txt').write_text('large content\n'*10000)
    git(root,'add','.');git(root,'commit','-m','content')
    with pytest.raises(CandidateSnapshotError,match='byte budget'):
        _run_git(root,['show','HEAD:big.txt'],max_output_bytes=100)
    result = _run_git(root,['show','HEAD:big.txt'],max_output_bytes=100,truncate_stdout=True)
    assert len(result.stdout) == 100 and result.stdout_truncated and result.returncode == 0
    def reaped_group_must_not_be_signalled(*args):
        raise AssertionError('Cannot signal a numeric process group after its leader was reaped')
    monkeypatch.setattr('patchbay.workers.integration_state.os.killpg',reaped_group_must_not_be_signalled)
    with pytest.raises(CandidateSnapshotError,match='exit code'):
        _run_git(root,['diff','--exit-code',base,'HEAD'],max_output_bytes=100,truncate_stdout=True)


@pytest.mark.parametrize('key',list(DEFAULT_IMPORT_LIMITS))
def test_default_inbox_limits_are_finite(tmp_path,key):
    for value in [None,'']:
        store = ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox'),key:value}})
        store._check_optional_limit(key,DEFAULT_IMPORT_LIMITS[key],key)
        with pytest.raises(ValueError,match='limit'):
            store._check_optional_limit(key,DEFAULT_IMPORT_LIMITS[key]+1,key)
    store.config['artifacts'][key] = 0
    with pytest.raises(ValueError,match='positive'):
        store._check_optional_limit(key,1,key)


def test_archive_entry_budget_and_duplicate_paths(tmp_path):
    store = ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox'),'max_file_count':2}})
    archive = tmp_path/'input.zip'; target = tmp_path/'unpacked'; target.mkdir()
    with zipfile.ZipFile(archive,'w') as z:
        for i in range(3):z.writestr(f'dir-{i}/',b'')
    with pytest.raises(ValueError,match='entry count'):
        store._unpack_zip(archive,target)
    assert not list(target.iterdir())
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('same',b'first');z.writestr('same',b'last')
    with pytest.raises(ValueError,match='duplicate'):
        store._unpack_zip(archive,target)
    assert (target/'same').read_bytes() == b'first'


def test_unpacked_stream_checks_actual_size(tmp_path,monkeypatch):
    store = ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox'),'max_single_file_bytes':3}})
    archive=tmp_path/'input.zip';target=tmp_path/'unpacked';target.mkdir()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('small',b'a')
    monkeypatch.setattr(zipfile.ZipFile,'open',lambda *a,**k:io.BytesIO(b'1234'))
    with pytest.raises(ValueError,match='max_single_file_bytes'):
        store._unpack_zip(archive,target)
    assert (target/'small').stat().st_size == 0


def test_materialization_reuses_exact_content_and_preserves_edits(tmp_path):
    root, _ = repository(tmp_path)
    store = ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox')},'repositories':{'allowed':[str(tmp_path)]}})
    workspace=store.workspace_id(str(root)); artifact_id='art_0123456789abcdef0123'
    inbox=store._artifact_dir(workspace,artifact_id);source=inbox/'unpacked';source.mkdir(parents=True)
    (source/'input.txt').write_text('retained\n')
    store._write_json(inbox/'artifact.json',{'artifact_id':artifact_id,'imported_at':1,'file_count':1})
    dest=tmp_path/'destination'
    store.materialize_artifacts(repo_path=str(root),artifact_ids=[artifact_id],destination_root=dest)
    assert store.materialize_artifacts(repo_path=str(root),artifact_ids=[artifact_id],destination_root=dest)
    (dest/artifact_id/'input.txt').write_text('implementation edit\n')
    with pytest.raises(ValueError,match='preserve edits'):
        store.materialize_artifacts(repo_path=str(root),artifact_ids=[artifact_id],destination_root=dest)
    assert (dest/artifact_id/'input.txt').read_text() == 'implementation edit\n'


def test_inbox_pages_keep_total_and_stable_tie_order(tmp_path,monkeypatch):
    store=ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox')}})
    monkeypatch.setattr(store,'_validated_repo_path',lambda x:x)
    monkeypatch.setattr(store,'_iter_metadata',lambda _: [{'artifact_id':f'art_{i:020x}','imported_at':1} for i in range(205)])
    pages=[store.list_artifacts(repo_path=str(tmp_path),offset=i,limit=100) for i in [0,100,200]]
    assert [len(p['artifacts']) for p in pages] == [100,100,5]
    assert all(p['count']==205 for p in pages) and pages[-1]['next_offset'] is None
    assert len({r['artifact_id'] for p in pages for r in p['artifacts']})==205


def test_pro_event_tail_ignores_large_prefix(tmp_path):
    from patchbay.pro_requests.store_legacy import ProRequestStore
    with (tmp_path/'events.jsonl').open('w') as stream:
        stream.write('old malformed prefix'*100000+'\n')
        for i in range(120):stream.write('{"seq":'+str(i)+'}\n')
    assert [e['seq'] for e in ProRequestStore._read_events(object(),tmp_path)] == list(range(20,120))


def test_dishonest_entry_count_refuses_before_zipinfo_allocation(tmp_path,monkeypatch):
    import struct
    store=ArtifactStore({'artifacts':{'root':str(tmp_path/'inbox'),'max_file_count':2}})
    path=tmp_path/'input.zip';target=tmp_path/'unpack';target.mkdir()
    with zipfile.ZipFile(path,'w') as z:
        for i in range(3):z.writestr(str(i),b'x')
    raw=bytearray(path.read_bytes())
    # Advertise one member while the central directory actually contains three.
    struct.pack_into('<HH',raw,len(raw)-22+8,1,1)
    path.write_bytes(raw)
    def no_allocation(*a,**k):raise AssertionError('ZipInfo allocation must follow bounded record count')
    monkeypatch.setattr(zipfile,'ZipFile',no_allocation)
    with pytest.raises(ValueError,match='entry count'):
        store._unpack_zip(path,target)
    assert not list(target.iterdir())
