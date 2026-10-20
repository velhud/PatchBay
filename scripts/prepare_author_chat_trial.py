#!/usr/bin/env python3
"""Prepare a disposable Author Mode artifact fixture for a manual Chat trial.

This script prepares local state only. It never runs the approved check, calls
ChatGPT, configures a client route, or records qualification evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.catalog import identity as catalog_identity
from patchbay.authoring.models import Check, Profile, RuntimeLimits, TaskPolicy
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.service import AuthorService


TASK_GOAL = (
    "Repair the addition behavior in app.py so the supplied unittest passes. "
    "Keep the change limited to the broken behavior. Done means the authorized "
    "unit check passes and the returned artifact changes only app.py."
)
CHECK_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return result.stdout.decode().strip()


def _write_json(path: Path, value: object, *, mode: int = 0o644) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)


def _source_binding(repo_root: Path) -> dict:
    """Bind the prepared records to HEAD and all visible nonignored worktree files."""
    commit = _git(repo_root, "rev-parse", "HEAD")
    commit_tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    status = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    entries = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout.split(b"\0")
    digest = hashlib.sha256()
    for raw_path in sorted(item for item in entries if item):
        path = repo_root / os.fsdecode(raw_path)
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        if path.is_symlink():
            mode, data = b"symlink", os.fsencode(os.readlink(path))
        elif path.is_file():
            mode = b"executable" if os.access(path, os.X_OK) else b"regular"
            data = path.read_bytes()
        else:
            mode, data = b"missing", b""
        digest.update(mode)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return {"commit": commit, "commit_tree": commit_tree, "worktree_dirty": bool(status),
            "worktree_sha256": digest.hexdigest()}


def _profile_template(source_commit: str) -> dict:
    return {
        "schema_version": 1,
        "qualification_status": "NOT_RUN",
        "prepared_source_commit": source_commit,
        "profile_id": None,
        "observations": {name: None for name in (
            "account_workspace", "visible_chat_model", "visible_effort", "ordinary_chat_mode",
            "client_version", "plugin_identity", "selection_method", "observed_at")},
        "server_configuration": {name: None for name in (
            "deployed_commit", "installed_entrypoint", "workflow", "principal_id", "role",
            "task_scope", "catalog_profile", "catalog_hash", "instructions_hash", "output_profile",
            "auth_mechanism", "ingress_route", "negotiated_protocol", "task_policy_digest",
            "resource_envelope")},
        "limitations": [
            "Prepared values confer no qualification or task authority.",
            "Record actual deployed source after Linux repairs; do not reuse a superseded source pin.",
        ],
    }


def _evidence_template(source_commit: str, repo_root: Path) -> dict:
    register = json.loads((repo_root / "docs/authoring/reliability-acceptance-tests.json").read_text())
    return {"schema_version": 1, "runs": {
        item["id"]: {"status": "NOT_RUN", "commit": source_commit, "command": "",
                     "reason": "Prepared specification; no scenario execution recorded.",
                     "artifacts": [], "execution_surface": None, "profile_id": None}
        for item in register
    }}


def _capture_template(repo_root: Path) -> str:
    corpus = json.loads((repo_root / "docs/authoring/golden-prompts.json").read_text())
    rows = []
    for case in corpus["cases"]:
        for variant in ("full-text+structured", "concise+structured"):
            rows.append({"case_id": case["id"], "variant": variant, "status": "NOT_RUN",
                         "input_raw": "", "output_raw": "", "tool_ids": [], "required_tool_ids": [],
                         "execution_surface": None, "profile_id": None,
                         "reason": "Template only; capture actual ordinary Chat observations."})
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _prepare(output: Path, repo_root: Path, trial_kit: Path | None = None) -> dict:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"output already exists; refusing to overwrite: {output}")
    if not (repo_root / "src/patchbay/authoring").is_dir():
        raise ValueError(f"repo root does not look like PatchBay source: {repo_root}")
    if trial_kit is not None:
        trial_kit = trial_kit.expanduser().resolve()
        if not trial_kit.is_dir() or any(not (trial_kit / name).is_file() for name in
                                         ("profile.json", "qualification-evidence.json", "ab-capture.jsonl")):
            raise ValueError("--trial-kit must contain profile.json, qualification-evidence.json, and ab-capture.jsonl")

    source_binding = _source_binding(repo_root)
    source_commit = source_binding["commit"]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    fixture = output / "fixture"
    source = fixture / "source"
    source.mkdir(parents=True)
    (source / "tests").mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "PatchBay disposable trial")
    _git(source, "config", "user.email", "patchbay-trial@localhost")
    (source / "app.py").write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    (source / "tests/test_app.py").write_text(
        "import unittest\nfrom app import add\n\n"
        "class AdditionTest(unittest.TestCase):\n"
        "    def test_addition(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
    (source / "README.md").write_text(
        "Disposable addition fixture. app.py intentionally has one failing behavior.\n",
        encoding="utf-8")
    _git(source, "add", "app.py", "tests/test_app.py", "README.md")
    _git(source, "commit", "-m", "intentionally broken addition fixture")
    fixture_commit = _git(source, "rev-parse", "HEAD")

    home = output / "author-home"
    limits = RuntimeLimits(minimum_free_bytes=0, max_jobs=2, cpu_cores=1,
                           memory_mb=1024, total_storage_bytes=256 * 1024**2,
                           snapshot_bytes=8 * 1024**2)
    Store.initialize(home, limits)
    service = AuthorService(home)
    profile = Profile(
        id="ordinary-chat-canary",
        backend="trusted-local",
        memory_mb=256,
        disk_mb=128,
        cpu=0.25,
        checks=[Check(id="unit", argv=CHECK_ARGV, timeout_seconds=30)],
    )
    policy = TaskPolicy(profile=profile, writable_paths=["app.py"], auto_check=False,
                        allow_background_checks=True)
    repo = service.repositories.register("ordinary-chat-fixture", source, policy,
                                         default_branch="main", key="trial-register")
    task = service.tasks.create(repo["id"], TASK_GOAL, key="trial-task")
    context = service.snapshots.export(task["id"])
    context_path = output / "context.zip"
    shutil.copyfile(service.store.blobs.path(context["bundle_sha"]), context_path)
    context_path.chmod(0o644)

    access = Access(service.store)
    principals = []
    for principal_id, role, key in (
        ("trial-author", "author", "trial-principal-author"),
        ("trial-reader", "reader", "trial-principal-reader"),
    ):
        issued = access.issue(principal_id, role, [task["id"]], key=key)
        principals.append({"principal_id": principal_id, "role": role,
                           "task_ids": [task["id"]], "credential_id": issued["credential_id"],
                           "token": issued["token"]})
    private_dir = output / "private"
    private_dir.mkdir(mode=0o700)
    _write_json(private_dir / "principals.json", {"principals": principals}, mode=0o600)

    if trial_kit is not None:
        shutil.copyfile(trial_kit / "profile.json", output / "profile.json")
        profile_data = json.loads((output / "profile.json").read_text())
        profile_data["prepared_source_commit"] = source_commit
        _write_json(output / "profile.json", profile_data)
        shutil.copyfile(trial_kit / "qualification-evidence.json", output / "qualification-evidence.json")
        evidence = json.loads((output / "qualification-evidence.json").read_text())
        for run in evidence.get("runs", {}).values():
            if isinstance(run, dict):
                run["commit"] = source_commit
        _write_json(output / "qualification-evidence.json", evidence)
        shutil.copyfile(trial_kit / "ab-capture.jsonl", output / "ab-capture.jsonl")
        (output / "ab-capture.jsonl").chmod(0o644)
    else:
        _write_json(output / "profile.json", _profile_template(source_commit))
        _write_json(output / "qualification-evidence.json", _evidence_template(source_commit, repo_root))
        (output / "ab-capture.jsonl").write_text(_capture_template(repo_root), encoding="utf-8")
        (output / "ab-capture.jsonl").chmod(0o644)
    runtime_catalog = catalog_identity()
    check_profile = profile.wire()
    _write_json(output / "prepared-identities.json", {
        "preparation_status": "PREPARED_NOT_RUN",
        "source_binding": source_binding,
        "runtime_catalog_identity": runtime_catalog,
        "check_profile": check_profile,
        "task_policy_digest": task["policy_digest"],
        "fixture_commit": fixture_commit,
        "repository_id": repo["id"], "task_id": task["id"],
        "context_id": context["id"], "context_commit": context["commit_oid"],
        "context_bundle_sha256": context["bundle_sha"],
        "author_principal_id": "trial-author", "reader_principal_id": "trial-reader",
        "approved_check": CHECK_ARGV,
        "approved_check_was_run": False,
    })
    instruction = f"""# Ordinary Chat manual artifact trial

