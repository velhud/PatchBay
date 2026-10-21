"""Actual disposable Author transitions; no model or deployed-service calls."""
from __future__ import annotations

import itertools
import json

import pytest
from pydantic import ValidationError

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces.inputs import ReadFile
from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.interfaces.mcp_legacy import CATALOG
from patchbay.authoring.service import AuthorService
from patchbay.authoring.revisions.drafts import Drafts
from patchbay.authoring.interfaces.http import create_app
from fastapi.testclient import TestClient
from test_interfaces import auth
from test_controller_contracts import qualified


def request(p, name, args, role="author"):
    return p.handle(role, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


def test_selector_runtime_exhaustive_and_published_alternatives():
    missing = object()
    context, draft = "ctx_" + "a" * 40, "draft_" + "b" * 32
    schema = ReadFile.model_json_schema()
    assert schema["additionalProperties"] is False
    assert [branch["required"] for branch in schema["oneOf"]] == [["context_id"], ["draft_id", "draft_version"]]
    for c, d, v in itertools.product((missing, None, context), (missing, None, draft), (missing, None, 1, 2, 0, -1, True, "1")):
        args = {k: val for k, val in (("context_id", c), ("draft_id", d), ("draft_version", v)) if val is not missing}
        args["path"] = "app.py"
        expected = (c == context and d in (missing, None) and v in (missing, None)) or (
            c in (missing, None) and d == draft and type(v) is int and v > 0)
        try:
            ReadFile.model_validate(args)
            accepted = True
        except ValidationError:
            accepted = False
        assert accepted == expected, args


def test_safe_argument_errors_are_bounded_field_specific_and_before_effect(setup_author):
    s, _, _, task, _ = setup_author
    p = qualified(s)
    before = s.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    bad = request(p, "author_read_file", {"path": "app.py"})
    assert bad["isError"]
    detail = bad["structuredContent"]["details"]
    assert detail["boundary"] == "arguments_rejected_before_effect"
    assert detail["field_errors"][0]["fields"] == ["context_id", "draft_id", "draft_version"]
    assert "author_recover_task" in detail["field_errors"][0]["repair"]
    marker = "private-content-must-never-be-echoed"
    args = {"path": "app.py", "context_id": marker, **{marker + str(i): marker for i in range(20)}}
    bad = request(p, "author_read_file", args)
    rendered = json.dumps(bad)
    assert marker not in rendered
    assert len(rendered) < 5000
    assert bad["structuredContent"]["details"]["omitted_error_count"] > 0
    assert s.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before
    context = s.snapshots.export(task["id"], key="fixture-existing-context")
    good = request(p, "author_read_file", {"context_id": context["id"], "path": "app.py"})
    assert not good["isError"] and "a - b" in good["structuredContent"]["text"]


def test_reader_and_writer_instructions_match_effect_catalog_and_denials(setup_author):
    s, _, _, _, _ = setup_author
    p = qualified(s)
    before = s.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    for role in ("reader", "author", "operator"):
        initialized = p.handle(role, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
        writable = any(not t["annotations"]["readOnlyHint"] for t in p.tools(role))
        if writable:
            assert "Export feedback when handing off" in initialized["instructions"]
        else:
            assert "Export feedback when handing off" not in initialized["instructions"]
            assert "Read or export pinned context" not in initialized["instructions"]
            assert "creating a new export requires an authorized writer or operator" in initialized["instructions"]
    for name, _, _, write in CATALOG:
        if write:
            with pytest.raises(AuthorError) as error:
                p.call("reader", name, {})
            assert error.value.code == "FORBIDDEN"
    for tool in p.tools("author") + p.tools("reader"):
        write = next(entry[3] for entry in CATALOG if entry[0] == tool["name"])
        assert tool["annotations"] == {"readOnlyHint": not write, "destructiveHint": write,
            "idempotentHint": True, "openWorldHint": tool["name"] in {"author_submit_package", "author_run_checks"}}
    assert s.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


@pytest.mark.parametrize("role,count", [("author", 20), ("operator", 22)])
def test_each_separately_qualified_writer_has_role_correct_catalog_and_guidance(setup_author, role, count):
    p = qualified(setup_author[0], role)
    initialized = p.handle(role, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
    assert len(p.tools(role)) == count
    assert "Export feedback when handing off" in initialized["instructions"]
    assert "no PR creation, automatic merge or deployment is authorized" in initialized["instructions"]
    assert len(p.tools("reader")) == 11


def test_post_effect_invalid_output_recovers_one_durable_task_and_original_key(setup_author, monkeypatch):
    s, _, repo, _, _ = setup_author
    p = qualified(s)
    original = s.tasks.create
    admitted = []
    def corrupt(*args, **kwargs):
        value = original(*args, **kwargs)
        admitted.append(value["id"])
        return {**value, "status": 42, "id": "task_" + "f" * 32, "private": "never-echo-this-result"}
    monkeypatch.setattr(s.tasks, "create", corrupt)
    args = {"repository": repo["id"], "goal": "Disposable accepted effect", "key": "original-task-key"}
    before = s.store.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    response = request(p, "author_create_task", args)
    assert response["isError"]
    fault = response["structuredContent"]
    assert fault["code"] == "INVALID_OUTPUT"
    details = fault["details"]
    assert details["boundary"] == "effect_may_exist_result_invalid"
    assert details["original_key"] == args["key"] and details["success_asserted"] is False
    assert details["task_id"] == admitted[0]
    assert "never-echo-this-result" not in json.dumps(response)
    assert "task_" + "f" * 32 not in json.dumps(response)
    fresh = MCP(AuthorService(s.store.home))
    for action in details["recovery_actions"]:
        assert not request(fresh, action["tool"], action["arguments"], role="reader")["isError"]
    monkeypatch.setattr(s.tasks, "create", original)
    replay = request(p, "author_create_task", args)
    assert replay["structuredContent"]["id"] == admitted[0]
    # A completely dropped successful response also replays the same task.
    original(**args)
    assert original(**args)["id"] == admitted[0]
    assert s.store.one("SELECT COUNT(*) AS n FROM tasks")["n"] == before + 1


def test_invalid_read_output_does_not_claim_a_possible_mutation(setup_author, monkeypatch):
    s, _, _, _, _ = setup_author
    p = MCP(s)
    before = s.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    monkeypatch.setattr(s.tasks, "list", lambda **kwargs: {"tasks": "bad"})
    result = request(p, "author_list_tasks", {}, role="reader")
    details = result["structuredContent"]["details"]
    assert result["isError"] and details["boundary"] == "read_result_invalid"
    assert details["effect_may_exist"] is False
    assert "original_key" not in details
    assert s.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


@pytest.mark.parametrize("write", [False, True])
def test_oversized_result_keeps_effect_boundary_and_durable_recovery(setup_author, monkeypatch, write):
    s, _, repo, task, _ = setup_author
    p = qualified(s)
    if write:
        original = s.tasks.create
        monkeypatch.setattr(s.tasks, "create", lambda *args, **kwargs: {**original(*args, **kwargs), "goal": "x" * (9 * 1024**2)})
        name, args = "author_create_task", {"repository": repo["id"], "goal": "One effect", "key": "oversize-key"}
    else:
        original = s.tasks.get
        monkeypatch.setattr(s.tasks, "get", lambda *args, **kwargs: {**original(*args, **kwargs), "goal": "x" * (9 * 1024**2)})
        name, args = "author_get_task", {"task_id": task["id"]}
    result = request(p, name, args)
    assert result["isError"] and result["structuredContent"]["code"] == "RESOURCE_LIMIT"
    details = result["structuredContent"]["details"]
    assert details["boundary"] == ("effect_result_too_large" if write else "read_result_too_large")
    assert details["effect_may_exist"] == write and details["success_asserted"] is False
    assert details["task_id"].startswith("task_") and details["recovery_actions"]
    assert len(json.dumps(result)) < 5000


def test_author_http_version_header_and_exact_source_readback(setup_author):
    s, _, _, task, lease = setup_author
    text = "λ first\n" + "長" * 4000 + "\nlast failure\n"
    draft = Drafts(s.store).apply(task["id"], token=lease["token"], epoch=lease["epoch"], key="unicode-draft",
        expected_revision=0, expected_version=0, edits=[{"op": "create", "path": "unicode.py", "content": text}])
    p = MCP(s)
    args = {"draft_id": draft["draft_id"], "draft_version": draft["version"], "path": "unicode.py", "max_lines": 1}
    parts, line = [], 1
    while line:
        page = request(p, "author_read_file", {**args, "start_line": line}, role="reader")
        assert not page["isError"]
        # Default compatibility text and structured source are byte-identical.
        assert json.loads(page["content"][0]["text"]) == page["structuredContent"]
        parts.append(page["structuredContent"]["text"])
        line = page["structuredContent"]["next_start_line"]
    assert "".join(parts) == text
    stale = request(p, "author_read_file", {**args, "draft_version": draft["version"] + 1}, role="reader")
    assert stale["isError"] and stale["structuredContent"]["code"] == "NOT_FOUND"
    # A single oversized line must be explicit, not silently skipped.
    limited = request(p, "author_read_file", {**args, "start_line": 2, "max_bytes": 64}, role="reader")
    assert limited["isError"] and limited["structuredContent"]["code"] == "RESOURCE_LIMIT"
    with TestClient(create_app(s.store.home)) as client:
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-99-99"}}
        initialized = client.post("/mcp", json=message, headers=auth(s, "reader"))
        assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
        for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={**auth(s, "reader"), "MCP-Protocol-Version": version})
            assert response.status_code == 200
        denied = client.post("/mcp", json=message, headers={**auth(s, "reader"), "MCP-Protocol-Version": "2099-99-99"})
        assert denied.status_code == 400


@pytest.mark.parametrize("version", [None, 1, True, [], {}, "", " "])
def test_author_malformed_initialize_is_a_safe_protocol_error(setup_author, version):
    p = MCP(setup_author[0])
    reply = p.handle("reader", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}})
    assert reply["error"]["code"] == -32602
