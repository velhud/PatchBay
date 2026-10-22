"""Exact source, selected export and preparation through actual Author owners."""
import base64
import hashlib
import json
import zipfile

import pytest
from pydantic import ValidationError

from patchbay.authoring.authority import AuthIdentity, identity_context
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.inputs import ReadFile
from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.packages import builder
from patchbay.authoring.revisions.drafts import Drafts
from patchbay.authoring.revisions.git_objects import Git
from conftest import git


def read(protocol, args):
    result = protocol.handle('reader', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'author_read_file', 'arguments': args}})['result']
    assert not result['isError'], result
    return result['structuredContent']


def extract(service, context, target):
    with zipfile.ZipFile(service.store.blobs.path(context['bundle_sha'])) as archive:
        archive.extractall(target)
    return target


def large_base(service, source, repo, *, binary=False):
    data = b'\x00\xff\r\n' * 80000 if binary else b'z' * 350000
    (source / 'large.bin').write_bytes(data)
    (source / '.gitattributes').write_text('app.py export-ignore\nlarge.bin export-subst\n')
    git(source, 'add', '.gitattributes', 'large.bin')
    git(source, 'commit', '-m', 'unrelated fixture payload')
    service.repositories.refresh(repo['id'], key='refresh-large')
    task = service.tasks.create(repo['id'], 'Small change on larger base', key='large-base')
    lease = service.leases.claim(task['id'])
    return task, lease, data


