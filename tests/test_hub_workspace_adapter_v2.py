from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest

from patchbay.hub.adapters.workspace import WorkspaceAdapter
from patchbay.protocol.context import RequestContext


class FakeFleet:
    def __init__(self, machines: list[dict[str, Any]]):
        self.machines = machines

    def list_machines(self) -> dict[str, Any]:
        return {"machines": deepcopy(self.machines)}


class DiscoveringFleet(FakeFleet):
    async def discover_workspaces(self, **kwargs) -> dict[str, Any]:
        assert kwargs["query"] == "RetailMind"
        assert kwargs["max_depth"] == 4
        return {
            "workspaces": [
                {
                    "machine_id": "machine_alpha",
                    "workspace_ref": "workspace_retailmind",
                    "workspace_projection_ref": "wsp_retailmind_alpha",
                    "alias": "RetailMind",
                    "path": "/srv/projects/RetailMind",
                    "git": True,
                }
            ],
            "truncated": True,
            "next_cursor": "edge-cursor-2",
        }


class FakeRuntime:
    def __init__(self, groups: Mapping[str, Mapping[str, Any]] | None = None):
        self.groups = {key: dict(value) for key, value in (groups or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def get_work_group(
        self, work_group_id: str, *, context: RequestContext | None = None
    ) -> dict[str, Any] | None:
        self.calls.append({"work_group_id": work_group_id, "context": context})
        group = self.groups.get(work_group_id)
        return deepcopy(group) if group else None


class RecordingBroker:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        call = {
            "machine_id": machine_id,
            "edge_generation": edge_generation,
            "action": action,
            "arguments": deepcopy(dict(arguments)),
            "target": deepcopy(dict(target)),
            "context": context,
        }
        self.calls.append(call)
        if action == "patchbay_edge_preflight":
            repo_path = str(arguments.get("repo_path") or "")
            if repo_path == "/etc":
                return {"ok": False, "error": "path is outside allowed roots"}
            return {
                "ok": True,
                "repo_requested": repo_path,
                "repo_resolved": repo_path,
                "repo_exists": True,
                "git_repo": True,
            }
        return deepcopy(
            self.results.get(
                action,
                {"workspace_id": "ws", "path": ".", "text": f"result from {action}"},
            )
        )


class FailedPreflightBroker(RecordingBroker):
    async def execute(self, **kwargs) -> dict[str, Any]:
        self.calls.append(deepcopy(kwargs))
        return {
            "status": "failed",
            "result": {"reason": "transport_error"},
            "operation": {"operation_id": "op_preflight"},
            "warnings": [],
            "next_actions": [],
        }


@pytest.fixture
def machines() -> list[dict[str, Any]]:
    return [
        {
            "machine_id": "machine_alpha",
            "display_name": "Alpha",
            "status": "online",
            "edge_generation": "edgegen_alpha",
            "tags": ["linux", "build"],
            "workspace_projections": [
                {
                    "workspace_ref": "workspace_projects",
                    "workspace_projection_ref": "wsp_projects_alpha",
                    "alias": "projects",
                    "path": "/srv/projects",
                    "git": False,
                },
                {
                    "workspace_ref": "workspace_patchbay",
                    "workspace_projection_ref": "wsp_patchbay_alpha",
                    "aliases": ["PatchBay", "patchbay-main"],
                    "display_name": "PatchBay",
                    "path": "/srv/projects/PatchBay",
                    "git": True,
                    "repository_identity": {"remote": "github.com/velhud/PatchBay"},
                    "preflight": {"status": "ok", "revision": 7},
                },
            ],
        },
        {
            "machine_id": "machine_beta",
            "display_name": "Beta",
            "status": "online",
            "edge_generation": "edgegen_beta",
            "tags": ["linux"],
            "workspace_projections": [
                {
                    "workspace_ref": "workspace_patchbay",
                    "workspace_projection_ref": "wsp_patchbay_beta",
                    "aliases": ["PatchBay"],
                    "display_name": "PatchBay",
                    "path": "/work/PatchBay",
                    "git": True,
                    "repository_identity": {"remote": "github.com/velhud/PatchBay"},
                    "preflight": {"status": "stale", "revision": 3},
                }
            ],
        },
        {
            "machine_id": "machine_offline",
            "display_name": "Offline",
            "status": "offline",
            "edge_generation": "edgegen_offline",
            "tags": ["linux"],
            "workspace_projections": [
                {
                    "workspace_ref": "workspace_patchbay",
                    "workspace_projection_ref": "wsp_patchbay_offline",
                    "alias": "PatchBay",
                    "path": "/offline/PatchBay",
                    "git": True,
                }
            ],
        },
    ]


def adapter(
    machines: list[dict[str, Any]],
    *,
    groups: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[WorkspaceAdapter, RecordingBroker]:
    broker = RecordingBroker()
    return WorkspaceAdapter(FakeFleet(machines), FakeRuntime(groups), broker), broker


@pytest.mark.asyncio
async def test_workspace_list_aggregates_logical_refs_and_cross_machine_projections(machines):
    subject, _broker = adapter(machines)

    envelope = await subject.workspace_list({"query": "PatchBay"})

    assert envelope["status"] == "ok"
    assert envelope["result"]["count"] == 1
    workspace = envelope["result"]["workspaces"][0]
    assert workspace["workspace_ref"] == "workspace_patchbay"
    assert workspace["aliases"] == ["PatchBay", "patchbay-main"]
    assert workspace["readiness"] == "ready"
    assert workspace["machine_availability"] == {
        "machine_ids": ["machine_alpha", "machine_beta"],
        "online_machine_ids": ["machine_alpha", "machine_beta"],
        "total": 2,
        "online": 2,
    }
    assert {item["repo_path"] for item in workspace["projections"]} == {
        "/srv/projects/PatchBay",
        "/work/PatchBay",
    }


@pytest.mark.asyncio
async def test_workspace_list_filters_tags_offline_and_reports_transport_paging(machines):
    subject, _broker = adapter(machines)

    page = await subject.workspace_list(
        {"required_tags": ["build"], "include_offline": True, "max_results": 1}
    )

    assert page["result"]["count"] == 1
    assert page["result"]["truncated"] is True
    assert page["result"]["next_cursor"] == page["result"]["workspaces"][0]["workspace_ref"]
    assert {
        projection["machine_id"]
        for projection in page["result"]["workspaces"][0]["projections"]
    } == {"machine_alpha"}


@pytest.mark.asyncio
async def test_workspace_list_merges_bounded_fleet_discovery_into_existing_projection_key(machines):
    broker = RecordingBroker()
    subject = WorkspaceAdapter(DiscoveringFleet(machines), FakeRuntime(), broker)

    result = await subject.workspace_list(
        {"query": "RetailMind", "discover": True, "max_depth": 4, "max_results": 5}
    )

    assert result["status"] == "ok"
    assert result["result"]["truncated"] is True
    assert result["result"]["next_cursor"] == "edge-cursor-2"
    assert result["result"]["workspaces"][0]["workspace_ref"] == "workspace_retailmind"
    assert result["result"]["workspaces"][0]["projections"][0]["machine_id"] == "machine_alpha"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_specific_alias_beats_broad_workspace_relative_projection(machines):
    subject, broker = adapter(machines)

    result = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "repo_path": "PatchBay",
            "ungrouped_reason": "tiny_check",
            "include_tree": True,
        }
    )

    assert result["status"] == "ok"
    assert [call["action"] for call in broker.calls] == [
        "patchbay_edge_preflight",
        "codex_open_workspace",
    ]
    assert broker.calls[0]["arguments"]["repo_path"] == "/srv/projects/PatchBay"
    assert broker.calls[1]["arguments"] == {
        "repo": "/srv/projects/PatchBay",
        "include_tree": True,
    }
    assert result["result"]["workspace"]["workspace_ref"] == "workspace_patchbay"


@pytest.mark.asyncio
async def test_workspace_ref_with_child_repo_path_keeps_child_target(machines):
    subject, broker = adapter(machines)

    result = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "workspace_ref": "workspace_projects",
            "repo_path": "/srv/projects/rotor-api",
            "ungrouped_reason": "operator_requested",
            "include_tree": False,
        }
    )

    assert result["status"] == "ok"
    assert broker.calls[0]["arguments"]["repo_path"] == "/srv/projects/rotor-api"
    assert broker.calls[1]["arguments"]["repo"] == "/srv/projects/rotor-api"


