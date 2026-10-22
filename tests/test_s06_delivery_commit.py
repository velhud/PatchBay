"""Actual no-model delivery persistence and replay fault regression."""
from pathlib import Path
import pytest

from patchbay.jobs.manager import JobManager
from patchbay.workers.runtime import WorkerRuntime
from patchbay.workers.runtime_integration import WORKER_INTEGRATED_SNAPSHOT_OPTION
from test_audit_regressions_20261019 import (
    NoModelExecutor, completed_worker, complete, git,
)


@pytest.mark.asyncio
async def test_applied_replay_has_a_consistent_delivered_baseline(tmp_path, monkeypatch):
    config, manager, runtime, job = await completed_worker(tmp_path)
    worker = Path(job.worktree_path)
    base = Path(config["repositories"]["default"])
    (worker / "first.txt").write_bytes(b"first accepted implementation\n")
    complete(manager, job)
    preview = await runtime.inspect_worker(worker="Luna", view="integration_preview")

    original = manager.mutate_job_options
    interrupted = False

    def persist_then_interrupt(job_id, mutator):
        nonlocal interrupted
        result = original(job_id, mutator)
        options = dict(manager.get_job(job_id).options or {})
        if not interrupted and options.get("_worker_integrated_at"):
            interrupted = True
            raise RuntimeError("S06 fixture: lost process after first durable delivery update")
        return result

    monkeypatch.setattr(manager, "mutate_job_options", persist_then_interrupt)
    with pytest.raises(RuntimeError, match="S06 fixture"):
        await runtime.integrate_worker(
            worker="Luna", preview_token=preview["preview_token"],
            idempotency_key="s06-one-delivery")
    assert interrupted
    assert (base / "first.txt").read_bytes() == b"first accepted implementation\n"

    reopened_manager = JobManager(config)
    reopened = WorkerRuntime(config, reopened_manager, NoModelExecutor())
    replay = await reopened.integrate_worker(
        worker="Luna", preview_token=preview["preview_token"],
        idempotency_key="s06-one-delivery")
    assert replay["applied"] is True
    retained = reopened_manager.get_job(job.job_id).options
    delivered = retained.get(WORKER_INTEGRATED_SNAPSHOT_OPTION)
    assert delivered, "An applied replay must repair/retain its delivered baseline"
    assert git(worker, "cat-file", "-t", delivered).strip() == "commit"
    assert git(worker, "rev-parse", delivered + "^{tree}").strip() == replay["worker_tree_sha"]
    snapshot = reopened._candidate_snapshot(reopened._resolve_worker("Luna"))
    assert snapshot.changed_files == (), "Already delivered work must not reappear as a new delta"


@pytest.mark.asyncio
async def test_anchor_failure_retains_delivery_and_replay_repairs_without_apply(tmp_path,monkeypatch):
    import patchbay.workers.runtime_integration as owner
    from patchbay.workers.integration_state import CandidateSnapshotError
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'one.txt').write_text('accepted\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    real=owner.create_snapshot_commit
    def fail(*args,**kwargs):raise CandidateSnapshotError('fixture anchor failure')
    monkeypatch.setattr(owner,'create_snapshot_commit',fail)
    result=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='anchor-fault')
    assert result['applied'] and result['baseline_repair_required']
    assert (base/'one.txt').read_text()=='accepted\n'
    assert manager.get_job(job.job_id).options['_worker_delivery_baseline_repair_required']
    cleanup=await runtime.stop_worker(worker='Luna',cleanup_workspace=True)
    assert not cleanup['workspace_cleaned'] and root.exists()
    followup=await runtime.message_worker(worker='Luna',message='Must wait for baseline repair.',idempotency_key='blocked-next-turn')
    assert not followup['accepted'] and followup['reason']=='delivery_baseline_repair_required' and len(manager.jobs)==1
    monkeypatch.setattr(owner,'create_snapshot_commit',real)
    replay=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='anchor-fault')
    assert replay['applied'] and not replay['baseline_repair_required']
    assert runtime._candidate_snapshot(runtime._resolve_worker('Luna')).changed_files==()