def test_long_unsealed_exact_versions_reconstruct_through_reader_mcp(setup_author):
    s, _, _, task, lease = setup_author
    drafts = Drafts(s.store)
    authority = {'token': lease['token'], 'epoch': lease['epoch']}
    text = 'é€' * 65000
    data = text.encode()
    first = drafts.apply(task['id'], expected_revision=0, expected_version=0, key='large-draft',
        edits=[{'op': 'create', 'path': 'long.txt', 'content': text}], **authority)
    second = drafts.apply(task['id'], draft_id=first['draft_id'], expected_revision=0, expected_version=1,
        key='advance', edits=[{'op': 'replace', 'path': 'long.txt', 'before_sha256': hashlib.sha256(data).hexdigest(),
        'content': 'second'}], **authority)
    protocol = MCP(s)
    selector = {'draft_id': first['draft_id'], 'draft_version': 1, 'path': 'long.txt'}
    failed = protocol.handle('reader', {'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'author_read_file','arguments':selector}})['result']
    assert failed['isError']
    hint = failed['structuredContent']['details']['next_read']
    assert hint['tool'] == 'author_read_file'
    assert hint['arguments']['draft_version'] == 1
    before = s.store.one('SELECT COUNT(*) n FROM operations')['n']
    collected = bytearray(); offset = 0
    while True:
        page = read(protocol, {**selector, 'byte_offset': offset, 'max_bytes': 65536})
        chunk = base64.b64decode(page['data_base64'], validate=True)
        assert page['returned_bytes'] == len(chunk) <= 65536
        assert page['chunk_sha256'] == hashlib.sha256(chunk).hexdigest()
        assert page['sha256'] == hashlib.sha256(data).hexdigest()
        assert page['commit'] == first['commit'] and page['producer_complete'] and not page['binary']
        collected.extend(chunk)
        current = read(protocol, {**selector, 'draft_version': 2, 'byte_offset': 0})
        assert base64.b64decode(current['data_base64']) == b'second'
        assert current['commit'] == second['commit']
        if not page['has_more']:
            assert page['next_byte_offset'] is None
            break
        offset = page['next_byte_offset']
    assert collected == data
    assert read(protocol, {**selector, 'byte_offset': len(data)})['returned_bytes'] == 0
    assert s.store.one('SELECT COUNT(*) n FROM operations')['n'] == before
    assert s.tasks.get(task['id'])['current_revision'] == 0
    with identity_context(AuthIdentity('wrong-reader', 'reader', ('task_'+'0'*32,))):
        with pytest.raises(AuthorError) as error:
            drafts.read(first['draft_id'], 1, 'long.txt', byte_offset=0)
        assert error.value.code == 'FORBIDDEN'
    with pytest.raises(AuthorError): drafts.read(first['draft_id'], 1, 'long.txt', byte_offset=len(data)+1)


@pytest.mark.parametrize('arguments', [
    {'draft_id':'draft_'+'0'*32, 'byte_offset':0},
    {'context_id':'ctx_'+'0'*40, 'draft_id':'draft_'+'0'*32, 'draft_version':1},
    {'context_id':'ctx_'+'0'*40, 'byte_offset':-1},
    {'context_id':'ctx_'+'0'*40, 'byte_offset':0, 'max_bytes':65537},
    {'context_id':'ctx_'+'0'*40, 'byte_offset':0, 'start_line':2},
])
def test_byte_selector_and_cursor_schema_rejects_ambiguity(arguments):
    with pytest.raises(ValidationError): ReadFile.model_validate({'path':'app.py', **arguments})


def test_binary_inherited_draft_and_empty_crlf_bytes(setup_author):
    s, source, repo, _, _ = setup_author
    task, lease, data = large_base(s, source, repo, binary=True)
    draft = Drafts(s.store).apply(task['id'], expected_revision=0, expected_version=0, key='binary-inherited',
        edits=[{'op':'create','path':'empty','content':''}], token=lease['token'], epoch=lease['epoch'])
    protocol=MCP(s); selector={'draft_id':draft['draft_id'],'draft_version':1}
    fallback=read(protocol,{**selector,'path':'large.bin'})
    page=read(protocol,fallback['next_read']['arguments'])
    assert page['binary'] and base64.b64decode(page['data_base64']) == data[:65536]
    assert page['sha256']==hashlib.sha256(data).hexdigest()
    empty=read(protocol,{**selector,'path':'empty','byte_offset':0})
    assert not empty['has_more'] and empty['bytes']==0 and empty['window_covers_full_file']


def test_selected_context_and_small_seal_never_materialize_unrelated_payload(setup_author, tmp_path, monkeypatch):
    s, source, repo, _, _ = setup_author
    task, lease, _ = large_base(s, source, repo)
    s.store.limits.archive.member_bytes=1024
    s.store.limits.snapshot_bytes=2048
    s.store.limits.archive.members=10  # metadata inventory is independent of payload count
    original=Git.blob; seen=[]
    excluded=s.repositories.git(repo['id']).entries(task['current_commit'])['large.bin']['oid']
    def record(self,oid,**kwargs):
        seen.append(oid)
        assert oid != excluded, 'Unselected payload was materialized'
        return original(self,oid,**kwargs)
    monkeypatch.setattr(Git,'blob',record)
    context=s.snapshots.export(task['id'], paths=['app.py'])
    unpacked=extract(s,context,tmp_path/'selected')
    inventory=json.loads((unpacked/'INVENTORY.json').read_text())
    assert inventory['complete'] and inventory['format_version']==2
    assert inventory['files']['large.bin']['sha256'] is None
    assert inventory['files']['large.bin']['bytes'] is None
    assert inventory['files']['large.bin']['content_metadata']=='not_materialized'
    assert (unpacked/'source/app.py').read_bytes()==(source/'app.py').read_bytes()  # export-ignore does not apply
    assert not (unpacked/'source/large.bin').exists()
    authority={'token':lease['token'],'epoch':lease['epoch']};drafts=Drafts(s.store)
    before=hashlib.sha256((source/'app.py').read_bytes()).hexdigest()
    draft=drafts.apply(task['id'],expected_revision=0,expected_version=0,key='small',edits=[
        {'op':'replace','path':'app.py','before_sha256':before,'content':'def add(a,b): return a+b\n'}],**authority)
    sealed=drafts.seal(draft['draft_id'],expected_version=1,key='small-seal',**authority)
    assert sealed['revision']==1 and excluded not in seen
    added=drafts.apply(task['id'],expected_revision=1,expected_version=0,key='new-only',edits=[
        {'op':'create','path':'new.txt','content':'new'}],**authority)
    assert drafts.seal(added['draft_id'],expected_version=1,key='new-seal',**authority)['revision']==2
    context=s.store.one('SELECT * FROM contexts WHERE revision=1')
    inventory=json.loads(s.store.blobs.read(context['inventory_sha']))
    assert inventory['identity_only'] and not any(e['included'] for e in inventory['files'].values())
    assert 'large.bin' in inventory['files'] and excluded not in seen
    assert len(seen)<20


def test_inventory_budget_empty_selection_and_omitted_presence(setup_author,tmp_path):
    s,_,_,task,_=setup_author
    full=s.snapshots.export(task['id'],paths=[])
    assert all(e['included'] for e in json.loads(s.store.blobs.read(full['inventory_sha']))['files'].values())
    context=s.snapshots.export(task['id'],identity_only=True)
    root=extract(s,context,tmp_path/'identity');edited=tmp_path/'edited'
    builder.prepare(root,edited)
    (edited/'app.py').write_text('cannot create an omitted existing file')
    with pytest.raises(ValueError,match='omitted'):builder.build(root,edited,tmp_path/'bad.zip')
    s.store.limits.context_inventory_paths=1
    with pytest.raises(AuthorError,match='inventory path'):s.snapshots.export(task['id'],paths=['README.md'])


def test_preparation_interruption_delete_output_and_legacy_compatibility(setup_author,tmp_path,monkeypatch):
    s,_,_,task,lease=setup_author
    context=s.snapshots.export(task['id']);root=extract(s,context,tmp_path/'context')
    original=builder._regular; count=0
    def fail_copy(path):
        nonlocal count
        count+=1
        if count==3:raise OSError('controlled interrupted preparation')
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(builder,'_regular',fail_copy)
        with pytest.raises(OSError):builder.prepare(root,tmp_path/'partial')
    with pytest.raises(ValueError,match='incomplete'):builder.build(root,tmp_path/'partial',tmp_path/'partial.zip')
    edited=tmp_path/'edited';builder.prepare(root,edited)
    (edited/'README.md').unlink()
    with pytest.raises(ValueError,match='outside'):builder.build(root,edited,edited/'not-yet-created.zip')
    assert not (edited/'not-yet-created.zip').exists()
    package=tmp_path/'delete.zip';manifest=builder.build(root,edited,package)
    assert [op['op'] for op in manifest['operations']]==['delete']
    assert s.materializer.submit(task['id'],package,key='delete',token=lease['token'],epoch=lease['epoch'])['revision']==1
    sparse=tmp_path/'sparse';sparse.mkdir();(sparse/'app.py').write_text('replacement')
    with pytest.raises(ValueError,match='completed prepared'):builder.build(root,sparse,tmp_path/'sparse.zip')
    manual=builder.build(root,sparse,tmp_path/'manual.zip',manual_tree=True)
    assert any(op['op']=='delete' for op in manual['operations'])
    # Existing format-1 manual trees retain their former explicit full-tree contract.
    metadata=json.loads((root/'CONTEXT.json').read_text());metadata['format_version']=1
    inventory=json.loads((root/'INVENTORY.json').read_text());inventory.pop('format_version');inventory.pop('complete')
    (root/'CONTEXT.json').write_text(json.dumps(metadata));(root/'INVENTORY.json').write_text(json.dumps(inventory))
    assert builder.build(root,sparse,tmp_path/'legacy.zip')['format_version']==1
    metadata['format_version']=3;(root/'CONTEXT.json').write_text(json.dumps(metadata))
    with pytest.raises(ValueError,match='Unsupported context'):builder.build(root,sparse,tmp_path/'future.zip')
