"""S08 audit proposals adapted and executed as disposable regression fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.tasks import Tasks


RELATION_ERRORS = {"CORRUPT_STATE", "SOURCE_STATE_INCONSISTENT", "GIT_REFERENCE_INVALID"}


def test_s08_backup_rejects_task_pointing_to_missing_commit(setup_author, tmp_path: Path) -> None:
    service, _source, repository, task, _lease = setup_author
    git = service.repositories.git(repository["id"])
    missing = "f" * git.oid_length
    assert missing != task["current_commit"]
    with service.store.transaction() as connection:
        connection.execute("UPDATE tasks SET current_commit=? WHERE id=?", (missing, task["id"]))
    archive = tmp_path / "must-not-be-published.zip"
    with pytest.raises(AuthorError) as failure:
        Maintenance(service.store).backup(archive, key="missing-commit-backup")
    assert failure.value.code in RELATION_ERRORS
    assert not archive.exists()


def test_s08_backup_rejects_sql_tree_mismatch(setup_author, tmp_path: Path) -> None:
    service, _source, repository, task, _lease = setup_author
    git = service.repositories.git(repository["id"])
    missing_tree = "f" * git.oid_length
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE revisions SET tree_oid=? WHERE task_id=? AND number=?",
            (missing_tree, task["id"], task["current_revision"]),
        )
    archive = tmp_path / "must-not-certify-tree.zip"
    with pytest.raises(AuthorError) as failure:
        Maintenance(service.store).backup(archive, key="tree-mismatch-backup")
    assert failure.value.code in RELATION_ERRORS
    assert not archive.exists()


def test_s08_valid_backup_restore_preserves_task_source_relation(setup_author, tmp_path: Path) -> None:
    service, _source, repository, task, _lease = setup_author
    archive = tmp_path / "valid-backup.zip"
    Maintenance(service.store).backup(archive, key="positive-backup")
    restored = Maintenance.restore(archive, tmp_path / "restored-offline")
    recovered = Tasks(restored).get(task["id"])
    assert recovered["current_commit"] == task["current_commit"]
    revision = Tasks(restored).revision(task["id"], task["current_revision"])
    from patchbay.authoring.repositories import Repositories
    git = Repositories(restored).git(repository["id"])
    assert git.tree(revision["commit_oid"]) == revision["tree_oid"]