This is preparation only. No ordinary Chat call, UI interaction, qualification,
or check execution has happened. Keep all observations unknown until directly
observed in the client and correlated with server receipts.

## Prepared inputs

- Disposable source checkout: `fixture/source/` at `{fixture_commit}`.
- Independent Author home: `author-home/`.
- Task: `{task['id']}`; repository: `{repo['id']}`.
- Immutable exported context: `context.zip`, context `{context['id']}` at
  `{context['commit_oid']}` with SHA-256 `{context['bundle_sha']}`.
- Task-scoped author and reader principals are in `private/principals.json`.
  The file is mode 0600. Do not paste credentials into chat or evidence.
- The only authorized verification is `{ ' '.join(CHECK_ARGV) }` in the
  disposable fixture. It is configured but has not been run. No publication,
  merge, deployment, extra check, or unrelated edit is authorized.

## Manual sequence

1. Configure the actual ordinary Chat client/service route outside this
   preparation script. Record the real account/workspace, visible model and
   effort, client/plugin version, ingress, negotiated protocol, deployed source,
   and catalog/instruction identities in `profile.json`; use null where unknown.
2. Select the intended Author read-only route and use G01 from the checked-in
   `docs/authoring/golden-prompts.json` corpus. Read `app.py` from this exact task/context. Verify the
   returned source pin against `prepared-identities.json`. Do not start a check
   or edit during G01.
