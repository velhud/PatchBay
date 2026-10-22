from __future__ import annotations

import sys

import json
from pathlib import Path

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.publication.git import Publisher, remote_head
from patchbay.authoring.publication.github import GitHubClient
from conftest import git
from test_core import package_for, submit
from test_execution import wait_for


def publishable(setup_author, tmp_path):
    service, source, original_repo, _, _ = setup_author
    remote = tmp_path / "remote.git"; remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(source, "push", str(remote), "main:main")
    policy = TaskPolicy.model_validate(original_repo["policy"])
    policy.publication.enabled = True
    policy.publication.remote_effects = "controlled_no_automation"
    repo = service.repositories.register("publishable", source, policy, remote=str(remote))
    task = service.tasks.create(repo["id"], "Fix addition for publication", key="publish-task")
    lease = service.leases.claim(task["id"])
    accepted = submit(service, task, package_for(service, task, tmp_path, {"app.py": "def add(a,b): return a+b\n"}), lease)
    operation = Checks(service.store).submit(task["id"], revision=1, key="checks", token=lease["token"], epoch=lease["epoch"])
    assert wait_for(service, operation)["state"] == "completed"
    return service, source, repo, task, lease, accepted, remote


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_exact_feature_publication_and_replay_leave_main_unchanged(setup_author, tmp_path):
    s, source, repo, task, lease, accepted, remote = publishable(setup_author, tmp_path)
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    publisher = Publisher(s.store)
    approved = publisher.approve(task["id"], 1, actor="test-operator", key="approve", **authority)
    result = publisher.apply(approved["approval_id"], key="publish", **authority)
    assert result["commit"] == accepted["commit"] and result["tree"] == accepted["tree"]
    assert git(remote, "rev-parse", "refs/heads/" + result["branch"]) == accepted["commit"]
    assert git(remote, "rev-parse", "refs/heads/main") == task["origin_commit"]
    assert git(source, "rev-parse", "HEAD") == task["origin_commit"]
    assert publisher.apply(approved["approval_id"], key="publish", **authority) == result
    assert result["merged"] is False


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_new_revision_invalidates_prior_approval(setup_author, tmp_path):
    s, _, _, task, lease, _, remote = publishable(setup_author, tmp_path)
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    approved = Publisher(s.store).approve(task["id"], 1, actor="operator", key="approve", **authority)
    submit(s, task, package_for(s, task, tmp_path, {"another.txt": "new change"}), lease, "revision-two")
    with pytest.raises(AuthorError) as error: Publisher(s.store).apply(approved["approval_id"], key="publish", **authority)
    assert error.value.code == "STALE_APPROVAL"
    assert git(remote, "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_target_moving_after_approval_blocks_publication(setup_author, tmp_path):
    s, source, _, task, lease, _, remote = publishable(setup_author, tmp_path)
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    approved = Publisher(s.store).approve(task["id"], 1, actor="operator", key="approve", **authority)
    (source / "README.md").write_text("Target advanced independently\n")
    git(source, "add", "README.md"); git(source, "commit", "-m", "target moves")
    git(source, "push", str(remote), "main:main")
    with pytest.raises(AuthorError) as error: Publisher(s.store).apply(approved["approval_id"], key="publish", **authority)
    assert error.value.code == "STALE_BASE"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_lost_push_terminal_response_is_reconciled(setup_author, tmp_path, monkeypatch):
    s, _, _, task, lease, _, remote = publishable(setup_author, tmp_path)
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    publisher = Publisher(s.store)
    approved = publisher.approve(task["id"], 1, actor="operator", key="approve", **authority)
    original = s.store.finish
    def lost(operation_id, state, **kwargs):
        if s.store.one("SELECT kind FROM operations WHERE id=?", (operation_id,))["kind"] == "publication.apply":
            raise RuntimeError("response lost after remote effect")
        return original(operation_id, state, **kwargs)
    monkeypatch.setattr(s.store, "finish", lost)
    with pytest.raises(RuntimeError): publisher.apply(approved["approval_id"], key="publish", **authority)
    before = git(remote, "show-ref")
    monkeypatch.setattr(s.store, "finish", original)
    recovered = publisher.apply(approved["approval_id"], key="publish", **authority)
    assert recovered["pushed"] is True and git(remote, "show-ref") == before


def test_pr_create_response_loss_does_not_create_second_pr():
    client = GitHubClient("example/project", token="fixture")
    state = {"pr": None, "creates": 0}
    commit, branch = "a" * 40, "author/task-fixture"
    def request(method, suffix, data=None):
        if method == "GET": return [state["pr"]] if state["pr"] else []
        if method == "PATCH": return state["pr"]
        state["creates"] += 1
        state["pr"] = {"number": 7, "html_url": "https://github.com/example/project/pull/7", "state": "open", "draft": True,
                       "head": {"ref": branch, "sha": commit, "repo": {"full_name": "example/project"}}, "base": {"ref": "main"}}
        raise AuthorError("PUBLICATION_UNKNOWN", "Lost POST response")
    client.request = request
    first = client.ensure_draft(branch, "main", commit, title="Fixture", body="Fixture")
    second = client.ensure_draft(branch, "main", commit, title="Fixture", body="Fixture")
    assert first["number"] == second["number"] == 7 and state["creates"] == 1


def test_missing_required_ci_is_pending_not_green():
    client = GitHubClient("example/project", token="fixture")
    client.request = lambda method, suffix, data=None: {"check_runs": []} if "check-runs" in suffix else []
    result = client.ci("a" * 40, ["required-unit"])
    assert result["state"] == "pending" and result["missing"] == ["required-unit"]
