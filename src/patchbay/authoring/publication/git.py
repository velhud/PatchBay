"""Content-bound approval and recoverable, fast-forward-only feature publication."""
from __future__ import annotations

import json
import os
from ..authority import assert_operator
from pathlib import Path

from ..errors import AuthorError, require
from ..feedback.export import Feedback
from ..models import TaskPolicy
from ..persistence.gates import mutation
from ..persistence.control import Control
from ..persistence.locks import file_lock
from ..repositories import Repositories
from ..revisions.git_objects import invoke
from ..revisions.leases import Leases
from ..tasks import Tasks
from ..util import canonical, fingerprint, utc_now
from .github import GitHubClient


def network_environment() -> dict:
    # Publisher authority is deliberately separate from candidate execution.
    allowed = ("PATH", "HOME", "USER", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS",
               "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    env["GIT_CONFIG_GLOBAL"] = os.environ.get("GIT_CONFIG_GLOBAL", str(Path.home() / ".gitconfig"))
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def remote_head(git, remote: str, branch: str) -> str | None:
    ref = "refs/heads/" + branch
    observed = invoke(git.root, ["ls-remote", "--heads", "--exit-code", "--", remote, ref],
                      env=network_environment(), check=False, timeout=60)
    if observed.returncode == 2 and not observed.stdout:
        return None
    require(observed.returncode == 0, "PUBLICATION_UNKNOWN", "Could not read the exact remote branch")
    rows = [line.split() for line in observed.stdout.decode("ascii").splitlines() if line.strip()]
    exact = [row[0] for row in rows if len(row) == 2 and row[1] == ref]
    require(len(exact) <= 1, "PUBLICATION_UNKNOWN", "Remote branch lookup was ambiguous")
    return exact[0] if exact else None


class Publisher:
    def __init__(self, store, *, github=None):
        self.store = store
        self.tasks, self.repositories, self.leases = Tasks(store), Repositories(store), Leases(store)
        self.github = github

    @staticmethod
    def _remote_authority(policy):
        require(policy.publication.allow_external_ci or policy.publication.remote_effects == "controlled_no_automation",
                "POLICY_DENIED", "Remote publication requires downstream-effect consent or an explicit operator-controlled no-automation destination policy")

    def _candidate(self, task_id: str, revision: int) -> tuple[dict, dict, TaskPolicy, dict]:
        task = self.tasks.get(task_id)
        Control(self.store).assert_granted(task_id)
        require(task["current_revision"] == revision, "STALE_APPROVAL", "Only the current task candidate can receive new publication authority")
        candidate = self.tasks.revision(task_id, revision)
        policy = TaskPolicy.model_validate(task["policy"])
        require(policy.publication.enabled, "POLICY_DENIED", "Publication is not enabled for this task")
        require(not policy.publication.draft_pr, "POLICY_DENIED", "PR creation is disabled; use exact authorized Git publication without a PR")
        require(not policy.publication.signing_required, "PUBLICATION_BLOCKED", "This release cannot produce the repository's required signed candidate commit")
        unresolved = self.store.one("SELECT 1 FROM runs WHERE task_id=? AND (state IN ('launching','running','cancelling','unknown_execution') OR cleanup='unresolved') LIMIT 1", (task_id,), required=False)
        require(not unresolved, "EXECUTION_UNKNOWN", "Task execution or cleanup must be resolved before publication")
        feedback = Feedback(self.store).get(task_id, revision=revision)
        require(feedback["classification"] == "READY_FOR_REVIEW" and feedback["receipt_sha"],
                "CHECKS_INCOMPLETE", "The exact candidate has no current passing canonical receipt")
        require(feedback["verification_receipt"]["source_unchanged"] is True, "EVIDENCE_INVALID", "Source identity was not preserved during checks")
        return task, candidate, policy, feedback

    def preview(self, task_id: str, revision: int) -> dict:
        task, candidate, policy, feedback = self._candidate(task_id, revision)
        repo = self.repositories.get(task["repository_id"])
        require(repo["remote"], "PUBLICATION_BLOCKED", "No publication remote is registered")
        self._remote_authority(policy)
        branch = policy.publication.branch_prefix + task_id
        require(branch != repo["default_branch"], "POLICY_DENIED", "Publication cannot target the default branch")
        git = self.repositories.git(repo["id"])
        feature_head = remote_head(git, repo["remote"], branch)
        target_head = remote_head(git, repo["remote"], repo["default_branch"])
        require(target_head is not None, "PUBLICATION_BLOCKED", "Target branch does not exist at the registered remote")
        if policy.publication.require_target_unchanged:
            require(target_head == task["origin_commit"], "STALE_BASE", "Target branch advanced or task was not based on that target; create a freshly checked integration candidate")
        return {"task_id": task_id, "revision": revision, "commit": candidate["commit_oid"], "tree": candidate["tree_oid"],
                "policy_digest": task["policy_digest"], "receipt_sha": feedback["receipt_sha"], "branch": branch,
                "base_branch": repo["default_branch"], "remote_head": feature_head, "target_head": target_head,
                "remote_effects": policy.publication.remote_effects,
                "publication_ready": True, "merge_authorized": False}

    @mutation
    def approve(self, task_id: str, revision: int, *, actor: str, key: str, token: str, epoch: int) -> dict:
        assert_operator(task_id)
        require(isinstance(actor, str) and 0 < len(actor) <= 200, "INVALID_REQUEST", "An approval actor is required")
        with file_lock(self.store.home / ("locks/task-" + task_id)):
            self.leases.assert_owned(task_id, token, epoch)
            request = {"task_id": task_id, "revision": revision, "actor": actor}
            op, _ = self.store.begin_operation(kind="publication.approve", task_id=task_id, key=key,
                                               fingerprint=fingerprint(request), payload=request, revision=revision)
            completed = self.store.operation(op["id"])
            if completed["state"] == "completed":
                return completed["result"]
            approval_id = "approval_" + op["id"][3:]
            existing = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,), required=False)
            if existing:
                task = self.tasks.get(task_id)
                require(task["policy_digest"] == existing["policy_digest"], "STALE_APPROVAL", "Retained approval belongs to an earlier policy")
                repo = self.repositories.get(task["repository_id"])
                policy = TaskPolicy.model_validate(task["policy"])
                result = {"approval_id": approval_id, "task_id": task_id, "revision": existing["revision"],
                          "commit": existing["commit_oid"], "tree": existing["tree_oid"],
                          "policy_digest": existing["policy_digest"], "receipt_sha": existing["receipt_sha"],
                          "branch": policy.publication.branch_prefix + task_id, "base_branch": repo["default_branch"],
                          "remote_head": existing["remote_head"], "target_head": existing["target_head"],
                          "publication_ready": not bool(existing["revoked"]), "merge_authorized": False,
                          "actor": existing["actor"]}
                self.store.finish(op["id"], "completed", result=result)
                return result
            preview = self.preview(task_id, revision)
            with self.store.transaction() as conn:
                self.leases.assert_owned(task_id, token, epoch, conn=conn)
                current = conn.execute("SELECT current_revision,current_commit FROM tasks WHERE id=?", (task_id,)).fetchone()
                require(current[0] == revision and current[1] == preview["commit"], "STALE_APPROVAL", "Candidate changed before approval")
                conn.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,0,?)",
                             (approval_id, task_id, revision, preview["commit"], preview["tree"], preview["policy_digest"],
                              preview["receipt_sha"], actor, preview["remote_head"], preview["target_head"], utc_now()))
                result = {"approval_id": approval_id, **preview, "actor": actor}
                conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                             (canonical(result).decode(), utc_now(), op["id"]))
                self.store.reference(conn, "approval:" + approval_id, preview["receipt_sha"])
                self.store.event(conn, "publication.approved", task_id, {"approval_id": approval_id, "revision": revision, "commit": preview["commit"]})
            return result

    @mutation
    def apply(self, approval_id: str, *, key: str, token: str, epoch: int) -> dict:
        approval = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        task_id = approval["task_id"]
        assert_operator(task_id)
        self.leases.assert_owned(task_id, token, epoch)
        request = {"approval_id": approval_id, "commit": approval["commit_oid"], "receipt_sha": approval["receipt_sha"]}
        op, _ = self.store.begin_operation(kind="publication.apply", task_id=task_id, key=key,
                                           fingerprint=fingerprint(request), payload=request, revision=approval["revision"])
        repo_id = self.tasks.get(task_id)["repository_id"]
        with file_lock(self.store.home / ("locks/publication-" + repo_id)), file_lock(self.store.home / ("locks/task-" + task_id)):
            observed = self.store.operation(op["id"])
            if observed["state"] == "completed":
                return observed["result"]
            require(observed["state"] not in {"failed", "blocked"}, "OPERATION_FAILED", "This publication was rejected; resolve the recorded cause before a new authorization")
            retained = self.store.one('SELECT * FROM publications WHERE id=?', (op['id'],), required=False)
            effect_possible = bool(retained and (retained['state'] in {'push_attempted', 'pushed', 'published'}
                                   or observed['state'] == 'publication_unknown'))
            try:
                self.leases.assert_owned(task_id, token, epoch)
                if effect_possible:
                    # A retained attempt is observed using its original address,
                    # even if current candidate/policy no longer permits a push.
                    git = self.repositories.git(repo_id)
                    actual = remote_head(git, retained['remote'], retained['branch'])
                    if actual == retained['commit_oid']:
                        return self._finish_previous_attempt(op, approval, retained)
                    require(actual in {None, retained['expected_head']}, 'REMOTE_CHANGED',
                            'The retained publication destination advanced independently; no push was repeated',
                            operation_id=op['id'])
                    raise AuthorError('PUBLICATION_UNKNOWN', 'The previous push is not confirmed; preserve its original intent for operator recovery',
                                      operation_id=op['id'], phase='remote_readback', external_effect_possible=True)
                require(not approval["revoked"], "STALE_APPROVAL", "Approval was revoked")
                task, candidate, policy, feedback = self._candidate(task_id, approval["revision"])
                self._remote_authority(policy)
                require(candidate["commit_oid"] == approval["commit_oid"] and candidate["tree_oid"] == approval["tree_oid"]
                        and task["policy_digest"] == approval["policy_digest"] and feedback["receipt_sha"] == approval["receipt_sha"],
                        "STALE_APPROVAL", "Candidate, policy, or canonical verification changed after review")
                repo = self.repositories.get(repo_id)
                require(repo["remote"], "PUBLICATION_BLOCKED", "Registered remote is unavailable")
                branch = policy.publication.branch_prefix + task_id
                require(branch != repo["default_branch"], "POLICY_DENIED", "Default-branch publication is not allowed")
                git = self.repositories.git(repo_id)
                unresolved = self.store.one("""SELECT p.id FROM publications p JOIN operations o ON o.id=p.id
                    WHERE p.task_id=? AND p.id<>? AND
                    (o.state='publication_unknown' OR p.state IN ('push_attempted','pushed')) LIMIT 1""",
                    (task_id, op['id']), required=False)
                require(not unresolved, 'PUBLICATION_UNKNOWN', 'Recover the retained publication before creating a successor push',
                        operation_id=unresolved['id'] if unresolved else None, external_effect_possible=False)
                target_head = remote_head(git, repo["remote"], repo["default_branch"])
                if policy.publication.require_target_unchanged:
                    require(target_head == approval["target_head"], "STALE_BASE", "The target moved after approval")
                current = remote_head(git, repo["remote"], branch)
                require(current in {approval["remote_head"], approval["commit_oid"]}, "REMOTE_CHANGED", "Feature branch advanced independently; it will not be overwritten")
                with self.store.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO publications VALUES(?,?,?,?,?,?,?,'prepared',NULL,?,?)",
                                 (op["id"], approval_id, task_id, approval["commit_oid"], branch, repo["remote"], approval["remote_head"], utc_now(), utc_now()))
                    conn.execute("UPDATE operations SET state='running',phase='remote_preflight',updated=? WHERE id=?", (utc_now(), op["id"]))
                    self.store.event(conn, "publication.preflight_started", task_id,
                                     {"operation_id": op["id"], "phase": "remote_preflight"})
                if current != approval["commit_oid"]:
                    if current:
                        # Fetch only this object into the author mirror, never the operator checkout.
                        with file_lock(self.store.home / ("locks/git-" + repo_id)):
                            git.run(["fetch", "--no-tags", "--no-recurse-submodules", "--", repo["remote"], current], env=network_environment(), timeout=120)
                        ancestor = invoke(git.root, ["merge-base", "--is-ancestor", current, approval["commit_oid"]], check=False)
                        require(ancestor.returncode == 0, "REMOTE_CHANGED", "Publication would rewrite remote history")
                    self.leases.assert_owned(task_id, token, epoch)
                    Control(self.store).assert_granted(task_id)
                    ref = "refs/heads/" + branch
                    # An explicit expected-head lease closes the read/push race.
                    # The ancestry gate above forbids non-fast-forward history rewrites.
                    lease = "--force-with-lease=" + ref + ":" + (current or "")
                    with self.store.transaction() as conn:
                        self.leases.assert_owned(task_id, token, epoch, conn=conn)
                        Control(self.store).assert_granted(task_id, conn=conn)
                        current_approval = conn.execute("SELECT revoked FROM approvals WHERE id=?", (approval_id,)).fetchone()
                        require(current_approval and not current_approval["revoked"], "STALE_APPROVAL", "Approval was revoked before push intent")
                        current_task = conn.execute("SELECT current_commit,policy_digest FROM tasks WHERE id=?", (task_id,)).fetchone()
                        require(current_task["current_commit"] == approval["commit_oid"] and current_task["policy_digest"] == approval["policy_digest"],
                                "STALE_APPROVAL", "Candidate or policy changed before push intent")
                        conn.execute("UPDATE publications SET state='push_attempted',updated=? WHERE id=?", (utc_now(), op['id']))
                        conn.execute("UPDATE operations SET phase='push_attempted',updated=? WHERE id=?", (utc_now(), op['id']))
                        self.store.event(conn, 'publication.push_attempted', task_id, {'operation_id': op['id']})
                    effect_possible = True
                    attempt = invoke(git.root, ["push", "--porcelain", lease, "--", repo["remote"], approval["commit_oid"] + ":" + ref],
                                     env=network_environment(), check=False, timeout=120)
                    after = remote_head(git, repo["remote"], branch)
                    require(after == approval["commit_oid"], "PUBLICATION_UNKNOWN", "Push did not yield a confirmed exact remote commit", exit_code=attempt.returncode)
                effect_possible = True
                with self.store.transaction() as conn:
                    conn.execute("UPDATE publications SET state='pushed',updated=? WHERE id=?", (utc_now(), op["id"]))
                    conn.execute("UPDATE operations SET phase='pushed',updated=? WHERE id=?", (utc_now(), op["id"]))
                    self.store.event(conn, "publication.pushed", task_id,
                                     {"operation_id": op["id"], "phase": "pushed"})
                pr, ci = None, {"state": "not_configured"}
                if policy.publication.github_repository:
                    client = self.github or GitHubClient(policy.publication.github_repository)
                    try:
                        ci = client.ci(approval["commit_oid"], policy.publication.required_ci)
                    except AuthorError as exc:
                        ci = {"state": "unknown", "error": exc.as_dict()}
                result = {"task_id": task_id, "revision": approval["revision"], "commit": approval["commit_oid"],
                          "tree": approval["tree_oid"], "branch": branch, "pushed": True, "pull_request": pr,
                          "ci": ci, "merged": False, "receipt_sha": approval["receipt_sha"],
                          "reconciled_previous_attempt": False, "historical": False}
                with self.store.transaction() as conn:
                    conn.execute("UPDATE publications SET state='published',pull_request=?,updated=? WHERE id=?",
                                 (canonical(pr).decode() if pr else None, utc_now(), op["id"]))
                    self.store.event(conn, "publication.completed", task_id,
                                     {"operation_id": op["id"], "phase": "published"})
                self.store.finish(op["id"], "completed", result=result)
                return result
            except AuthorError as exc:
                exc.details.update(phase='remote_effect_or_readback' if effect_possible else 'pre_effect',
                                   external_effect_possible=effect_possible)
                error = exc.as_dict()
                self.store.finish(op["id"], "publication_unknown" if effect_possible else "blocked", error=error)
                raise
            except Exception:
                # A lost subprocess reply may follow a successful remote push.
                # Keep the admitted identity recoverable; finish preserves any
                # already committed terminal outcome. Explicit same-key replay
                # performs exact remote readback before considering another push.
                self.store.finish(op["id"], "publication_unknown" if effect_possible else "blocked", error={
                    "code": "PUBLICATION_UNKNOWN",
                    "message": "Publication outcome requires exact remote readback" if effect_possible else 'Publication stopped before a push attempt',
                    'details': {'phase': 'remote_effect_or_readback' if effect_possible else 'pre_effect',
                                'external_effect_possible': effect_possible},
                })
                raise

    def _finish_previous_attempt(self, operation, approval, publication):
        """Record an observed past effect without granting fresh publication/CI."""
        task = self.tasks.get(approval['task_id'])
        result = {'task_id': approval['task_id'], 'revision': approval['revision'],
                  'commit': publication['commit_oid'], 'tree': approval['tree_oid'],
                  'branch': publication['branch'], 'pushed': True, 'pull_request': None,
                  'ci': {'state': 'not_requested_during_recovery'}, 'merged': False,
                  'receipt_sha': approval['receipt_sha'], 'reconciled_previous_attempt': True,
                  'historical': task['current_commit'] != approval['commit_oid'] or
                                task['policy_digest'] != approval['policy_digest']}
        with self.store.transaction() as conn:
            conn.execute("UPDATE publications SET state='published',updated=? WHERE id=?", (utc_now(), operation['id']))
            self.store.event(conn, 'publication.readback_confirmed', approval['task_id'], {'operation_id': operation['id']})
        self.store.finish(operation['id'], 'completed', result=result)
        return result
