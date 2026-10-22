"""Disposable Git candidate/base regressions adapted from the S06 research."""
from pathlib import Path
import pytest

import patchbay.jobs.manager as manager_module
from patchbay.jobs.manager import JobManager
from patchbay.workers.integration_state import build_candidate_snapshot
from test_audit_regressions_20261019 import configuration, git


@pytest.mark.parametrize("committed", [False, True])
def test_required_ignored_tracked_addition_is_not_omitted(tmp_path, committed):
    config = configuration(tmp_path)
    repo = Path(config["repositories"]["default"])
    (repo / ".gitignore").write_text("*.generated\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture ignore policy")
    base = git(repo, "rev-parse", "HEAD").strip()

    required = b"required generated implementation\n"
    (repo / "required.generated").write_bytes(required)
    (repo / "scratch.generated").write_bytes(b"untracked ignored scratch\n")
    git(repo, "add", "-f", "required.generated")
    if committed:
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-m", "explicitly tracked generated source")

    real_index = (repo / ".git" / "index").read_bytes()
    snapshot = build_candidate_snapshot(repo, base)
    assert "required.generated" in snapshot.changed_files
    assert "scratch.generated" not in snapshot.changed_files
    assert git(repo, "show", snapshot.tree_sha + ":required.generated", text=False) == required
    assert (repo / ".git" / "index").read_bytes() == real_index


