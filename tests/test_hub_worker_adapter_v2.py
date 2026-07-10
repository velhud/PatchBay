from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

import pytest

from patchbay.hub.adapters.worker import HubWorkerAdapterV2
from patchbay.hub.broker import OperationBroker, OperationBrokerConflict
from patchbay.hub.operations import public_envelope
from patchbay.hub.protocol_v2 import validate_hub_v2_tool_output
from patchbay.hub.store_v2 import HubStoreV2, semantic_payload_hash
from patchbay.protocol.context import RequestContext


GROUP_ROUTE = {
    "principal_ref": "principal_adapter",
    "work_group_id": "group_alpha",
    "lane_id": "implementation",
    "machine_id": "machine_one",
    "edge_generation": 7,
    "workspace_ref": "workspace_patchbay",
    "workspace_projection_ref": "wsp_patchbay_machine_one",
    "repo_path": "/srv/repos/patchbay",
    "work_group": {
        "work_group_id": "group_alpha",
        "title": "Hub worker adapter",
    },
    "lane": {"lane_id": "implementation", "title": "Implementation"},
    "machine": {"machine_id": "machine_one", "name": "Build VM"},
    "workspace": {
        "workspace_ref": "workspace_patchbay",
        "workspace_projection_ref": "wsp_patchbay_machine_one",
    },
}

WORKER_ROUTE = {
    **GROUP_ROUTE,
    "fleet_worker_ref": "fworker_machine_one_implementer",
    "edge_worker_id": "wrk_edge_123",
    "worker": {
        "fleet_worker_ref": "fworker_machine_one_implementer",
        "edge_worker_id": "wrk_edge_123",
        "name": "Implementer",
        "turn_state": "completed",
        "projection_revision": 41,
    },
}

CONTEXT = RequestContext(
    client_ref="client_adapter",
    owner_ref="owner_adapter",
    chatgpt_session_ref="conversation_adapter",
    work_run_ref="run_adapter",
)


class RecordingRuntime:
    def __init__(
        self,
        *,
        group_route: Mapping[str, Any] | None = None,
        worker_route: Mapping[str, Any] | None = None,
        read_result: Mapping[str, Any] | None = None,
    ):
        self.group_route = deepcopy(dict(group_route or GROUP_ROUTE))
        self.worker_route = deepcopy(dict(worker_route or WORKER_ROUTE))
        self.read_result = deepcopy(dict(read_result or {"source": "edge"}))
        self.resolve_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []

    async def resolve_target(self, *, tool_name, arguments, context=None):
        self.resolve_calls.append(
            {
                "tool_name": tool_name,
                "arguments": deepcopy(dict(arguments)),
                "context": context,
            }
        )
        if tool_name in {
            "patchbay_worker_start",
            "patchbay_worker_start_batch",
            "patchbay_worker_list",
            "patchbay_worker_status",
            "patchbay_worker_wait",
            "patchbay_worker_options",
            "patchbay_worker_inbox",
        } and not arguments.get("worker") and not arguments.get("fleet_worker_ref"):
            return deepcopy(self.group_route)
        return deepcopy(self.worker_route)

    async def execute_read(self, *, payload, context=None):
        self.read_calls.append({"payload": deepcopy(dict(payload)), "context": context})
        return deepcopy(self.read_result)


class RecordingProjection:
    def __init__(self):
        self.query_result: Mapping[str, Any] = {
            "workers": [
                {
                    "name": "Implementer",
                    "fleet_worker_ref": "fworker_machine_one_implementer",
                    "turn_state": "working",
                    "liveness": "active",
                }
            ],
            "count": 1,
            "projection_revision": 42,
        }
        self.wait_result: Mapping[str, Any] = {
            "workers": [],
            "count": 0,
            "projection_revision": 43,
            "waited_seconds": 12,
        }
        self.worker_result: Mapping[str, Any] | None = {
            "fleet_worker_ref": "fworker_machine_one_implementer",
            "turn_state": "completed",
            "projection_revision": 41,
        }
        self.query_calls: list[dict[str, Any]] = []
        self.wait_calls: list[dict[str, Any]] = []
        self.worker_calls: list[dict[str, Any]] = []

    async def query(self, *, view, filters, route, context=None):
        self.query_calls.append(
            {
                "view": view,
                "filters": deepcopy(dict(filters)),
                "route": deepcopy(dict(route)),
                "context": context,
            }
        )
        return deepcopy(dict(self.query_result))

    async def wait(
        self,
        *,
        filters,
        route,
        since_revision,
        timeout_seconds,
        context=None,
    ):
        self.wait_calls.append(
            {
                "filters": deepcopy(dict(filters)),
                "route": deepcopy(dict(route)),
                "since_revision": since_revision,
                "timeout_seconds": timeout_seconds,
                "context": context,
            }
        )
        return deepcopy(dict(self.wait_result))

    async def get_worker(self, *, route, context=None):
        self.worker_calls.append(
            {"route": deepcopy(dict(route)), "context": context}
        )
        return deepcopy(dict(self.worker_result)) if self.worker_result is not None else None