@pytest.mark.asyncio
async def test_group_pin_overrides_conflicting_explicit_projection(machines):
    groups = {
        "group_review": {
            "work_group_id": "group_review",
            "pinned_machine_id": "machine_beta",
            "pinned_edge_generation": "edgegen_beta",
            "workspace_ref": "workspace_patchbay",
            "workspace_projection_ref": "wsp_patchbay_beta",
            "repo_path": "/work/PatchBay",
        }
    }
    subject, broker = adapter(machines, groups=groups)
    context = RequestContext(
        client_ref="client_workspace",
        chatgpt_session_ref="conversation_workspace",
    )

    result = await subject.handle_tool_call(
        "patchbay_workspace_tree",
        {
            "work_group_id": "group_review",
            "machine_id": "machine_alpha",
            "workspace_ref": "workspace_projects",
            "path": "src",
        },
        context=context,
    )

    assert result["status"] == "ok"
    assert {call["machine_id"] for call in broker.calls} == {"machine_beta"}
    assert {call["edge_generation"] for call in broker.calls} == {"edgegen_beta"}
    assert broker.calls[1]["arguments"] == {"repo": "/work/PatchBay", "path": "src"}
    assert all(call["context"] is context for call in broker.calls)
    assert subject.runtime.calls == [{"work_group_id": "group_review", "context": context}]
    assert result["result"]["work_group"]["work_group_id"] == "group_review"