def test_worker_creation_uses_the_already_resolved_base(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    repo = Path(config["repositories"]["default"])
    base_a = git(repo, "rev-parse", "HEAD").strip()
    (repo / "README.md").write_text("later independent source\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "prepare alternate commit")
    base_b = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "reset", "--hard", base_a)  # Disposable fixture only.

    original_repo_factory = manager_module.git.Repo
    switched = False

    class GitProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def worktree(self, *args, **kwargs):
            nonlocal switched
            if args and args[0] == "add" and not switched:
                switched = True
                git(repo, "reset", "--hard", base_b)  # Move after sampled base.
            return self.wrapped.worktree(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    class RepoProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.git = GitProxy(wrapped.git)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def repository(*args, **kwargs):
        return RepoProxy(original_repo_factory(*args, **kwargs))

    monkeypatch.setattr(manager_module.git, "Repo", repository)
    manager = JobManager(config)
    worktree, _branch, recorded_base = manager.create_worker_worktree(
        "s06-pinned-base-fixture", str(repo))
    assert switched, "Fault injection did not reach the intended creation boundary"
    assert recorded_base == base_a
    assert git(worktree, "rev-parse", "HEAD").strip() == base_a


def test_working_tree_precedence_and_literal_exclusions(tmp_path):
    config = configuration(tmp_path); repo = Path(config['repositories']['default'])
    baseline = git(repo, 'rev-parse', 'HEAD').strip()
    (repo/'a[1].txt').write_text('context\n')
    (repo/'a1.txt').write_text('implementation\n')
    (repo/'README.md').write_text('staged\n'); git(repo, 'add', 'README.md')
    (repo/'README.md').write_text('working tree\n')
    index = (repo/'.git/index').read_bytes()
    snap = build_candidate_snapshot(repo, baseline, excluded_paths=['a[1].txt'])
    assert 'a[1].txt' not in snap.changed_files and 'a1.txt' in snap.changed_files
    assert git(repo, 'show', snap.tree_sha+':README.md') == 'working tree\n'
    assert (repo/'.git/index').read_bytes() == index


def test_sparse_index_refuses_without_modifying_real_index(tmp_path):
    from patchbay.workers.integration_state import CandidateSnapshotError
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    baseline=git(repo,'rev-parse','HEAD').strip()
    git(repo,'update-index','--skip-worktree','README.md');index=(repo/'.git/index').read_bytes()
    with pytest.raises(CandidateSnapshotError,match='Sparse'):
        build_candidate_snapshot(repo,baseline)
    assert (repo/'.git/index').read_bytes()==index


def test_changed_gitlink_is_refused(tmp_path):
    from patchbay.workers.integration_state import CandidateSnapshotError
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    baseline=git(repo,'rev-parse','HEAD').strip()
    git(repo,'update-index','--add','--cacheinfo','160000,'+baseline+',module')
    index=(repo/'.git/index').read_bytes()
    with pytest.raises(CandidateSnapshotError,match='gitlink'):
        build_candidate_snapshot(repo,baseline)
    assert (repo/'.git/index').read_bytes()==index


def test_raw_capture_disables_display_helpers_and_rejects_conversion(tmp_path):
    from patchbay.workers.integration_state import CandidateSnapshotError
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    (repo/'.gitattributes').write_text('*.txt text eol=lf diff=hostile\n')
    git(repo,'add','.gitattributes');git(repo,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','attributes')
    baseline=git(repo,'rev-parse','HEAD').strip()
    git(repo,'config','diff.hostile.textconv','this-command-must-never-run')
    git(repo,'config','diff.external','this-command-must-never-run')
    (repo/'plain.txt').write_bytes(b'plain\n')
    assert 'plain.txt' in build_candidate_snapshot(repo,baseline).changed_files
    (repo/'plain.txt').write_bytes(b'converted\r\n')
    with pytest.raises(CandidateSnapshotError,match='conversion'):
        build_candidate_snapshot(repo,baseline)


def test_creation_does_not_inherit_dirty_operator_source(tmp_path):
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    baseline=git(repo,'rev-parse','HEAD').strip();original=(repo/'README.md').read_bytes()
    (repo/'README.md').write_bytes(b'operator dirty bytes\n');index=(repo/'.git/index').read_bytes()
    manager=JobManager(config);root,branch,recorded=manager.create_worker_worktree('s06-dirty-base',str(repo))
    assert recorded==baseline and (root/'README.md').read_bytes()==original
    assert (repo/'README.md').read_bytes()==b'operator dirty bytes\n'
    assert (repo/'.git/index').read_bytes()==index


def test_partial_worktree_allocation_rolls_back_only_its_own_branch(tmp_path,monkeypatch):
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    git(repo,'branch','existing-unrelated');factory=manager_module.git.Repo
    class GitProxy:
        def __init__(self,wrapped):self.wrapped=wrapped
        def worktree(self,*args,**kwargs):
            result=self.wrapped.worktree(*args,**kwargs)
            if args[0]=='add':raise RuntimeError('fixture after successful branch allocation')
            return result
        def __getattr__(self,name):return getattr(self.wrapped,name)
    class RepoProxy:
        def __init__(self,wrapped):self.wrapped=wrapped;self.git=GitProxy(wrapped.git)
        def __getattr__(self,name):return getattr(self.wrapped,name)
    monkeypatch.setattr(manager_module.git,'Repo',lambda *a,**kw:RepoProxy(factory(*a,**kw)))
    manager=JobManager(config)
    with pytest.raises(ValueError):manager.create_worker_worktree('allocation-fault',str(repo))
    assert not (manager.worker_worktrees_dir()/'worker-allocation-fault').exists()
    assert not git(repo,'branch','--list','codex/worker-allocation-fault').strip()
    assert git(repo,'branch','--list','existing-unrelated').strip()


def test_new_snapshot_ref_cannot_overwrite_an_existing_generation(tmp_path):
    from patchbay.workers.integration_state import create_snapshot_commit,CandidateSnapshotError
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    base=git(repo,'rev-parse','HEAD').strip();tree=git(repo,'rev-parse','HEAD^{tree}').strip()
    ref='refs/patchbay/fixture-snapshot'
    first=create_snapshot_commit(repo,tree_sha=tree,parent_revision=base,ref_name=ref)
    with pytest.raises(CandidateSnapshotError):
        create_snapshot_commit(repo,tree_sha=tree,parent_revision=base,ref_name=ref)
    assert git(repo,'rev-parse',ref).strip()==first


def test_readback_disagreement_removes_only_newly_owned_workspace(tmp_path,monkeypatch):
    from types import SimpleNamespace
    config=configuration(tmp_path);repo=Path(config['repositories']['default']);factory=manager_module.git.Repo
    baseline=git(repo,'rev-parse','HEAD').strip()
    (repo/'later.txt').write_text('later\n');git(repo,'add','later.txt');git(repo,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','later')
    different=git(repo,'rev-parse','HEAD').strip();git(repo,'reset','--hard',baseline)
    class Proxy:
        def __init__(self,wrapped):self.wrapped=wrapped
        @property
        def head(self):return SimpleNamespace(commit=SimpleNamespace(hexsha=different))
        def __getattr__(self,name):return getattr(self.wrapped,name)
    def repository(path,*a,**kw):
        actual=factory(path,*a,**kw)
        return actual if Path(path).resolve()==repo.resolve() else Proxy(actual)
    monkeypatch.setattr(manager_module.git,'Repo',repository);manager=JobManager(config)
    with pytest.raises(ValueError):manager.create_worker_worktree('readback-fault',str(repo))
    assert not (manager.worker_worktrees_dir()/'worker-readback-fault').exists()
    assert not git(repo,'branch','--list','codex/worker-readback-fault').strip()
    assert git(repo,'rev-parse','HEAD').strip()==baseline


def test_unmerged_index_is_refused_and_preserved(tmp_path):
    import subprocess
    from patchbay.workers.integration_state import CandidateSnapshotError
    config=configuration(tmp_path);repo=Path(config['repositories']['default']);base=git(repo,'rev-parse','HEAD').strip()
    oid=git(repo,'rev-parse','HEAD:README.md').strip()
    rows=f'0 {"0"*40}\tREADME.md\n100644 {oid} 1\tREADME.md\n100644 {oid} 2\tREADME.md\n100644 {oid} 3\tREADME.md\n'
    subprocess.run(['git','update-index','--index-info'],cwd=repo,input=rows,text=True,check=True)
    index=(repo/'.git/index').read_bytes()
    with pytest.raises(CandidateSnapshotError,match='Unmerged'):build_candidate_snapshot(repo,base)
    assert (repo/'.git/index').read_bytes()==index


def test_ignored_tracked_symlink_to_directory_is_captured_literally(tmp_path):
    config=configuration(tmp_path);repo=Path(config['repositories']['default'])
    (repo/'.gitignore').write_text('*.generated\n');git(repo,'add','.gitignore');git(repo,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','ignore')
    base=git(repo,'rev-parse','HEAD').strip();(repo/'directory').mkdir();(repo/'directory'/'source.txt').write_text('source\n')
    (repo/'link.generated').symlink_to('directory',target_is_directory=True);git(repo,'add','-f','link.generated')
    index=(repo/'.git/index').read_bytes();snapshot=build_candidate_snapshot(repo,base)
    assert 'link.generated' in snapshot.changed_files and snapshot.entries['link.generated']['mode']=='120000'
    assert git(repo,'show',snapshot.tree_sha+':link.generated')=='directory'
    assert (repo/'.git/index').read_bytes()==index


@pytest.mark.parametrize('committed,mixed',[(False,False),(True,False),(True,True)])
def test_gitlink_dirt_cannot_be_hidden_by_repository_ignore_policy(tmp_path,committed,mixed):
    from patchbay.workers.integration_state import CandidateSnapshotError,porcelain_paths
    config=configuration(tmp_path);repo=Path(config['repositories']['default']);module=repo/'module';module.mkdir()
    git(module,'init');(module/'source.txt').write_text('original\n');git(module,'add','source.txt');git(module,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','module')
    (repo/'.gitmodules').write_text('[submodule "module"]\n path = module\n url = https://example.invalid/fixture.git\n')
    git(repo,'add','.gitmodules','module');git(repo,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','track gitlink')
    baseline=git(repo,'rev-parse','HEAD').strip();git(repo,'config','diff.ignoreSubmodules','all');git(repo,'config','submodule.module.ignore','all')
    (module/'source.txt').write_text('new module source\n')
    if committed:
        git(module,'add','source.txt');git(module,'-c','user.name=Fixture','-c','user.email=f@example.invalid','commit','-m','new module generation')
    if mixed:(repo/'normal.txt').write_text('normal source change\n')
    assert 'module' not in git(repo,'status','--porcelain')
    assert 'module' in porcelain_paths(repo)
    index=(repo/'.git/index').read_bytes()
    with pytest.raises(CandidateSnapshotError,match='gitlink'):build_candidate_snapshot(repo,baseline)
    assert (repo/'.git/index').read_bytes()==index and module.exists()
