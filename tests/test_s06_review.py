"""Retained source, explicit review and generation boundaries without inference."""
from pathlib import Path
import pytest
from patchbay.jobs.manager import JobManager
from patchbay.workers.runtime import WorkerRuntime
from patchbay.workers.integration_state import CandidateSnapshotError
from test_audit_regressions_20261019 import completed_worker, complete, NoModelExecutor, git


@pytest.mark.asyncio
async def test_review_reads_stay_pinned_and_controller_decision_is_explicit(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'output.txt').write_text('reviewed version\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    target=preview['review_target'];cid=target['candidate_id']
    (root/'output.txt').write_text('later unreviewed version\n')
    reopened=WorkerRuntime(config,JobManager(config),NoModelExecutor())
    retained=await reopened.inspect_worker(worker='Luna',view='file',file_path='output.txt',candidate_id=cid)
    assert retained['text']=='reviewed version' and retained['mutable_workspace'] is False
    with pytest.raises(ValueError,match='stale'):
        await runtime.integrate_worker(worker='Luna',review_decision={'candidate_id':cid,'disposition':'accepted'})
    (root/'output.txt').write_text('reviewed version\n')
    applied=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='reviewed',
        review_decision={'candidate_id':cid,'disposition':'accepted','evidence_refs':['review-fixture-report']})
    assert applied['applied'] and applied['controller_review']['tree_sha']==target['tree_sha']
    assert manager.get_job(job.job_id).options['_worker_controller_review']['candidate_id']==cid
    (Path(config['repositories']['default'])/'output.txt').write_text('later operator change\n')
    replay=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='reviewed')
    assert replay['applied'] and replay['receipt_scope']=='historical_delivery' and replay['current_target_matches'] is False


@pytest.mark.asyncio
async def test_dependent_lane_starts_at_explicit_retained_snapshot(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'upstream.txt').write_text('upstream generation\n');complete(manager,job)
    target=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    started=await runtime.start_worker(name='Dependent',brief='Use exactly the approved upstream generation.',
        repo_path=config['repositories']['default'],source_candidate={'worker':'Luna','candidate_id':target['candidate_id']})
    child=runtime._resolve_worker('Dependent')[-1]
    assert started['accepted'] and (Path(child.worktree_path)/'upstream.txt').read_text()=='upstream generation\n'
    assert git(Path(child.worktree_path),'rev-parse','HEAD').strip()==target['snapshot_commit']
    assert not (Path(config['repositories']['default'])/'upstream.txt').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[1,10,100])
async def test_peer_diff_captures_once_and_reports_member_failures(tmp_path,monkeypatch,count):
    config,manager,runtime,job=await completed_worker(tmp_path)
    for i in range(count): (Path(job.worktree_path)/f'file-{i}.txt').write_text('implementation\n')
    complete(manager,job);original=runtime._candidate_snapshot;captures=[]
    def capture(jobs):
        captures.append(1);return original(jobs)
    monkeypatch.setattr(runtime,'_candidate_snapshot',capture)
    import subprocess, time, json
    run = subprocess.run; commands = []
    def observe(*args,**kwargs):
        commands.append(1);return run(*args,**kwargs)
    monkeypatch.setattr(subprocess, 'run', observe)
    started = time.monotonic()
    context=runtime._worker_context_prompt(['Luna'],detail='review',repo_path=config['repositories']['default'])
    print(json.dumps({'paths': count, 'captures': len(captures), 'subprocesses': len(commands),
        'retained_prompt_bytes': len(context['prompt'].encode()), 'seconds': round(time.monotonic()-started,4)}))
    assert len(captures)==1 and len(context['members'])==count
    assert context['review_targets'][0]['tree_sha']
    assert len(context['prompt'].encode())<=160000
    original_member=runtime._candidate_diff_member
    def failed(*args,**kwargs):
        if args[1]=='file-0.txt': return {'file_path':args[1],'diff':'','error':'git_observation_failed','complete':False,'truncated':False}
        return original_member(*args,**kwargs)
    monkeypatch.setattr(runtime,'_candidate_diff_member',failed)
    bad=runtime._context_diff_capture(runtime._resolve_worker('Luna'),byte_budget=100000)
    assert not bad['complete'] and any(m.get('error')=='git_observation_failed' for m in bad['members'])


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['mode','symlink'])
async def test_copied_context_requires_kind_and_mode_identity(tmp_path,mutation):
    config,manager,runtime,job=await completed_worker(tmp_path)
    # Existing helper creates a normal worker. Start another with selected context.
    base=Path(config['repositories']['default']);(base/'context.txt').write_text('same bytes\n')
    await runtime.start_worker(name='Context',brief='Read immutable context.',repo_path=str(base),include_untracked_from_base=['context.txt'])
    child=runtime._resolve_worker('Context')[-1];root=Path(child.worktree_path);target=root/'context.txt';complete(manager,child)
    if mutation=='mode': target.chmod(0o755)
    else:
        target.unlink();target.symlink_to('README.md')
    preview=await runtime.inspect_worker(worker='Context',view='integration_preview')
    assert not preview['can_apply'] and preview['reason']=='source_observation_failed'
    assert target.exists()


@pytest.mark.asyncio
async def test_source_reservation_coordinates_followup_cleanup_and_apply(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path);complete(manager,job)
    async with runtime.repo_locks.hold(job.worktree_path,operation='fixture-source-writer'):
        for method,kw in [(runtime.integrate_worker,{}),(runtime.message_worker,{'message':'Next turn'}),
                          (runtime.stop_worker,{'cleanup_workspace':True}),
                          (runtime.inspect_worker,{'view':'integration_preview'})]:
            result=await method(worker='Luna',**kw)
            assert result['repo_busy'] and not result['workspace_cleaned']
    assert Path(job.worktree_path).exists()


