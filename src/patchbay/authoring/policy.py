"""Explicit operator policy revisions invalidate old evidence and approvals."""
from __future__ import annotations

import json
from .authority import assert_operator

from .errors import require
from .models import TaskPolicy
from .persistence.control import Control
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .util import canonical, fingerprint, utc_now


class Policies:
    def __init__(self, store):
        self.store = store

    @mutation
    def update(self, task_id: str, policy: TaskPolicy | dict, *, expected_digest: str, actor: str, key: str) -> dict:
        assert_operator(task_id)
        require(isinstance(actor, str) and 0 < len(actor) <= 200, "INVALID_REQUEST", "Policy revision requires an explicit operator actor")
        policy = TaskPolicy.model_validate(policy)
        require(not policy.publication.draft_pr, "POLICY_DENIED", "PR creation is disabled")
        request = {"policy": policy.wire(), "expected_digest": expected_digest, "actor": actor}
        op, _ = self.store.begin_operation(kind="policy.update", task_id=task_id, key=key,
            fingerprint=fingerprint(request), payload=request)
        with file_lock(self.store.home / ("locks/task-" + task_id)), self.store.transaction() as conn:
            completed = conn.execute("SELECT result FROM operations WHERE id=? AND state='completed'", (op["id"],)).fetchone()
            if completed:
                return json.loads(completed["result"])
            Control(self.store).assert_granted(task_id, conn=conn)
            task = conn.execute("SELECT policy,policy_digest FROM tasks WHERE id=?", (task_id,)).fetchone()
            require(task is not None, "NOT_FOUND", "Task is unavailable")
            require(task["policy_digest"] == expected_digest, "STALE_POLICY", "Task policy changed; inspect its current grant before proposing another revision")
            digest = fingerprint(policy.wire())
            conn.execute("UPDATE tasks SET policy=?,policy_digest=?,status='policy_changed',updated=? WHERE id=?",
                         (canonical(policy.wire()).decode(), digest, utc_now(), task_id))
            conn.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task_id,))
            queued = conn.execute("SELECT id FROM runs WHERE task_id=? AND state='queued'", (task_id,)).fetchall()
            for row in queued:
                conn.execute("UPDATE runs SET state='blocked',cleanup='not_required',updated=? WHERE id=?", (utc_now(), row["id"]))
                conn.execute("UPDATE operations SET state='blocked',phase='terminal',error=?,updated=? WHERE id=?",
                    (canonical({"code": "POLICY_CHANGED", "message": "Queued verification belongs to an earlier approved policy"}).decode(), utc_now(), row["id"]))
            result = {"task_id": task_id, "policy_digest": digest, "previous_policy_digest": expected_digest,
                      "actor": actor, "new_verification_required": True, "queued_checks_blocked": len(queued)}
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(result).decode(), utc_now(), op["id"]))
            self.store.event(conn, "policy.updated", task_id, result)
            return result