class RecordingBroker:
    def __init__(self, *, child_results: Mapping[str, Mapping[str, Any]] | None = None):
        self.counter = 0
        self.operations: dict[str, dict[str, Any]] = {}
        self.operation_scopes: dict[tuple[str, str, str, str], str] = {}
        self.children: dict[tuple[str, str], str] = {}
        self.child_results = deepcopy(dict(child_results or {}))
        self.create_calls: list[dict[str, Any]] = []
        self.child_calls: list[dict[str, Any]] = []
        self.prepare_calls: list[str] = []
        self.dispatch_calls: list[str] = []
        self.aggregate_calls: list[str] = []
        self.association_calls: list[dict[str, str]] = []

    def _new_operation(self, *, tool, logical_target, idempotency_key, payload, **extra):
        self.counter += 1
        operation_id = f"op_{self.counter}"
        operation = {
            "operation_id": operation_id,
            "tool": tool,
            "logical_target": logical_target,
            "idempotency_key": idempotency_key,
            "semantic_payload_hash": semantic_payload_hash(payload),
            "state": "created",
            "revision": 1,
            "parent_operation_id": extra.get("parent_operation_id"),
            "item_id": extra.get("item_id", ""),
            "result": None,
            "created_at": float(self.counter),
            "updated_at": float(self.counter),
        }
        self.operations[operation_id] = operation
        return operation

    def create_operation(
        self,
        *,
        tool,
        logical_target,
        idempotency_key,
        payload,
        principal_ref="",
    ):
        call = {
            "tool": tool,
            "logical_target": logical_target,
            "idempotency_key": idempotency_key,
            "payload": deepcopy(dict(payload)),
            "principal_ref": principal_ref,
        }
        self.create_calls.append(call)
        scope = (principal_ref, tool, logical_target, idempotency_key)
        existing_id = self.operation_scopes.get(scope)
        if existing_id:
            existing = self.operations[existing_id]
            if existing["semantic_payload_hash"] != semantic_payload_hash(payload):
                raise OperationBrokerConflict("idempotency_payload_conflict")
            return {**deepcopy(existing), "idempotent_replay": True}
        operation = self._new_operation(
            tool=tool,
            logical_target=logical_target,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        self.operation_scopes[scope] = operation["operation_id"]
        return deepcopy(operation)

    def create_child_operation(
        self,
        parent_operation_id,
        *,
        item_id,
        tool,
        logical_target,
        payload,
        principal_ref="",
    ):
        call = {
            "parent_operation_id": parent_operation_id,
            "item_id": item_id,
            "tool": tool,
            "logical_target": logical_target,
            "payload": deepcopy(dict(payload)),
            "principal_ref": principal_ref,
        }
        self.child_calls.append(call)
        key = (parent_operation_id, item_id)
        existing_id = self.children.get(key)
        if existing_id:
            existing = self.operations[existing_id]
            if existing["semantic_payload_hash"] != semantic_payload_hash(payload):
                raise OperationBrokerConflict("child_operation_payload_conflict")
            return {**deepcopy(existing), "idempotent_replay": True}
        operation = self._new_operation(
            tool=tool,
            logical_target=logical_target,
            idempotency_key=f"child:{parent_operation_id}:{item_id}",
            payload=payload,
            parent_operation_id=parent_operation_id,
            item_id=item_id,
        )
        terminal = self.child_results.get(item_id)
        if terminal:
            operation.update(
                {
                    "state": str(terminal["state"]),
                    "result": deepcopy(dict(terminal["result"])),
                    "revision": 4,
                }
            )
            self.operations[operation["operation_id"]] = operation
        self.children[key] = operation["operation_id"]
        return deepcopy(operation)

    def prepare_operation(self, operation_id, *, expected_revision, principal_ref=""):
        self.prepare_calls.append(operation_id)
        operation = self.operations[operation_id]
        assert operation["revision"] == expected_revision
        operation.update(state="payload_ready", revision=expected_revision + 1)
        return deepcopy(operation)

    def make_dispatchable(self, operation_id, *, expected_revision, principal_ref=""):
        self.dispatch_calls.append(operation_id)
        operation = self.operations[operation_id]
        assert operation["revision"] == expected_revision
        operation.update(state="dispatchable", revision=expected_revision + 1)
        return deepcopy(operation)

    def aggregate_parent(self, parent_operation_id, *, principal_ref=""):
        self.aggregate_calls.append(parent_operation_id)
        parent = self.operations[parent_operation_id]
        child_operations = [
            self.operations[operation_id]
            for (parent_id, _), operation_id in self.children.items()
            if parent_id == parent_operation_id
        ]
        terminal_states = {"succeeded", "blocked", "failed", "cancelled"}
        if child_operations and all(
            child["state"] in terminal_states for child in child_operations
        ):
            statuses = [child["result"]["status"] for child in child_operations]
            status = statuses[0] if len(set(statuses)) == 1 else "partial"
            parent.update(
                state="succeeded" if status in {"ok", "partial"} else status,
                revision=parent["revision"] + 1,
                result=public_envelope(
                    status,
                    result={
                        "items": [
                            {
                                "item_id": child["item_id"],
                                "operation_id": child["operation_id"],
                                "status": child["result"]["status"],
                                "result": deepcopy(child["result"]["result"]),
                            }
                            for child in child_operations
                        ]
                    },
                ),
            )
        return deepcopy(parent)

    def associate_operation(
        self,
        operation_id,
        *,
        work_group_id,
        principal_ref="",
        kind="worker",
    ):
        association = {
            "operation_id": operation_id,
            "work_group_id": work_group_id,
            "principal_ref": principal_ref,
            "kind": kind,
        }
        self.association_calls.append(association)
        return deepcopy(association)


def make_adapter(
    *,
    runtime: RecordingRuntime | None = None,
    broker: Any | None = None,
    projection: RecordingProjection | None = None,
):
    runtime = runtime or RecordingRuntime()
    broker = broker or RecordingBroker()
    projection = projection or RecordingProjection()
    return HubWorkerAdapterV2(runtime, broker, projection), runtime, broker, projection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_action", "expected_edge_arguments", "mutating"),
    [
        (
            "patchbay_worker_options",
            {
                "work_group_id": "group_alpha",
                "repo_path": "/explicit/repo",
                "model": "gpt-test",
                "max_models": 0,
                "include_model_details": False,
            },
            "codex_worker_options",
            {
                "repo_path": "/explicit/repo",
                "model": "gpt-test",
                "max_models": 0,
                "include_model_details": False,
            },
            False,
        ),
        (
            "patchbay_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {
                    "download_url": "https://files.invalid/a",
                    "file_id": "file-1",
                    "mime_type": "text/plain",
                    "file_name": "a.txt",
                },
                "artifact_id": "artifact-1",
                "label": "Input",
                "work_group_id": "group_alpha",
                "view": "file",
                "file_path": "a.txt",
                "max_bytes": 0,
                "max_entries": 0,
                "takeover": False,
                "takeover_reason": "",
                "idempotency_key": "inbox-1",
            },
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {
                    "download_url": "https://files.invalid/a",
                    "file_id": "file-1",
                    "mime_type": "text/plain",
                    "file_name": "a.txt",
                },
                "artifact_id": "artifact-1",
                "label": "Input",
                "repo_path": "/srv/repos/patchbay",
                "view": "file",
                "file_path": "a.txt",
                "max_bytes": 0,
                "max_entries": 0,
                "takeover": False,
                "takeover_reason": "",
            },
            True,
        ),
        (
            "patchbay_worker_start",
            {
                "work_group_id": "group_alpha",
                "lane": "implementation",
                "name": "Implementer",
                "brief": "Build it.",
                "workspace_mode": "isolated_write",
                "auto_suffix": False,
                "include_untracked_from_base": [],
                "context_from_workers": ["Researcher"],
                "context_from_artifacts": ["artifact-1"],
                "context_detail": "review",
                "model": "gpt-test",
                "reasoning_effort": "high",
                "idempotency_key": "start-1",
            },
            "codex_worker_start",
            {
                "name": "Implementer",
                "brief": "Build it.",
                "repo_path": "/srv/repos/patchbay",
                "workspace_mode": "isolated_write",
                "auto_suffix": False,
                "include_untracked_from_base": [],
                "context_from_workers": ["Researcher"],
                "context_from_artifacts": ["artifact-1"],
                "context_detail": "review",
                "model": "gpt-test",
                "reasoning_effort": "high",
            },
            True,
        ),
        (
            "patchbay_worker_message",
            {
                "work_group_id": "group_alpha",
                "fleet_worker_ref": "fworker_machine_one_implementer",
                "message": "Continue.",
                "context_from_workers": [],
                "context_from_artifacts": ["artifact-2"],
                "context_detail": "diff",
                "model": "gpt-next",
                "reasoning_effort": "xhigh",
                "takeover": True,
                "takeover_reason": "user confirmed",
                "idempotency_key": "message-1",
            },
            "codex_worker_message",
            {
                "worker": "wrk_edge_123",
                "message": "Continue.",
                "repo_path": "/srv/repos/patchbay",
                "context_from_workers": [],
                "context_from_artifacts": ["artifact-2"],
                "context_detail": "diff",
                "model": "gpt-next",
                "reasoning_effort": "xhigh",
                "takeover": True,
                "takeover_reason": "user confirmed",
            },
            True,
        ),
        (
            "patchbay_worker_inspect",
            {
                "work_group_id": "group_alpha",
                "fleet_worker_ref": "fworker_machine_one_implementer",
                "wait_seconds": 0,
                "view": "file",
                "file_path": "src/large.py",
                "start_line": 101,
                "end_line": 175,
                "max_bytes": 4096,
                "accepted_dirty_base": ["docs/*.md"],
            },
            "codex_worker_inspect",
            {
                "worker": "wrk_edge_123",
                "wait_seconds": 0,
                "view": "file",
                "file_path": "src/large.py",
                "repo_path": "/srv/repos/patchbay",
                "start_line": 101,
                "end_line": 175,
                "max_bytes": 4096,
                "accepted_dirty_base": ["docs/*.md"],
            },
            False,
        ),
        (
            "patchbay_worker_integrate",
            {
                "work_group_id": "group_alpha",
                "worker": "Implementer",
                "preview_token": "preview-signed-1",
                "allow_dirty_base": False,
                "accepted_dirty_base": ["docs/phase.md"],
                "takeover": False,
                "takeover_reason": "",
                "idempotency_key": "integrate-1",
            },
            "codex_worker_integrate",
            {
                "worker": "wrk_edge_123",
                "repo_path": "/srv/repos/patchbay",
                "preview_token": "preview-signed-1",
                "allow_dirty_base": False,
                "accepted_dirty_base": ["docs/phase.md"],
                "takeover": False,
                "takeover_reason": "",
            },
            True,
        ),
        (
            "patchbay_worker_stop",
            {
                "work_group_id": "group_alpha",
                "worker": "Implementer",
                "cleanup_workspace": False,
                "discard_unintegrated_changes": False,
                "force": True,
                "takeover": False,
                "takeover_reason": "",
                "idempotency_key": "stop-1",
            },
            "codex_worker_stop",
            {
                "worker": "wrk_edge_123",
                "repo_path": "/srv/repos/patchbay",
                "cleanup_workspace": False,
                "discard_unintegrated_changes": False,
                "force": True,
                "takeover": False,
                "takeover_reason": "",
            },
            True,
        ),
    ],
)
async def test_every_mature_single_edge_field_is_preserved(
    tool_name,
    arguments,
    expected_action,
    expected_edge_arguments,
    mutating,
):
    adapter, runtime, broker, projection = make_adapter()

    result = await adapter.handle_tool_call(tool_name, arguments, context=CONTEXT)
    validate_hub_v2_tool_output(tool_name, result)

    if mutating:
        payload = broker.create_calls[0]["payload"]
        assert runtime.read_calls == []
        assert result["status"] == "pending"
    else:
        payload = runtime.read_calls[0]["payload"]
        assert broker.create_calls == []
        assert result["status"] == "ok"
    assert payload["action"] == expected_action
    assert payload["arguments"] == expected_edge_arguments
    assert payload["target"] == {
        key: value
        for key, value in {
            "work_group_id": "group_alpha",
            "lane_id": "implementation",
            "machine_id": "machine_one",
            "edge_generation": 7,
            "workspace_ref": "workspace_patchbay",
            "workspace_projection_ref": "wsp_patchbay_machine_one",
            "fleet_worker_ref": (
                "fworker_machine_one_implementer"
                if tool_name
                in {
                    "patchbay_worker_message",
                    "patchbay_worker_inspect",
                    "patchbay_worker_integrate",
                    "patchbay_worker_stop",
                }
                else ""
            ),
            "edge_worker_id": (
                "wrk_edge_123"
                if tool_name
                in {
                    "patchbay_worker_message",
                    "patchbay_worker_inspect",
                    "patchbay_worker_integrate",
                    "patchbay_worker_stop",
                }
                else ""
            ),
        }.items()
        if value
    }
    assert payload["context"]["work_group_id"] == "group_alpha"
    assert payload["context"]["lane_id"] == "implementation"
    assert "idempotency_key" not in payload["arguments"]
    assert "fleet_worker_ref" not in payload["arguments"]
    assert projection.wait_calls == []


