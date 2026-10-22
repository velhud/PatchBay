"""Dirty capture records its deliberately weaker trusted-local guarantee."""
from patchbay.authoring.dirty import DirtySnapshots


def test_dirty_capture_reports_per_file_quiescence_and_replays_pinned_bytes(setup_author,monkeypatch):
    s,source,repo,_,_=setup_author
    (source/'app.py').write_text('first live bytes\n')
    original=DirtySnapshots._record
    def lost_record(*args,**kwargs):raise OSError('lost receipt after retained Git ref')
    with monkeypatch.context() as patch:
        patch.setattr(DirtySnapshots,'_record',lost_record)
        try:DirtySnapshots(s.store).capture(repo['id'],['app.py'],key='dirty')
        except OSError:pass
    (source/'app.py').write_text('later live bytes\n')
    result=DirtySnapshots(s.store).capture(repo['id'],['app.py'],key='dirty')
    assert result['capture_consistency']=='per_file_stable_reads_and_unchanged_head'
    assert result['globally_atomic'] is False and 'quiescent' in result['consistency_requirement']
    assert s.repositories.git(repo['id']).read_file(result['commit'],'app.py')==b'first live bytes\n'
    assert (source/'app.py').read_text()=='later live bytes\n'
