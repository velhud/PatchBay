"""Durable, discoverable receipts for deterministic control-plane mutations."""
from __future__ import annotations

import inspect
import json
import uuid
from functools import wraps
from pathlib import Path

from ..errors import require
from ..util import fingerprint
from .locks import file_lock


def replayable(kind: str):
    """Add a caller retry key without creating a second mutation implementation.

    Exports are reconstructed from frozen revision inputs after an interrupted
    request. Their existing content identities make that reconstruction safe.
    Commands with uncertain external effects keep their specialized journals.
    """
    def decorate(method):
        signature = inspect.signature(method)

        @wraps(method)
        def wrapped(self, *args, **kwargs):
            key = kwargs.pop("key", None) or "local-" + uuid.uuid4().hex
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            arguments = {name: value for name, value in bound.arguments.items() if name != "self"}
            request = {name: str(value) if isinstance(value, Path) else value.wire() if hasattr(value, "wire") else value
                       for name, value in arguments.items()}
            task_id = arguments.get("task_id", "")
            if not task_id and arguments.get("operation_id"):
                task_id = self.store.one("SELECT task_id FROM operations WHERE id=?", (arguments["operation_id"],))["task_id"]
            frozen = dict(request)
            if task_id and "revision" in frozen and frozen["revision"] is None:
                frozen["revision"] = self.store.one("SELECT current_revision FROM tasks WHERE id=?", (task_id,))["current_revision"]
            operation, _ = self.store.begin_operation(kind=kind, task_id=task_id, key=key,
                fingerprint=fingerprint(request), payload={"arguments": frozen})
            with file_lock(self.store.home / ("locks/request-" + operation["id"])):
                current = self.store.one("SELECT * FROM operations WHERE id=?", (operation["id"],))
                if current["state"] == "completed":
                    return json.loads(current["result"])
                require(current["state"] not in {"blocked", "failed"}, "OPERATION_FAILED", "Use the recorded request failure before a deliberate new attempt")
                values = json.loads(current["payload"])["arguments"]
                result = method(self, **values)
                if isinstance(result, dict):
                    result = {**result, "request_operation_id": operation["id"]}
                self.store.finish(operation["id"], "completed", result=result)
                return result
        return wrapped
    return decorate