@pytest.mark.asyncio
async def test_inbox_list_and_inspect_are_routed_reads_not_mutations():
    adapter, runtime, broker, _ = make_adapter()
    for index, action in enumerate(("list", "inspect"), start=1):
        result = await adapter.handle_tool_call(
            "patchbay_worker_inbox",
            {
                "action": action,
                "artifact_id": "artifact-1",
                "work_group_id": "group_alpha",
                "idempotency_key": f"inbox-read-{index}",
            },
        )
        assert result["status"] == "ok"

    assert [call["payload"]["arguments"]["action"] for call in runtime.read_calls] == [
        "list",
        "inspect",
    ]
    assert broker.create_calls == []


@pytest.mark.asyncio
async def test_projection_reads_and_hub_event_wait_never_route_sleeping_edge_calls():
    adapter, runtime, broker, projection = make_adapter()
    list_args = {
        "work_group_id": "group_alpha",
        "lane": "implementation",
        "active_only": True,
        "include_stopped": False,
        "owned_only": True,
        "created_after": 123.5,
        "scope": "current_group",
        "cursor": "cursor-1",
        "limit": 25,
    }
    status_args = {
        **list_args,
        "force_refresh": True,
        "since_revision": 41,
    }
    wait_args = {
        **list_args,
        "wait_seconds": 12,
        "since_revision": 42,
    }

    listed = await adapter.handle_tool_call("patchbay_worker_list", list_args)
    status = await adapter.handle_tool_call("patchbay_worker_status", status_args)
    waited = await adapter.handle_tool_call("patchbay_worker_wait", wait_args)
    validate_hub_v2_tool_output("patchbay_worker_list", listed)
    validate_hub_v2_tool_output("patchbay_worker_status", status)
    validate_hub_v2_tool_output("patchbay_worker_wait", waited)

    assert [call["view"] for call in projection.query_calls] == ["list", "status"]
    assert projection.query_calls[0]["filters"] == list_args
    assert projection.query_calls[1]["filters"] == status_args
    assert projection.wait_calls == [
        {
            "filters": wait_args,
            "route": projection.wait_calls[0]["route"],
            "since_revision": 42,
            "timeout_seconds": 12.0,
            "context": None,
        }
    ]
    assert runtime.read_calls == []
    assert broker.create_calls == []
    assert listed["status"] == status["status"] == waited["status"] == "ok"
    assert listed["result"]["work_group"]["work_group_id"] == "group_alpha"
    assert listed["result"]["workers"][0]["machine_id"] == "machine_one"
    assert waited["result"]["projection_revision"] == 43


