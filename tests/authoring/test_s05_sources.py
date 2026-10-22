"""Real owned Git/Python producers, controlled EOF/exit/deadline faults only."""
import hashlib
import subprocess
import sys

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.revisions import git_objects
from patchbay.authoring.source_windows import read_window


def inject_blob_producer(monkeypatch,script):
    original=git_objects.subprocess.Popen
    def spawn(command,*args,**kwargs):
        if isinstance(command,list) and command[:1]==['git'] and 'cat-file' in command:
            position=command.index('cat-file')
            if command[position+1]=='blob':command=[sys.executable,'-c',script]
        return original(command,*args,**kwargs)
    monkeypatch.setattr(git_objects.subprocess,'Popen',spawn)


@pytest.mark.parametrize('fault',['short','nonzero'])
def test_full_object_cannot_certify_short_or_failed_producer(setup_author,monkeypatch,fault):
    service,_,repo,task,_=setup_author
    git=service.repositories.git(repo['id']);data=git.read_file(task['current_commit'],'app.py')
    emitted=data[:5] if fault=='short' else data
    inject_blob_producer(monkeypatch,'import sys;sys.stdout.buffer.write('+repr(emitted)+');sys.stdout.flush();sys.exit('+('0' if fault=='short' else '7')+')')
    with pytest.raises(AuthorError) as error:
        read_window(git,task['current_commit'],'app.py',start_line=1,max_lines=1,max_bytes=100)
    assert error.value.code=='SOURCE_READ_FAILED'


def test_bounded_prefix_does_not_require_full_object_consumption(setup_author):
    service,_,repo,_,_=setup_author;git=service.repositories.git(repo['id'])
    data=b'x'*200000
    oid=git.text(['hash-object','-w','--stdin'],data=data)
    with git.open_blob(oid,mode='prefix') as stream:assert stream.read(64)==data[:64]
    with pytest.raises(AuthorError,match='stopped before'):
        with git.open_blob(oid) as stream:stream.read(64)


def test_stalled_blob_read_has_finite_deadline_and_reaps_owned_child(setup_author,monkeypatch):
    service,_,repo,task,_=setup_author;git=service.repositories.git(repo['id'])
    oid=git.entries(task['current_commit'])['app.py']['oid']
    inject_blob_producer(monkeypatch,'import time;time.sleep(30)')
    stream=None
    with pytest.raises(AuthorError) as error:
        with git.open_blob(oid,timeout=.1) as stream:stream.read()
    assert error.value.code=='SOURCE_READ_TIMEOUT'
    assert stream.closed and stream.process.returncode is not None


def test_failed_diff_cannot_appear_as_clean_empty_diff(setup_author,monkeypatch):
    service,_,repo,task,_=setup_author;git=service.repositories.git(repo['id'])
    original=git_objects.subprocess.Popen
    def spawn(command,*args,**kwargs):
        if isinstance(command,list) and command[:1]==['git'] and len(command)>3 and command[3]=='diff':
            command=[sys.executable,'-c','import sys;sys.stderr.write("fixture failure");sys.exit(7)']
        return original(command,*args,**kwargs)
    monkeypatch.setattr(git_objects.subprocess,'Popen',spawn)
    with pytest.raises(AuthorError) as error:git.diff(task['current_commit'],task['current_commit'])
    assert error.value.code=='SOURCE_READ_FAILED'


def test_diff_budget_is_explicit_partial_with_guidance(setup_author):
    service,_,repo,task,_=setup_author;git=service.repositories.git(repo['id'])
    commit,_=git.create_candidate(task['current_commit'],{'app.py':(b'z'*5000,'100644')},
        operation_id='fixture-diff',created=1700000000,ref='refs/author/fixture-diff',staging=service.store.home/'staging')
    result=git.diff(task['current_commit'],commit,limit=20)
    assert result['partial'] and result['truncated'] and not result['complete']
    assert result['returned_bytes']==20 and 'narrower path' in result['next_action']
    full=git.diff(task['current_commit'],commit)
    assert full['complete'] and not full['partial']


def test_nul_and_split_crlf_byte_identity(setup_author):
    service,_,repo,task,_=setup_author;git=service.repositories.git(repo['id'])
    data='a\x00b\r\né\r\nx'.encode('utf-8')
    commit,_=git.create_candidate(task['current_commit'],{'bytes.txt':(data,'100755')},
        operation_id='fixture-bytes',created=1700000000,ref='refs/author/fixture-bytes',staging=service.store.home/'staging')
    import base64
    page=read_window(git,commit,'bytes.txt',start_line=1,max_lines=200,max_bytes=3,byte_offset=0)
    assert page['binary'] and base64.b64decode(page['data_base64'])==data[:3]
    assert page['mode']=='100755' and page['sha256']==hashlib.sha256(data).hexdigest()
