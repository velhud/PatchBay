from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.http import create_app
from patchbay.authoring.interfaces.mcp import MCP
from conftest import qualification_profile
from test_core import package_for


def auth(service, role):
    tokens = json.loads((service.store.home / "tokens.json").read_text())
    return {"Authorization": "Bearer " + tokens[role]}


def test_http_auth_origin_and_packaged_ui(setup_author):
    s, _, _, _, _ = setup_author
    with TestClient(create_app(s.store.home)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/style.css").status_code == 200
        assert client.get("/api/tasks").status_code == 401
        response = client.get("/api/tasks", headers=auth(s, "reader"))
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get("/api/tasks", headers={**auth(s, "operator"), "Origin": "https://untrusted.example"}).status_code == 403


def test_read_only_mcp_catalog_and_forged_mutation_refusal(setup_author):
    s, _, repo, task, _ = setup_author
    protocol = MCP(s)
    names = [tool["name"] for tool in protocol.tools("reader")]
    assert len(names) == 11 and all(name.startswith("author_") for name in names)
    assert not any(name.startswith("codex_") for name in names)
    before = s.store.one("SELECT COUNT(*) AS count FROM operations")["count"]
    result = protocol.handle("reader", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "author_create_task", "arguments": {"repository": repo["id"], "goal": "Unauthorized", "key": "forged"}}})
    assert result["result"]["isError"] is True
    assert s.store.one("SELECT COUNT(*) AS count FROM operations")["count"] == before
    assert protocol.handle("reader", {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "author_create_task"}}) is None


def test_mcp_write_gate_is_separate_from_runtime_role(setup_author):
    s, _, _, task, _ = setup_author
    protocol = MCP(s)
    assert len(protocol.tools("operator")) == 11
    Access(s.store).qualify({**qualification_profile(), "model": "test-double", "client": "unit-test", "ordinary_chat": True,
                            "write_supported": True, "probe_description": "Disposable test fixture only, not a live Pro qualification",
                            "observed_at": "fixture"}, actor="test-operator")
    tools = {tool["name"]: tool for tool in protocol.tools("author")}
    assert "author_submit_package" in tools
    assert tools["author_submit_package"]["annotations"]["readOnlyHint"] is False
    assert tools["author_submit_package"]["_meta"]["openai/fileParams"] == ["artifact_file"]
    native = tools["author_submit_package"]["inputSchema"]["$defs"]["NativeFile"]
    assert {"download_url", "file_id", "mime_type", "file_name"} == set(native["properties"])
    assert len(protocol.tools("reader")) == 11


def test_http_import_uses_same_candidate_core_and_reader_cannot_write(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    package = package_for(s, task, tmp_path, {"added.txt": "actual code bytes"})
    headers = {**auth(s, "operator"), "Content-Type": "application/zip", "Idempotency-Key": "http-upload",
               "X-Author-Lease": lease["token"], "X-Author-Epoch": str(lease["epoch"])}
    with TestClient(create_app(s.store.home)) as client:
        assert client.post("/api/context", json={"task_id": task["id"]}, headers=auth(s, "reader")).status_code == 403
        response = client.post(f"/api/tasks/{task['id']}/packages", content=package.read_bytes(), headers=headers)
        assert response.status_code == 200, response.text
        accepted = response.json()
        assert accepted["revision"] == 1
        replay = client.post(f"/api/tasks/{task['id']}/packages", content=package.read_bytes(), headers=headers)
        assert replay.json()["commit"] == accepted["commit"]
        assert s.tasks.get(task["id"])["current_revision"] == 1
        exported = client.post("/api/context", json={"task_id": task["id"]}, headers=auth(s, "operator"))
        download = client.get(exported.json()["download"], headers=auth(s, "reader"))
        assert download.status_code == 200 and download.content.startswith(b"PK")


def test_read_only_mcp_calls_do_not_update_domain_events(setup_author):
    s, _, _, task, _ = setup_author
    protocol = MCP(s)
    before = s.store.one("SELECT COUNT(*) AS count FROM events")["count"]
    for number in range(10):
        result = protocol.handle("reader", {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {
            "name": "author_get_task", "arguments": {"task_id": task["id"]}}})
        assert result["result"]["isError"] is False
    assert s.store.one("SELECT COUNT(*) AS count FROM events")["count"] == before
