"""Context loading must make progress without repeating or dropping source."""
import asyncio
import hashlib
import json

import pytest

from patchbay.protocol.mcp import MCPProtocol
from patchbay.workspace.context import WorkspaceContext


def context_for(root):
    return WorkspaceContext({
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {"max_read_bytes": 10000, "blocked_globs": []},
    })


@pytest.mark.parametrize("content", [b"", b"a", b"a\n", b"a\n\n", b"a\r\n", b"\n", "雪\n".encode()])
def test_exact_source_reaches_logical_eof(tmp_path, content):
    (tmp_path / "source.txt").write_bytes(content)
    context = context_for(tmp_path)
    result = context.read_file({"file_path": "source.txt"})
    lines = content.decode().split("\n")
    assert result["end_line"] == result["total_lines"] == len(lines)
    assert result["truncated"] is False
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["bytes"] == len(content)
    assert [row.split(" | ", 1)[1] for row in result["text"].split("\n")] == [line.removesuffix("\r") for line in lines]
    last = context.read_file({"file_path": "source.txt", "start_line": len(lines), "end_line": len(lines)})
    assert last["end_line"] == len(lines)
    assert "next_start_line" not in last


def test_small_pages_reach_final_empty_line_without_repeating(tmp_path):
    (tmp_path / "source.txt").write_text("first\nsecond\n", encoding="utf-8")
    context, start, recovered = context_for(tmp_path), 1, []
    for _ in range(3):
        result = context.read_file({"file_path": "source.txt", "start_line": start, "max_bytes": 12})
        recovered.extend(row.split(" | ", 1)[1] for row in result["text"].split("\n"))
        if "next_start_line" not in result:
            break
        assert result["next_start_line"] > start
        start = result["next_start_line"]
    assert recovered == ["first", "second", ""]
    assert result["end_line"] == result["total_lines"] == 3


def test_repeated_selectors_load_source_once_and_keep_instruction_chain(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("Keep source exact.")
    (tmp_path / "one.py").write_text("unique_source = 1")
    (tmp_path / "two.py").write_text("second_source = 2")
    context, reads = context_for(tmp_path), []
    original_read = context.read_file

    def read(args):
        reads.append(args["file_path"])
        return original_read(args)

    monkeypatch.setattr(context, "read_file", read)
    result = context.load_context({"selected_paths": ["one.py", "one.py", "two.py", "one.py"], "include_ai_bridge": False, "include_git": False})
    assert result["selected_files"] == ["one.py", "two.py"]
    assert reads == ["AGENTS.md", "one.py", "two.py"]
    assert result["text"].count("unique_source = 1") == 1
    assert "Keep source exact." in result["text"]


def test_unicode_result_is_lossless_without_ascii_expansion():
    payload = {"path": "source.txt", "text": "雪 café 🚀\n" * 100, "next_start_line": 101, "truncated": True}

    class Handler:
        async def handle_tool_call(self, *args, **kwargs):
            return payload

    protocol = MCPProtocol({}, Handler())
    result = asyncio.run(protocol._handle_tools_call({"name": "codex_read_file", "arguments": {"file_path": "source.txt"}}))
    text = result["content"][0]["text"]
    assert json.loads(text) == result["structuredContent"] == payload
    assert len(text.encode()) < len(json.dumps(payload, separators=(",", ":")).encode())
    assert "雪" in text


def test_unicode_bulk_result_keeps_the_existing_summary_boundary():
    from patchbay.protocol.mcp import MAX_MCP_TEXT_RESULT_CHARS

    payload = {"text": "雪🚀" * 1000, "truncated": True, "next_start_line": 101}
    assert len(json.dumps(payload, separators=(",", ":"))) > MAX_MCP_TEXT_RESULT_CHARS
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) < MAX_MCP_TEXT_RESULT_CHARS
    class Handler:
        async def handle_tool_call(self, *args, **kwargs):
            return payload

    result = asyncio.run(MCPProtocol({}, Handler())._handle_tools_call({
        "name": "codex_read_file", "arguments": {"file_path": "source.txt"},
    }))
    assert result["structuredContent"] == payload
    text = result["content"][0]["text"]
    assert "Full payload is in structuredContent" in text
    assert "雪" not in text
    assert '"next_start_line":101' in text


def test_discovery_does_not_descend_into_explicitly_blocked_subtrees(tmp_path, monkeypatch):
    from patchbay.workspace import discovery

    for name in ["src/AGENTS.md", ".venv/deep/AGENTS.md", "cache/AGENTS.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules")
    scanned = []
    original = discovery.os.scandir

    def scan(path):
        scanned.append(str(path))
        assert ".venv" not in str(path)
        return original(path)

    monkeypatch.setattr(discovery.os, "scandir", scan)
    # A name-only exclusion must not newly exclude its descendants.
    paths = list(discovery.context_paths(tmp_path, [".venv/**", "cache"]))
    assert tmp_path / "src/AGENTS.md" in paths
    assert tmp_path / "cache/AGENTS.md" in paths
    assert not any("deep" in str(path) for path in paths)
    assert str(tmp_path / "src") in scanned


def test_focused_discovery_keeps_workspace_relative_policy(tmp_path):
    from patchbay.workspace.discovery import context_paths

    for name in ["src/generated/deep/AGENTS.md", "src/AGENTS.md"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rules")
    paths = list(context_paths(tmp_path / "src", ["src/generated/**"], policy_root=tmp_path))
    assert tmp_path / "src/AGENTS.md" in paths
    assert tmp_path / "src/generated/deep/AGENTS.md" not in paths
