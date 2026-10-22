"""Disposable local Git publication faults; synthetic receipts test mechanics only."""
import os
import subprocess

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.models import TaskPolicy
from patchbay.authoring.execution.receipts import record_receipt
from patchbay.authoring.publication.git import Publisher
from patchbay.authoring.revisions import git_objects
from conftest import git
from test_core import package_for, submit
from test_fault_boundaries import _seed_check_run, _synthetic_receipt


@pytest.mark.parametrize('fault', ['push_timeout', 'readback_timeout', 'policy_changed', 'concurrent_replay', 'remote_moved', 'preflight_timeout', 'preflight_denial'])
def test_typed_timeout_after_push_keeps_original_operation_recoverable(
    setup_author, tmp_path, monkeypatch, fault
):
    service, source, original_repo, _, _ = setup_author
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    remote = tmp_path / "only-disposable-remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    git(source, "push", str(remote), "main:main")

    policy = TaskPolicy.model_validate(original_repo["policy"])
    policy.publication.enabled = True
    policy.publication.remote_effects = "controlled_no_automation"
    repo = service.repositories.register(
        "s05-timeout-fixture", source, policy, remote=str(remote)
    )
    task = service.tasks.create(repo["id"], "Typed timeout mechanics", key="task")
    lease = service.leases.claim(task["id"])
    candidate = submit(
        service, task,
        package_for(service, task, tmp_path,
                    {"app.py": "def add(a,b): return a+b\n"}),
        lease,
    )
    run, current = _seed_check_run(service, task, key="synthetic-only")
    record_receipt(
        service.store, run["id"],
        _synthetic_receipt(service, current, run, passed=True),
    )
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    publisher = Publisher(service.store)
    approval = publisher.approve(
        task["id"], 1, actor="fixture-operator", key="approval", **authority
    )

    if fault == 'preflight_denial':
        from patchbay.authoring.policy import Policies
        policy.publication.enabled = False
        Policies(service.store).update(task['id'], policy, expected_digest=current['policy_digest'],
                                      actor='fixture-operator', key='disable-before-push')
    original_run = git_objects.subprocess.run
    pushes = []
    fault_enabled = {"value": True}

    def lose_ack_at_lower_wrapper(command, *args, **kwargs):
        # Global Git durability options precede -C. Match the actual subcommand
        # without pinning unrelated option positions; still inject after the
        # real disposable push, preserving the original recovery oracle.
        subcommand = None
        if isinstance(command, (list, tuple)) and command and command[0] == "git" and "-C" in command:
            position = command.index("-C") + 2
            if position < len(command):
                subcommand = command[position]
        if fault_enabled['value'] and subcommand == 'ls-remote':
            if fault == 'preflight_timeout' or (fault == 'readback_timeout' and pushes):
                raise subprocess.TimeoutExpired(command, kwargs.get('timeout', 60))
        result = original_run(command, *args, **kwargs)
        is_push = subcommand == "push"
        if is_push:
            pushes.append(tuple(command))
            # The real disposable push has completed. The wrapper must not turn
            # this synthetic lost acknowledgement into a terminal rejection.
            if fault_enabled["value"] and fault != "readback_timeout":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 120))
        return result

    monkeypatch.setattr(git_objects.subprocess, "run", lose_ack_at_lower_wrapper)
    arguments = dict(approval_id=approval["approval_id"],
                     key="same-publication-intent", **authority)
    try:
        publisher.apply(**arguments)
    except AuthorError as error:
        if fault in {'preflight_timeout', 'preflight_denial'}:
            assert error.details['external_effect_possible'] is False
        else:
            assert error.details['external_effect_possible'] is True
        # The oracle is durable effect state, not one particular public code.
        pass
    operation = service.store.one(
        "SELECT * FROM operations WHERE task_id=? AND kind='publication.apply'",
        (task["id"],),
    )
    if fault in {'preflight_timeout', 'preflight_denial'}:
        assert not pushes and operation['state']=='blocked'
        assert git(remote, 'for-each-ref', '--format=%(refname)', 'refs/heads/author/') == ''
        return
    assert operation["state"] in {"publication_unknown", "completed"}
    assert len(pushes) == 1

    fault_enabled["value"] = False
    if fault == 'policy_changed':
        from patchbay.authoring.policy import Policies
        policy.publication.enabled = False
        Policies(service.store).update(task['id'], policy, expected_digest=current['policy_digest'],
                                      actor='fixture-operator', key='disable-after-push')
    if fault == 'remote_moved':
        branch=policy.publication.branch_prefix+task['id']
        git(remote, 'update-ref', 'refs/heads/'+branch, task['origin_commit'])
        with pytest.raises(AuthorError) as error: publisher.apply(**arguments)
        assert error.value.code=='REMOTE_CHANGED' and error.value.details['external_effect_possible']
        assert service.store.operation(operation['id'])['state']=='publication_unknown' and len(pushes)==1
        return
    if fault == 'concurrent_replay':
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:publisher.apply(**arguments),range(2)))
        assert results[0]==results[1]
        recovered=results[0]
    else:
        recovered = publisher.apply(**arguments)
    assert recovered['reconciled_previous_attempt'] and recovered['historical']==(fault=='policy_changed')
    assert len(pushes) == 1, "Recovery must read back the prior push, not repeat it"
    assert recovered["pushed"] is True and recovered["merged"] is False
    assert recovered["commit"] == candidate["commit"]
    assert git(remote, "rev-parse", "refs/heads/" + recovered["branch"]) == candidate["commit"]
    assert git(remote, "rev-parse", "refs/heads/main") == task["origin_commit"]
    assert git(source, "rev-parse", "HEAD") == task["origin_commit"]
    assert service.store.one(
        "SELECT COUNT(*) AS n FROM operations WHERE task_id=? AND kind='publication.apply'",
        (task["id"],),
    )["n"] == 1