@pytest.mark.asyncio
async def test_worker_wait_without_revision_uses_current_worker_projection_as_baseline():
    adapter, runtime, broker, projection = make_adapter()
    args = {
        "work_group_id": "group_alpha",
        "wait_seconds": 20,
        "scope": "history",
        "limit": 20,
    }

    waited = await adapter.handle_tool_call("patchbay_worker_wait", args)

    assert projection.query_calls[0]["view"] == "status"
    assert projection.wait_calls[0]["since_revision"] == 42
    assert projection.wait_calls[0]["timeout_seconds"] == 20.0
    assert runtime.read_calls == []
    assert broker.create_calls == []
    assert waited["status"] == "ok"


@pytest.mark.asyncio
async def test_message_during_active_turn_is_blocked_without_an_operation():
    projection = RecordingProjection()
    projection.worker_result = {
        "fleet_worker_ref": "fworker_machine_one_implementer",
        "turn_state": "working",
        "projection_revision": 77,
    }
    worker_route = deepcopy(WORKER_ROUTE)
    worker_route["worker"] = {
        **worker_route["worker"],
        "turn_state": "completed",
    }
    adapter, runtime, broker, projection = make_adapter(
        runtime=RecordingRuntime(worker_route=worker_route), projection=projection
    )

    result = await adapter.handle_tool_call(
        "patchbay_worker_message",
        {
            "work_group_id": "group_alpha",
            "fleet_worker_ref": "fworker_machine_one_implementer",
            "message": "Do more.",
            "idempotency_key": "message-active-1",
        },
    )

    assert result["status"] == "blocked"
    assert result["result"]["reason"] == "active_turn_in_progress"
    assert result["result"]["turn_state"] == "working"
    assert result["operation"] == {}
    assert result["next_actions"][0]["tool"] == "patchbay_worker_wait"
    assert broker.create_calls == []
    assert runtime.read_calls == []


