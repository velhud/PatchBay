"""Actual no-model S10 restart provenance regressions; raw brief logging stays off."""
from pathlib import Path
import hashlib
import pytest
from patchbay.jobs.manager import JobManager
from patchbay.workers.runtime import WorkerRuntime
from test_audit_regressions_20261019 import completed_worker, complete, NoModelExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("preview_before_reload", [False, True])
async def test_new_review_target_keeps_admitted_brief_identity(tmp_path, preview_before_reload):
    config, manager, runtime, job = await completed_worker(tmp_path)
    assert job.prompt_artifact is None
    assert job.prompt, "The fixture must admit a nonempty exact worker prompt"
    expected = hashlib.sha256(job.prompt.encode("utf-8")).hexdigest()
    root = Path(job.worktree_path)
    (root / "review-provenance.txt").write_text("same source\n", encoding="utf-8")
    complete(manager, job)
    before = None
    if preview_before_reload:
        before = (await runtime.inspect_worker(worker="Luna", view="integration_preview"))["review_target"]
        assert before["contract_sha256"] == expected
    reloaded_manager = JobManager(config)
    restored = reloaded_manager.get_job(job.job_id)
    assert restored is not None and restored.prompt == "", "Do not restore raw prompt text just to pass"
    reloaded = WorkerRuntime(config, reloaded_manager, NoModelExecutor())
    after = (await reloaded.inspect_worker(worker="Luna", view="integration_preview"))["review_target"]
    assert after["contract_sha256"] == expected
    if before is not None:
        assert after["candidate_id"] == before["candidate_id"]
        assert after["tree_sha"] == before["tree_sha"]
        old = await reloaded.inspect_worker(worker="Luna", view="file",
                    file_path="review-provenance.txt", candidate_id=before["candidate_id"])
        assert old["text"] == "same source" and old["mutable_workspace"] is False


def test_admission_keeps_utf8_identity_without_private_body_logging(tmp_path):
    import json
    from test_job_manager_persistence import make_config
    from patchbay.evidence import job_prompt_logging_enabled
    config=make_config(tmp_path);assert not job_prompt_logging_enabled(config)
    manager=JobManager(config);brief='Private exact brief: π 漢字\nsecond line\n'
    job_id=manager.create_job('plan',brief,config['repositories']['default'])
    path=manager.job_state_dir/(job_id+'.json');record=json.loads(path.read_text())
    assert 'prompt' not in record and brief not in path.read_text()
    assert record['prompt_sha256']==hashlib.sha256(brief.encode()).hexdigest()
    assert record['prompt_bytes']==len(brief.encode()) and record['prompt_artifact'] is None
    restored=JobManager(config).get_job(job_id)
    assert restored.prompt=='' and restored.prompt_sha256==record['prompt_sha256']


def test_empty_admitted_brief_is_known_not_legacy_unknown(tmp_path):
    from test_job_manager_persistence import make_config
    config=make_config(tmp_path);manager=JobManager(config)
    job_id=manager.create_job('plan','',config['repositories']['default'])
    restored=JobManager(config).get_job(job_id)
    assert restored.prompt_sha256==hashlib.sha256(b'').hexdigest() and restored.prompt_bytes==0


def test_invalid_utf8_refuses_before_worktree_or_durable_admission(tmp_path,monkeypatch):
    from test_job_manager_persistence import make_config
    config=make_config(tmp_path);manager=JobManager(config)
    def forbidden(*a,**k):raise AssertionError('Cannot allocate before valid brief identity')
    monkeypatch.setattr(manager,'_create_worktree',forbidden)
    with pytest.raises(ValueError,match='UTF-8'):
        manager.create_job('apply','bad\ud800',config['repositories']['default'])
    assert manager.jobs=={} and not list(manager.job_state_dir.glob('*.json'))