@pytest.mark.asyncio
async def test_exact_postimage_reconciliation_rejects_out_of_hunk_drift(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'one.txt').write_text('accepted\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    snap=runtime._candidate_snapshot(runtime._resolve_worker('Luna'));patch,info=runtime._integration_patch_from_snapshot(snap)
    before=runtime._integration_file_fingerprints(str(base),['one.txt'])
    record={'patch':patch,'patch_info':info,'preview':{'changed_files':['one.txt']},
        'pre_apply_file_fingerprints':before,'pre_apply_dirty_fingerprint':runtime._dirty_worktree_fingerprint(str(base))}
    (base/'one.txt').write_text('accepted\nextra unapproved line\n')
    assert runtime._reconcile_applying_token(str(base),record)=='outcome_unknown'
    (base/'one.txt').write_text('accepted\n')
    assert runtime._reconcile_applying_token(str(base),record)=='applied'
    (base/'one.txt').chmod(0o755)
    assert runtime._reconcile_applying_token(str(base),record)=='outcome_unknown'


@pytest.mark.asyncio
async def test_accepted_unrelated_dirt_is_preserved_but_touched_drift_refuses(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'README.md').write_text('worker revision\n');complete(manager,job)
    (base/'unrelated.txt').write_text('operator bytes\n')
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview',accepted_dirty_base=['unrelated.txt'])
    assert preview['can_apply']
    (base/'README.md').write_text('operator touched source\n')
    blocked=await runtime.integrate_worker(worker='Luna',allow_dirty_base=True)
    assert not blocked['applied'] and blocked['apply_check']=='affected_preimage_mismatch'
    assert (base/'unrelated.txt').read_text()=='operator bytes\n'


@pytest.mark.asyncio
async def test_source_drift_after_validation_cannot_substitute_patch_and_base_stays_reserved(tmp_path,monkeypatch):
    from patchbay.repo_locks import RepoMutationLockManager,RepoMutationBusy
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'one.txt').write_text('candidate C\n');complete(manager,job)
    original_preview=runtime._integration_preview
    def drift(*args,**kwargs):
        result=original_preview(*args,**kwargs)
        (root/'one.txt').write_text('later candidate D\n')
        return result
    monkeypatch.setattr(runtime,'_integration_preview',drift)
    original_persist=manager.mutate_job_options;reserved=[]
    def persist(job_id,mutator):
        def wrapped(options):
            result=mutator(options)
            if result.get('_worker_integrated_at'):
                with pytest.raises(RepoMutationBusy):
                    with RepoMutationLockManager(config).hold_sync(str(base),operation='competing-fixture'):
                        pass
                reserved.append(True)
            return result
        return original_persist(job_id,wrapped)
    monkeypatch.setattr(manager,'mutate_job_options',persist)
    result=await runtime.integrate_worker(worker='Luna')
    assert result['applied'] and reserved
    assert (base/'one.txt').read_text()=='candidate C\n'
    assert (root/'one.txt').read_text()=='later candidate D\n'
    assert runtime._candidate_snapshot(runtime._resolve_worker('Luna')).changed_files==('one.txt',)


@pytest.mark.asyncio
async def test_crash_after_anchor_ref_adopts_the_exact_ref_without_reapplying(tmp_path,monkeypatch):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);(root/'one.txt').write_text('accepted\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview');original=manager.mutate_job_options
    def interrupt(job_id,mutator):
        def check(options):
            result=mutator(options)
            if result.get('_worker_integrated_at'):raise RuntimeError('fixture after ref before options')
            return result
        return original(job_id,check)
    monkeypatch.setattr(manager,'mutate_job_options',interrupt)
    with pytest.raises(RuntimeError,match='after ref'):
        await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='ref-interruption')
    ref=runtime._integration_ref_name(runtime._resolve_worker('Luna'));anchored=git(root,'rev-parse',ref).strip()
    reopened_manager=JobManager(config);reopened=WorkerRuntime(config,reopened_manager,NoModelExecutor())
    result=await reopened.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='ref-interruption')
    assert result['applied'] and result['integrated_snapshot_revision']==anchored
    assert reopened._candidate_snapshot(reopened._resolve_worker('Luna')).changed_files==()