@pytest.mark.asyncio
async def test_inspect_preserves_file_pagination_and_semantic_route_envelope():
    runtime = RecordingRuntime(
        read_result={
            "view": "file",
            "file_path": "src/large.py",
            "text": "page",
            "start_line": 101,
            "end_line": 175,
            "next_start_line": 176,
            "total_lines": 400,
            "max_bytes_applied": 4096,
            "truncated": True,
        }
    )
    adapter, runtime, _, _ = make_adapter(runtime=runtime)

    result = await adapter.handle_tool_call(
        "patchbay_worker_inspect",
        {
            "work_group_id": "group_alpha",
            "worker": "Implementer",
            "view": "file",
            "file_path": "src/large.py",
            "start_line": 101,
            "end_line": 175,
            "max_bytes": 4096,
        },
    )

    assert runtime.read_calls[0]["payload"]["arguments"]["start_line"] == 101
    assert runtime.read_calls[0]["payload"]["arguments"]["end_line"] == 175
    assert runtime.read_calls[0]["payload"]["arguments"]["max_bytes"] == 4096
    assert result["status"] == "ok"
    assert result["result"]["next_start_line"] == 176
    assert result["result"]["fleet_worker_ref"] == "fworker_machine_one_implementer"
    assert result["result"]["machine"] == {
        "machine_id": "machine_one",
        "name": "Build VM",
    }