3. If a later canary write is explicitly authorized by the operator, use the
   task-scoped author principal, edit only `app.py`, prepare a package from the
   returned context, and submit it once. Then run only the configured `unit`
   check. Record actual tool IDs/arguments, operation IDs, receipts, raw input
   and output, timestamps, and artifact hashes. Stop on unexpected scope or
   route behavior.
4. Preserve exact raw evidence and update `qualification-evidence.json` and
   `ab-capture.jsonl` only for attempts actually observed. Keep every other row
   `NOT_RUN`; do not infer selected model, route, write support, costs, or
   qualification from prepared files or local checks.

The test objective is a correct addition result and a returned change limited
to `app.py`. Read-only route usefulness and actual ordinary Chat execution are
separate evidence. This fixture does not qualify a client or account.
"""
    (output / "operator-instructions.md").write_text(instruction, encoding="utf-8")
    (output / "operator-instructions.md").chmod(0o644)
    return {"output": str(output), "task_id": task["id"], "repository_id": repo["id"],
            "context_id": context["id"], "context_sha256": context["bundle_sha"],
            "fixture_commit": fixture_commit, "prepared_source_commit": source_commit,
            "source_worktree_sha256": source_binding["worktree_sha256"],
            "runtime_sha256": runtime_catalog["runtime_sha256"],
            "catalog_sha256": runtime_catalog["sha256"],
            "qualification_status": "NOT_RUN", "ordinary_chat_invoked": False,
            "approved_check_executed": False,
            "credentials_file": str(private_dir / "principals.json"),
            "credentials_mode": "0600"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trial-kit", type=Path,
                        help="optional directory containing profile.json, qualification-evidence.json, and ab-capture.jsonl")
    args = parser.parse_args(argv)
    try:
        summary = _prepare(args.output_dir, args.repo_root.resolve(), args.trial_kit)
    except Exception as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