@pytest.mark.asyncio
@pytest.mark.parametrize('retained',[False,True])
async def test_legacy_brief_uses_verified_target_or_explicit_unknown(tmp_path,retained):
    import json
    config,manager,runtime,job=await completed_worker(tmp_path)
    (Path(job.worktree_path)/'legacy.txt').write_text('retained source\n');complete(manager,job)
    expected=hashlib.sha256(job.prompt.encode()).hexdigest();before=None
    if retained:
        before=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    path=manager.job_state_dir/(job.job_id+'.json');record=json.loads(path.read_text())
    record.pop('prompt_sha256');record.pop('prompt_bytes')
    if retained:
        # Preserve the exact old descriptor/ID; emulate the pre-S10 optional field shape.
        record['options']['_worker_review_targets'][before['candidate_id']].pop('contract_identity')
        before=record['options']['_worker_review_targets'][before['candidate_id']].copy()
    path.write_text(json.dumps(record))
    reopened_manager=JobManager(config);reopened=WorkerRuntime(config,reopened_manager,NoModelExecutor())
    after=(await reopened.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    assert reopened_manager.get_job(job.job_id).prompt==''
    if retained:
        assert after==before and after['contract_sha256']==expected
        saved=json.loads(path.read_text())['options']['_worker_review_targets'][before['candidate_id']]
        assert saved==before
        read=await reopened.inspect_worker(worker='Luna',view='file',file_path='legacy.txt',candidate_id=before['candidate_id'])
        assert read['text']=='retained source' and not read['mutable_workspace']
    else:
        assert after['contract_sha256'] is None and after['contract_identity']['state']=='legacy_unknown'
        assert after['contract_sha256']!=hashlib.sha256(b'').hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize('sha,count',[('bad',3),(None,4),('f'*64,None),('f'*64,-1),('f'*64,True),('f'*64,0),(hashlib.sha256(b'').hexdigest(),1)])
async def test_malformed_brief_metadata_refuses_new_target(tmp_path,sha,count):
    from patchbay.workers.integration_state import CandidateSnapshotError
    config,manager,runtime,job=await completed_worker(tmp_path);complete(manager,job)
    job.prompt_sha256=sha;job.prompt_bytes=count
    snapshot=runtime._candidate_snapshot([job])
    with pytest.raises(CandidateSnapshotError) as error:runtime._retain_review_target([job],snapshot)
    assert error.value.reason=='brief_identity_invalid'
    assert not (job.options or {}).get('_worker_review_targets')


@pytest.mark.asyncio
async def test_changed_source_and_turn_still_change_target_without_rewriting_old_source(tmp_path):
    config,manager,runtime,job=await completed_worker(tmp_path)
    source=Path(job.worktree_path)/'turn.txt';source.write_text('version one\n');complete(manager,job)
    first=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    source.write_text('version two\n')
    second=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    assert second['candidate_id']!=first['candidate_id'] and second['contract_sha256']==first['contract_sha256']
    await runtime.message_worker(worker='Luna',message='A distinct next brief.')
    following=runtime._resolve_worker('Luna')[-1];complete(manager,following)
    third=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    assert third['turn_job_id']!=first['turn_job_id'] and third['contract_sha256']!=first['contract_sha256']
    old=await runtime.inspect_worker(worker='Luna',view='file',file_path='turn.txt',candidate_id=first['candidate_id'])
    assert old['text']=='version one' and not old['mutable_workspace']


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption',['descriptor','anchor'])
@pytest.mark.parametrize('legacy',[False,True])
async def test_legacy_digest_cannot_be_recovered_from_corrupt_retained_target(tmp_path,corruption,legacy):
    import json
    from patchbay.workers.integration_state import CandidateSnapshotError
    from test_audit_regressions_20261019 import git
    config,manager,runtime,job=await completed_worker(tmp_path);complete(manager,job)
    target=(await runtime.inspect_worker(worker='Luna',view='integration_preview'))['review_target']
    path=manager.job_state_dir/(job.job_id+'.json');record=json.loads(path.read_text())
    if legacy:
        record.pop('prompt_sha256');record.pop('prompt_bytes')
    if corruption=='descriptor':record['options']['_worker_review_targets'][target['candidate_id']]['contract_sha256']='f'*64
    else:git(Path(job.worktree_path),'update-ref','-d',target['snapshot_ref'])
    path.write_text(json.dumps(record))
    reopened=WorkerRuntime(config,JobManager(config),NoModelExecutor());jobs=reopened._resolve_worker('Luna')
    with pytest.raises(CandidateSnapshotError) as error:reopened._retain_review_target(jobs,reopened._candidate_snapshot(jobs))
    assert error.value.reason=='brief_identity_invalid'
    assert json.loads(path.read_text())['options']['_worker_review_targets']==record['options']['_worker_review_targets']