@pytest.mark.asyncio
async def test_integrate_and_destructive_cleanup_require_explicit_tokens_before_resolution():
    adapter, runtime, broker, _ = make_adapter()

    with pytest.raises(ValueError, match="preview_token"):
        await adapter.handle_tool_call(
            "patchbay_worker_integrate",
            {
                "work_group_id": "group_alpha",
                "worker": "Implementer",
                "idempotency_key": "integrate-missing-preview",
            },
        )
    with pytest.raises(ValueError, match="discard_unintegrated_changes"):
        await adapter.handle_tool_call(
            "patchbay_worker_stop",
            {
                "work_group_id": "group_alpha",
                "worker": "Implementer",
                "cleanup_workspace": True,
                "idempotency_key": "stop-missing-discard",
            },
        )

    assert runtime.resolve_calls == []
    assert broker.create_calls == []


@pytest.mark.asyncio
async def test_single_mutation_creates_dispatchable_semantic_operation_not_queue_receipt():
    adapter, runtime, broker, _ = make_adapter()

    result = await adapter.handle_tool_call(
        "patchbay_worker_start",
        {
            "work_group_id": "group_alpha",
            "lane": "implementation",
            "name": "Implementer",
            "brief": "Implement the adapter.",
            "idempotency_key": "start-semantic-1",
        },
        context=CONTEXT,
    )

    assert result == {
        "status": "pending",
        "result": {
            "work_group": GROUP_ROUTE["work_group"],
            "lane": GROUP_ROUTE["lane"],
            "machine": GROUP_ROUTE["machine"],
            "workspace": GROUP_ROUTE["workspace"],
            "edge_generation": "7",
            "workspace_ref": "workspace_patchbay",
            "workspace_projection_ref": "wsp_patchbay_machine_one",
        },
        "operation": {
            "operation_id": "op_1",
            "state": "dispatchable",
            "idempotency_key": "start-semantic-1",
            "semantic_payload_hash": broker.operations["op_1"]["semantic_payload_hash"],
            "revision": 3,
            "created_at": 1.0,
            "updated_at": 1.0,
            "tool_name": "patchbay_worker_start",
            "machine_id": "machine_one",
            "edge_generation": "7",
        },
        "warnings": [],
        "next_actions": [
            {
                "tool": "patchbay_operation_status",
                "arguments": {"operation_id": "op_1"},
            }
        ],
    }
    encoded = json.dumps(result, sort_keys=True)
    assert "command_id" not in encoded
    assert '"queued"' not in encoded
    assert runtime.read_calls == []
    assert broker.prepare_calls == ["op_1"]
    assert broker.dispatch_calls == ["op_1"]