@pytest.mark.asyncio
@pytest.mark.parametrize('target_drift',[False,True])
async def test_legacy_split_delivery_repairs_only_with_exact_target_evidence(tmp_path,target_drift):
    config,manager,runtime,job=await completed_worker(tmp_path)
    root=Path(job.worktree_path);base=Path(config['repositories']['default']);(root/'one.txt').write_text('accepted\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    result=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='legacy-split')
    assert result['applied']
    def split(options):
        for key in ['_worker_integrated_snapshot_revision','_worker_integrated_worker_tree','_worker_integrated_snapshot_ref']:options.pop(key,None)
        return options
    manager.mutate_job_options(job.job_id,split)
    if target_drift:(base/'one.txt').write_text('later operator content\n')
    replay=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='legacy-split')
    assert replay['applied']
    assert bool(replay.get('baseline_repair_required'))==target_drift
    if not target_drift:assert runtime._candidate_snapshot(runtime._resolve_worker('Luna')).changed_files==()
    else:
        assert (base/'one.txt').read_text()=='later operator content\n'
        assert not (await runtime.stop_worker(worker='Luna',cleanup_workspace=True))['workspace_cleaned']


@pytest.mark.asyncio
async def test_delivery_preserves_staged_operator_index_and_head(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path);root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'one.txt').write_text('worker\n');complete(manager,job)
    (base/'README.md').write_text('staged operator bytes\n');git(base,'add','README.md')
    index=(base/'.git/index').read_bytes();head=git(base,'rev-parse','HEAD')
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview',accepted_dirty_base=['README.md'])
    result=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='preserve-index',accepted_dirty_base=['README.md'])
    assert result['applied'] and (base/'.git/index').read_bytes()==index and git(base,'rev-parse','HEAD')==head
    assert (base/'README.md').read_text()=='staged operator bytes\n'


@pytest.mark.asyncio
async def test_noncooperating_post_apply_drift_retains_unknown_intent(tmp_path,monkeypatch):
    from patchbay.workers import runtime_legacy as owner
    config,manager,runtime,job=await completed_worker(tmp_path);root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'one.txt').write_text('worker\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview');run=owner.subprocess.run;calls=[]
    def drift(command,*args,**kwargs):
        result=run(command,*args,**kwargs)
        if command[:2]==['git','apply'] and '--check' not in command:
            calls.append(1);(base/'one.txt').write_text('external drift\n')
        return result
    monkeypatch.setattr(owner.subprocess,'run',drift)
    first=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='external-drift')
    assert not first['applied'] and first['reason']=='integration_outcome_unknown'
    replay=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='external-drift')
    assert not replay['applied'] and replay['reason']=='integration_outcome_unknown' and len(calls)==1
    assert root.exists() and (base/'one.txt').read_text()=='external drift\n'


