"""Disposable blob corruption through the real export owner; no operator data."""
import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.cli import copy_blob


def test_corrupt_blob_cannot_be_exported_under_the_old_digest(
    setup_author, tmp_path
):
    service, _, _, _, _ = setup_author
    expected = b"s05 fixture: intact\n"
    sha = service.store.blobs.put(expected)
    fixture_blob = service.store.blobs.path(sha)
    fixture_blob.write_bytes(b"x" * len(expected))
    destination = tmp_path / "not-a-successful-export.bin"

    with pytest.raises(AuthorError) as error:
        copy_blob(service.store, sha, destination)

    assert error.value.code in {
        "CORRUPT_STATE", "ARTIFACT_INTEGRITY_ERROR", "CONTENT_HASH_MISMATCH"
    }
    assert not destination.exists(), "A failed export must not expose a final-name artifact"


def test_authenticated_http_refuses_corrupt_and_cross_task_artifacts(setup_author):
    from fastapi.testclient import TestClient
    from patchbay.authoring.interfaces.auth import Access
    from patchbay.authoring.interfaces.http import create_app
    s,_,repo,task,_=setup_author
    context=s.snapshots.export(task['id'])
    other=s.tasks.create(repo['id'],'Other task',key='other-artifact')
    other_context=s.snapshots.export(other['id'])
    token=Access(s.store).issue('artifact-reader','reader',[task['id']],key='reader')['token']
    headers={'Authorization':'Bearer '+token}
    with TestClient(create_app(s.store.home)) as client:
        response=client.get('/api/blobs/'+context['bundle_sha'],headers=headers)
        assert response.status_code==200
        import hashlib
        assert hashlib.sha256(response.content).hexdigest()==context['bundle_sha']
        assert response.headers['x-content-sha256']==context['bundle_sha']
        assert client.get('/api/blobs/'+other_context['bundle_sha'],headers=headers).status_code==403
        s.store.blobs.path(context['bundle_sha']).write_bytes(b'corrupt referenced bytes')
        response=client.get('/api/blobs/'+context['bundle_sha'],headers=headers)
        assert response.status_code==409 and response.json()['error']['code']=='CORRUPT_STATE'


def test_interrupted_export_collision_and_link_never_expose_wrong_final(setup_author,tmp_path,monkeypatch):
    import os
    s,_,_,_,_=setup_author;blobs=s.store.blobs
    data=b'x'*200000;sha=blobs.put(data);output=tmp_path/'delivery.bin'
    original=blobs.delivery_chunks
    def interrupted(*args):
        yield b'x'*65536
        raise OSError('controlled write interruption')
    with monkeypatch.context() as patch:
        patch.setattr(blobs,'delivery_chunks',interrupted)
        with pytest.raises(OSError):blobs.export(sha,output)
    assert not output.exists() and not list(tmp_path.glob('.author-export-*'))
    real_link=os.link
    def race(source,target,*args,**kwargs):
        output.write_bytes(b'concurrent user data')
        return real_link(source,target,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os,'link',race)
        with pytest.raises(AuthorError,match='already exists'):blobs.export(sha,output)
    assert output.read_bytes()==b'concurrent user data'
    output.unlink();output.symlink_to(tmp_path/'untouched')
    with pytest.raises(AuthorError):blobs.export(sha,output)
    assert not (tmp_path/'untouched').exists() and blobs.verify(sha)
    output.unlink()
    assert blobs.export(sha,output)['bytes']==len(data)
    assert output.read_bytes()==data and not list(tmp_path.glob('.author-export-*'))


def test_delivery_uses_same_verified_descriptor_and_bounded_chunks(setup_author,tmp_path):
    import hashlib
    import os
    s,_,_,_,_=setup_author;blobs=s.store.blobs;data=b'z'*2000000;sha=blobs.put(data)
    with blobs.open_verified(sha) as stream:
        replacement=tmp_path/'different';replacement.write_bytes(b'wrong bytes')
        os.replace(replacement,blobs.path(sha))
        class Bounded:
            def read(self,size):
                assert 0<size<=65536
                return stream.read(size)
        h=hashlib.sha256();count=0
        for chunk in blobs.delivery_chunks(Bounded(),sha,len(data)):
            assert len(chunk)<=65536
            h.update(chunk);count+=len(chunk)
        assert count==len(data) and h.hexdigest()==sha


def test_cancelled_verifier_closes_late_descriptor(setup_author):
    import asyncio
    import threading
    from patchbay.authoring.interfaces.http import open_delivery_source
    s,_,_,_,_=setup_author;blobs=s.store.blobs;sha=blobs.put(b'controlled verifier')
    entered=threading.Event();release=threading.Event();returned=[]
    class Delayed:
        def open_verified(self,sha):
            entered.set()
            assert release.wait(5)
            source=blobs.open_verified(sha);returned.append(source);return source
    async def scenario():
        task=asyncio.create_task(open_delivery_source(Delayed(),sha))
        assert await asyncio.to_thread(entered.wait,5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        release.set()
        for _ in range(100):
            if returned and returned[0].closed:break
            await asyncio.sleep(.01)
        assert returned and returned[0].closed
    asyncio.run(scenario())


def test_failed_http_send_releases_owned_descriptor():
    import asyncio
    import io
    from patchbay.authoring.interfaces.http import VerifiedBlobResponse
    source=io.BytesIO(b'bounded delivery')
    response=VerifiedBlobResponse(source,iter([b'bounded delivery']))
    async def send(message):raise RuntimeError('controlled response disconnect')
    async def receive():return {'type':'http.disconnect'}
    async def scenario():
        with pytest.raises(RuntimeError):
            await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    asyncio.run(scenario())
    assert source.closed