@pytest.mark.asyncio
async def test_batch_prevalidates_before_any_route_or_broker_side_effect():
    adapter, runtime, broker, _ = make_adapter()
    arguments = {
        "work_group_id": "group_alpha",
        "shared_brief": "Build V2.",
        "workers": [
            {
                "item_id": "same",
                "idempotency_key": "item-1",
                "name": "One",
                "lane": "one",
                "mission": "First.",
            },
            {
                "item_id": "same",
                "idempotency_key": "item-2",
                "name": "Two",
                "lane": "two",
                "mission": "Second.",
            },
        ],
        "idempotency_key": "batch-invalid",
    }

    with pytest.raises(ValueError, match="duplicate batch item_id"):
        await adapter.handle_tool_call("patchbay_worker_start_batch", arguments)

    assert runtime.resolve_calls == []
    assert broker.create_calls == []
    assert broker.child_calls == []


def batch_arguments():
    return {
        "work_group_id": "group_alpha",
        "shared_brief": "Build the V2 adapter with exact contract parity.",
        "context_from_workers": ["Architect"],
        "context_from_artifacts": ["artifact-shared"],
        "context_detail": "review",
        "workers": [
            {
                "item_id": "implementation",
                "idempotency_key": "item-implementation",
                "name": "Implementer",
                "lane": "implementation",
                "mission": "Implement the adapter.",
                "workspace_mode": "isolated_write",
                "model": "gpt-impl",
                "reasoning_effort": "high",
                "context_from_workers": ["Researcher", "Architect"],
                "context_from_artifacts": ["artifact-implementation"],
                "include_untracked_from_base": ["docs/*.md"],
                "auto_suffix": False,
            },
            {
                "item_id": "verification",
                "idempotency_key": "item-verification",
                "name": "Verifier",
                "lane": "verification",
                "mission": "Verify every semantic boundary.",
                "workspace_mode": "read_only",
            },
        ],
        "idempotency_key": "batch-parent-1",
    }


@pytest.mark.asyncio
async def test_batch_creates_stable_parent_children_and_replays_without_duplicates():
    adapter, runtime, broker, _ = make_adapter()
    arguments = batch_arguments()

    first = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", arguments, context=CONTEXT
    )
    second = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", arguments, context=CONTEXT
    )
    validate_hub_v2_tool_output("patchbay_worker_start_batch", first)
    validate_hub_v2_tool_output("patchbay_worker_start_batch", second)

    assert first["status"] == second["status"] == "pending"
    assert first["operation"]["operation_id"] == second["operation"]["operation_id"] == "op_1"
    assert [item["operation"]["operation_id"] for item in first["result"]["items"]] == [
        "op_2",
        "op_3",
    ]
    assert [item["operation"]["operation_id"] for item in second["result"]["items"]] == [
        "op_2",
        "op_3",
    ]
    assert len(broker.operations) == 3
    assert [call["item_id"] for call in broker.child_calls] == [
        "implementation",
        "verification",
        "implementation",
        "verification",
    ]

    implementation = broker.child_calls[0]["payload"]
    assert implementation["item_id"] == "implementation"
    assert implementation["item_idempotency_key"] == "item-implementation"
    assert implementation["action"] == "codex_worker_start"
    assert implementation["arguments"] == {
        "name": "Implementer",
        "brief": (
            "Build the V2 adapter with exact contract parity.\n\n"
            "Worker mission:\nImplement the adapter."
        ),
        "repo_path": "/srv/repos/patchbay",
        "workspace_mode": "isolated_write",
        "auto_suffix": False,
        "include_untracked_from_base": ["docs/*.md"],
        "context_from_workers": ["Architect", "Researcher"],
        "context_from_artifacts": [
            "artifact-shared",
            "artifact-implementation",
        ],
        "context_detail": "review",
        "model": "gpt-impl",
        "reasoning_effort": "high",
    }
    assert implementation["lane_id"] == "implementation"
    assert implementation["target"]["lane_id"] == "implementation"


@pytest.mark.asyncio
async def test_terminal_mixed_batch_returns_partial_item_semantics():
    broker = RecordingBroker(
        child_results={
            "implementation": {
                "state": "succeeded",
                "result": public_envelope(
                    "ok", result={"name": "Implementer", "accepted": True}
                ),
            },
            "verification": {
                "state": "blocked",
                "result": public_envelope(
                    "blocked", result={"reason": "capacity_blocked"}
                ),
            },
        }
    )
    adapter, _, broker, _ = make_adapter(broker=broker)

    result = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", batch_arguments()
    )
    validate_hub_v2_tool_output("patchbay_worker_start_batch", result)

    assert result["status"] == "partial"
    assert [item["status"] for item in result["result"]["items"]] == [
        "ok",
        "blocked",
    ]
    assert result["result"]["items"][0]["result"]["name"] == "Implementer"
    assert result["result"]["items"][1]["result"]["reason"] == "capacity_blocked"
    assert result["operation"]["state"] == "succeeded"
    assert result["next_actions"] == []