@pytest.mark.asyncio
async def test_old_applied_token_never_rewinds_a_later_delivery(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path);root=Path(job.worktree_path);base=Path(config['repositories']['default'])
    (root/'first.txt').write_text('first\n');complete(manager,job)
    first=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert (await runtime.integrate_worker(worker='Luna',preview_token=first['preview_token'],idempotency_key='first-delivery'))['applied']
    assert (await runtime.message_worker(worker='Luna',message='Make a second independent edit.'))['accepted']
    next_job=runtime._resolve_worker('Luna')[-1];(root/'second.txt').write_text('second\n');complete(manager,next_job)
    second=await runtime.inspect_worker(worker='Luna',view='integration_preview',accepted_dirty_base=['first.txt'])
    assert (await runtime.integrate_worker(worker='Luna',preview_token=second['preview_token'],idempotency_key='second-delivery',accepted_dirty_base=['first.txt']))['applied']
    current=manager.get_job(next_job.job_id).options[WORKER_INTEGRATED_SNAPSHOT_OPTION]
    old=await runtime.integrate_worker(worker='Luna',preview_token=first['preview_token'],idempotency_key='first-delivery')
    assert old['applied'] and old['receipt_scope']=='historical_delivery'
    assert manager.get_job(next_job.job_id).options[WORKER_INTEGRATED_SNAPSHOT_OPTION]==current
    assert runtime._candidate_snapshot(runtime._resolve_worker('Luna')).changed_files==()
    assert (base/'first.txt').read_text()=='first\n' and (base/'second.txt').read_text()=='second\n'
    # The same tree can recur in a later turn without becoming the same receipt.
    first_baseline=old['worker_baseline_revision'];first_patch=old['patch_sha256']
    assert (await runtime.message_worker(worker='Luna',message='Remove only second.txt.'))['accepted']
    third_job=runtime._resolve_worker('Luna')[-1];(root/'second.txt').unlink();complete(manager,third_job)
    third=await runtime.inspect_worker(worker='Luna',view='integration_preview',accepted_dirty_base=['first.txt','second.txt'])
    assert (await runtime.integrate_worker(worker='Luna',preview_token=third['preview_token'],idempotency_key='third-delivery',accepted_dirty_base=['first.txt','second.txt']))['applied']
    latest=manager.get_job(third_job.job_id).options[WORKER_INTEGRATED_SNAPSHOT_OPTION]
    oldest=await runtime.integrate_worker(worker='Luna',preview_token=first['preview_token'],idempotency_key='first-delivery')
    assert oldest['worker_baseline_revision']==first_baseline and oldest['patch_sha256']==first_patch
    assert manager.get_job(third_job.job_id).options[WORKER_INTEGRATED_SNAPSHOT_OPTION]==latest


@pytest.mark.asyncio
async def test_legacy_raw_tree_baseline_is_repaired_to_a_pinned_commit(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path);root=Path(job.worktree_path)
    (root/'one.txt').write_text('accepted\n');complete(manager,job)
    preview=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    accepted=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='raw-tree-legacy')
    tree=accepted['worker_tree_sha']
    manager.mutate_job_options(job.job_id,lambda options:{**options,WORKER_INTEGRATED_SNAPSHOT_OPTION:tree})
    blocked=await runtime.inspect_worker(worker='Luna',view='integration_preview')
    assert not blocked['can_apply']
    followup=await runtime.message_worker(worker='Luna',message='Must not use a raw tree as a future commit.')
    assert not followup['accepted'] and followup['reason']=='delivery_baseline_repair_required'
    replay=await runtime.integrate_worker(worker='Luna',preview_token=preview['preview_token'],idempotency_key='raw-tree-legacy')
    assert replay['applied'] and not replay['baseline_repair_required']
    baseline=manager.get_job(job.job_id).options[WORKER_INTEGRATED_SNAPSHOT_OPTION]
    assert git(root,'cat-file','-t',baseline).strip()=='commit'
    assert runtime._candidate_snapshot(runtime._resolve_worker('Luna')).changed_files==()


@pytest.mark.asyncio
async def test_unkeyed_anchor_failure_keeps_durable_candidate_evidence(tmp_path,monkeypatch):
    import patchbay.workers.runtime_integration as owner
    from patchbay.workers.integration_state import CandidateSnapshotError
    config,manager,runtime,job=await completed_worker(tmp_path)
    (Path(job.worktree_path)/'one.txt').write_text('accepted\n');complete(manager,job)
    def fail(*args,**kwargs):raise CandidateSnapshotError('fixture failed anchor')
    monkeypatch.setattr(owner,'create_snapshot_commit',fail)
    result=await runtime.integrate_worker(worker='Luna')
    assert result['applied'] and result['baseline_repair_required']
    pending=manager.get_job(job.job_id).options['_worker_delivery_pending_anchor']
    assert pending['worker_tree_sha']==result['worker_tree_sha'] and pending['patch_sha256']==result['patch_sha256']
    followup=await runtime.message_worker(worker='Luna',message='Must wait for explicit repair.')
    assert not followup['accepted'] and len(manager.jobs)==1