@pytest.mark.asyncio
async def test_explicit_workspace_projection_rejects_conflicting_repo_hint(machines):
    subject, broker = adapter(machines)

    result = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "workspace_ref": "workspace_patchbay",
            "repo_path": "/etc",
            "ungrouped_reason": "tiny_check",
        }
    )

    assert result["status"] == "blocked"
    assert result["result"]["reason"] == "workspace_path_mismatch"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_preflight_transport_failure_is_not_reclassified_as_domain_block(machines):
    broker = FailedPreflightBroker()
    subject = WorkspaceAdapter(FakeFleet(machines), FakeRuntime(), broker)

    result = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "workspace_ref": "workspace_patchbay",
            "ungrouped_reason": "tiny_check",
        }
    )

    assert result["status"] == "failed"
    assert result["result"]["reason"] == "transport_error"
    assert result["operation"]["operation_id"] == "op_preflight"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_read_file_preserves_page_fields_and_search_preserves_timeout_recovery(machines):
    subject, broker = adapter(machines)
    broker.results["codex_read_file"] = {
        "workspace_id": "ws",
        "path": "README.md",
        "text": "10 | page",
        "start_line": 10,
        "end_line": 10,
        "requested_end_line": 20,
        "total_lines": 50,
        "max_bytes_applied": 64,
        "truncated": True,
        "next_start_line": 11,
    }
    broker.results["codex_search_repo"] = {
        "workspace_id": "ws",
        "matches": [{"path": "src/a.py", "line": 1, "text": "needle"}],
        "truncated": True,
        "timed_out": True,
        "timeout_ms": 1250,
        "suggested_next": "Narrow the path.",
    }
    route = {
        "machine_id": "machine_alpha",
        "workspace_ref": "workspace_patchbay",
        "ungrouped_reason": "operator_requested",
    }

    read = await subject.workspace_read_file(
        {**route, "file_path": "README.md", "start_line": 10, "end_line": 20, "max_bytes": 64}
    )
    search = await subject.workspace_search(
        {**route, "query": "needle", "path": "src", "max_results": 5, "timeout_ms": 1250}
    )

    assert read["status"] == "ok"
    assert read["result"]["next_start_line"] == 11
    assert read["result"]["requested_end_line"] == 20
    assert read["result"]["max_bytes_applied"] == 64
    assert search["status"] == "partial"
    assert search["result"]["timed_out"] is True
    assert search["result"]["suggested_next"] == "Narrow the path."
    search_call = next(call for call in broker.calls if call["action"] == "codex_search_repo")
    assert search_call["arguments"]["timeout_ms"] == 1250
    assert search_call["arguments"]["max_results"] == 5


@pytest.mark.asyncio
async def test_invalid_route_and_out_of_root_path_are_blocked_before_inspection(machines):
    subject, broker = adapter(machines)

    invalid = await subject.workspace_open({"repo_path": "PatchBay"})
    traversal = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "repo_path": "../PatchBay",
            "ungrouped_reason": "tiny_check",
        }
    )
    outside = await subject.workspace_open(
        {
            "machine_id": "machine_alpha",
            "repo_path": "/etc",
            "ungrouped_reason": "tiny_check",
        }
    )

    assert invalid["status"] == "blocked"
    assert invalid["result"]["reason"] == "workspace_target_required"
    assert traversal["status"] == "blocked"
    assert traversal["result"]["reason"] == "invalid_repo_path"
    assert outside["status"] == "blocked"
    assert outside["result"]["reason"] == "path is outside allowed roots"
    assert [call["action"] for call in broker.calls] == ["patchbay_edge_preflight"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view", "expected_action", "public_arguments", "edge_arguments"),
    [
        (
            "status",
            "codex_git_status",
            {"file_path": "src/a.py", "porcelain": True},
            {"file_path": "src/a.py", "porcelain": True},
        ),
        (
            "summary",
            "codex_show_changes",
            {"staged": True, "include_diff": True, "max_bytes": 4000},
            {"staged": True, "include_diff": True, "max_diff_bytes": 4000},
        ),
        (
            "diff",
            "codex_git_diff",
            {"file_path": "src/a.py", "staged": False, "max_bytes": 9000},
            {"file_path": "src/a.py", "staged": False, "max_bytes": 9000},
        ),
    ],
)
async def test_changes_maps_strict_views_to_read_only_git_actions(
    machines, view, expected_action, public_arguments, edge_arguments
):
    subject, broker = adapter(machines)

    result = await subject.workspace_changes(
        {
            "machine_id": "machine_alpha",
            "workspace_ref": "workspace_patchbay",
            "ungrouped_reason": "tiny_check",
            "view": view,
            **public_arguments,
        }
    )

    assert result["status"] == "ok"
    assert [call["action"] for call in broker.calls] == [
        "patchbay_edge_preflight",
        expected_action,
    ]
    assert broker.calls[1]["arguments"] == {
        **edge_arguments,
        "repo": "/srv/projects/PatchBay",
    }
    assert all("write" not in call["action"] and "bash" not in call["action"] for call in broker.calls)