@pytest.mark.asyncio
async def test_completed_batch_retry_replays_parent_result_without_recreating_children():
    broker = RecordingBroker(
        child_results={
            "implementation": {
                "state": "succeeded",
                "result": public_envelope("ok", result={"name": "Implementer"}),
            },
            "verification": {
                "state": "succeeded",
                "result": public_envelope("ok", result={"name": "Verifier"}),
            },
        }
    )
    adapter, _, _, _ = make_adapter(broker=broker)

    first = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", batch_arguments()
    )
    replay = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", batch_arguments()
    )

    assert first == replay
    assert replay["status"] == "ok"
    assert [item["result"]["name"] for item in replay["result"]["items"]] == [
        "Implementer",
        "Verifier",
    ]
    assert len(broker.child_calls) == 2
    assert len(broker.operations) == 3
    assert [call["operation_id"] for call in broker.association_calls] == [
        "op_1",
        "op_2",
        "op_3",
        "op_1",
    ]


@pytest.mark.asyncio
async def test_batch_retry_ignores_volatile_request_activity_metadata():
    adapter, _, broker, _ = make_adapter()
    first_context = RequestContext(
        client_ref="client-stable",
        chatgpt_session_ref="conversation-stable",
        work_run_ref="run-stable",
        work_run_started_at=100.0,
        work_run_last_activity_at=101.0,
        active_mcp_sessions=1,
    )
    retry_context = RequestContext(
        client_ref="client-stable",
        chatgpt_session_ref="conversation-stable",
        work_run_ref="run-stable",
        work_run_started_at=100.0,
        work_run_last_activity_at=150.0,
        active_mcp_sessions=7,
    )

    first = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", batch_arguments(), context=first_context
    )
    replay = await adapter.handle_tool_call(
        "patchbay_worker_start_batch", batch_arguments(), context=retry_context
    )

    assert replay["operation"]["operation_id"] == first["operation"]["operation_id"]
    assert broker.create_calls[0]["payload"] == broker.create_calls[1]["payload"]
    assert "work_run_last_activity_at" not in broker.create_calls[0]["payload"]["context"]
    assert "active_mcp_sessions" not in broker.create_calls[0]["payload"]["context"]


@pytest.mark.asyncio
async def test_real_broker_idempotency_replays_and_conflicts_semantically(tmp_path):
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")
    broker = OperationBroker(store)
    worker_route = {**WORKER_ROUTE, "principal_ref": store.principal_ref}
    projection = RecordingProjection()
    runtime = RecordingRuntime(worker_route=worker_route)
    adapter = HubWorkerAdapterV2(runtime, broker, projection)
    base_arguments = {
        "work_group_id": "group_alpha",
        "worker": "Implementer",
        "message": "Continue with tests.",
        "idempotency_key": "message-real-broker-1",
    }

    first = await adapter.handle_tool_call("patchbay_worker_message", base_arguments)
    replay = await adapter.handle_tool_call("patchbay_worker_message", base_arguments)
    conflict = await adapter.handle_tool_call(
        "patchbay_worker_message",
        {**base_arguments, "message": "Use a different semantic payload."},
    )

    assert first["status"] == replay["status"] == "pending"
    assert first["operation"]["operation_id"] == replay["operation"]["operation_id"]
    assert conflict["status"] == "blocked"
    assert conflict["result"]["reason"] == "idempotency_payload_conflict"
    assert conflict["operation"] == {}
    operation_count = store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    assert operation_count == 1
    store.close()


@pytest.mark.asyncio
async def test_routed_queue_receipt_is_never_reported_as_success():
    runtime = RecordingRuntime(
        read_result={
            "command_id": "cmd-old-queue",
            "state": "queued",
            "accepted": True,
        }
    )
    adapter, _, _, _ = make_adapter(runtime=runtime)

    result = await adapter.handle_tool_call(
        "patchbay_worker_options", {"work_group_id": "group_alpha"}
    )

    assert result["status"] == "pending"
    assert result["operation"] == {}
    assert "command_id" not in json.dumps(result)