@pytest.mark.asyncio
async def test_safe_cleanup_preserves_only_ignored_tracked_implementation(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'.gitignore').write_text('*.generated\n')
    git(root,'add','.gitignore');git(root,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','ignore')
    (root/'needed.generated').write_text('required\n');git(root,'add','-f','needed.generated');complete(manager,job)
    result=await runtime.stop_worker(worker='Luna',cleanup_workspace=True,discard_unintegrated_changes=False)
    assert not result['workspace_cleaned'] and (root/'needed.generated').exists()
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    result=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='deliver-before-cleanup')
    assert result['applied']
    result=await runtime.stop_worker(worker='Luna',cleanup_workspace=True,discard_unintegrated_changes=False)
    assert result['workspace_cleaned'] and not root.exists()


@pytest.mark.asyncio
async def test_member_truncation_and_protection_make_aggregate_partial(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'long.txt').write_text('x'*300+'\n');(root/'.env').write_text('SECRET_FIXTURE=private-value\n');complete(manager,job)
    runtime.config.setdefault('security',{})['max_diff_bytes']=80
    runtime.config['security']['blocked_globs'].append('.env')
    context=runtime._worker_context_prompt(['Luna'],detail='review',repo_path=config['repositories']['default'])
    assert context['truncated'] and any(m.get('truncated') for m in context['members'])
    assert any(m.get('error')=='protected_source_path' for m in context['members'])
    assert 'private-value' not in context['prompt']


@pytest.mark.asyncio
async def test_status_observation_failure_is_never_clean(tmp_path,monkeypatch):
    import patchbay.workers.runtime_integration as owner
    config,manager,runtime,job=await completed_worker(tmp_path)
    (Path(job.worktree_path)/'one.txt').write_text('new\n');complete(manager,job)
    def fail(*args,**kwargs):raise CandidateSnapshotError('fixture failed status')
    monkeypatch.setattr(owner,'porcelain_paths',fail)
    result=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert not result['can_apply'] and not result['observation_complete']
    assert Path(job.worktree_path).exists()


@pytest.mark.asyncio
async def test_retained_review_and_cleanup_retry_survive_workspace_removal(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'one.txt').write_text('retained\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert (await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='cleanup-once'))['applied']
    assert (await runtime.stop_worker(worker='Luna',cleanup_workspace=True))['workspace_cleaned']
    root.mkdir();(root/'unrelated-reuse.txt').write_text('preserve unrelated reuse\n')
    again=await runtime.stop_worker(worker='Luna',cleanup_workspace=True)
    assert (root/'unrelated-reuse.txt').read_text()=='preserve unrelated reuse\n'
    pinned=await runtime.inspect_worker(worker='Luna',view='file',candidate_id=preview['review_target']['candidate_id'],file_path='one.txt')
    assert pinned['text']=='retained'


@pytest.mark.asyncio
async def test_file_to_directory_delivery_has_exact_absence_preimages(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'README.md').unlink();(root/'README.md').mkdir();(root/'README.md'/'child.txt').write_text('new child\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert preview['can_apply']
    result=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='file-to-directory')
    assert result['applied'] and (Path(config['repositories']['default'])/'README.md'/'child.txt').read_text()=='new child\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy',[False,True])
async def test_unchanged_and_legacy_context_revalidate_but_deleted_context_is_preserved(tmp_path,legacy):
    config,manager,runtime,job=await completed_worker(tmp_path)
    base=Path(config['repositories']['default']);(base/'context.txt').write_text('context\n')
    await runtime.start_worker(name='Context',brief='Read only selected context.',repo_path=str(base),include_untracked_from_base=['context.txt'])
    child=runtime._resolve_worker('Context')[-1];complete(manager,child)
    if legacy:
        def remove_manifest(options):options.pop('_worker_included_untracked_base_manifest',None);return options
        manager.mutate_job_options(child.job_id,remove_manifest)
    assert runtime._candidate_snapshot(runtime._resolve_worker('Context')).changed_files==()
    (Path(child.worktree_path)/'context.txt').unlink()
    preview=await runtime.inspect_worker(worker='Context',view='integration_preview')
    assert preview['reason']=='source_observation_failed' and Path(child.worktree_path).exists()


@pytest.mark.asyncio
async def test_retention_budget_refuses_before_creating_an_unrecorded_ref(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    (Path(job.worktree_path)/'one.txt').write_text('implementation\n');complete(manager,job)
    manager.mutate_job_options(job.job_id,lambda options:{**options,'_worker_review_targets':{f'{i:064x}':{} for i in range(128)}})
    root=Path(job.worktree_path);before=git(root,'for-each-ref','--format=%(refname)','refs/patchbay')
    result=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert not result['can_apply'] and result['reason']=='review_target_budget_exhausted'
    assert git(root,'for-each-ref','--format=%(refname)','refs/patchbay')==before


@pytest.mark.asyncio
async def test_public_preview_distinguishes_unsupported_gitlink_from_observation_failure(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path);root=Path(job.worktree_path)
    commit=git(root,'rev-parse','HEAD').strip();git(root,'update-index','--add','--cacheinfo','160000,'+commit+',module')
    (root/'normal.txt').write_text('ordinary mixed change\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert not preview['can_apply'] and preview['reason']=='unsupported_gitlink'
    assert not (Path(config['repositories']['default'])/'normal.txt').exists()
    assert root.exists()
