import json
import os
import subprocess

import pytest

from patchbay.workspace.context import WorkspaceContext


def make_config(root):
    return {
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {
            "max_read_bytes": 10_000,
            "max_search_results": 10,
            "max_tree_entries": 100,
            "blocked_globs": [
                ".git",
                ".git/**",
                "**/.git/**",
                ".env",
                ".env.*",
                "**/.env",
                "**/.env.*",
                "**/*secret*",
            ],
        },
    }


def init_repo(root):
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


def write_skill(root, name, *, skill_name=None, description=""):
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    declared_name = skill_name or name
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"name: {declared_name}\n"
        f"description: {description}\n\n"
        f"# {declared_name}\n\n"
        "Use this skill for focused verification.\n",
        encoding="utf-8",
    )
    return skill_file


def test_open_workspace_returns_git_agents_and_bounded_tree(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "README.md").write_text("needle in readme\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("follow project rules\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle in secret file\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.open_summary({"include_tree": True})

    assert result["workspace_id"].startswith("ws_")
    assert result["git"]["is_git_repo"] is True
    assert result["agents_files"] == ["AGENTS.md"]
    assert "README.md" in result["tree"]["text"]
    assert ".env" not in result["tree"]["text"]


def test_read_file_returns_line_slice_and_redacts_secret_like_text(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("line one\npassword = dummy-secret-for-test\nline three\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.read_file({"file_path": "notes.txt", "start_line": 2, "end_line": 2})

    assert result["path"] == "notes.txt"
    assert result["start_line"] == 2
    assert result["end_line"] == 2
    assert "2 | password = [REDACTED_POSSIBLE_SECRET]" in result["text"]
    assert "dummy-secret-for-test" not in result["text"]


def test_read_file_line_slice_does_not_require_max_bytes_above_whole_file_size(tmp_path):
    target = tmp_path / "large-notes.txt"
    target.write_text("\n".join(f"line {i} " + ("x" * 200) for i in range(1, 80)), encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.read_file(
        {
            "file_path": "large-notes.txt",
            "start_line": 10,
            "end_line": 10,
            "max_bytes": 4000,
        }
    )

    assert result["bytes"] > 4000
    assert result["start_line"] == 10
    assert result["end_line"] == 10
    assert result["requested_end_line"] == 10
    assert result["max_bytes_applied"] == 4000
    assert "10 | line 10" in result["text"]
    assert "next_start_line" not in result


def test_read_file_full_large_file_is_paged_by_returned_slice(tmp_path):
    target = tmp_path / "large-notes.txt"
    target.write_text("\n".join(f"line {i} " + ("x" * 40) for i in range(1, 80)), encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.read_file({"file_path": "large-notes.txt", "max_bytes": 260})

    assert result["bytes"] > 260
    assert result["start_line"] == 1
    assert result["end_line"] < result["total_lines"]
    assert result["requested_end_line"] == result["total_lines"]
    assert result["truncated"] is True
    assert result["next_start_line"] == result["end_line"] + 1
    assert len(result["text"].encode("utf-8")) <= 260


def test_blocked_file_read_is_rejected(tmp_path):
    (tmp_path / ".env").write_text("hidden secret\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="blocked by safety rules"):
        context.read_file({"file_path": ".env"})


def test_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    os.symlink(outside, tmp_path / "outside-link.txt")

    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="symlink"):
        context.read_file({"file_path": "outside-link.txt"})


def test_search_repo_omits_blocked_files(tmp_path):
    (tmp_path / "README.md").write_text("needle in readme\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle in secret file\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.search_repo({"query": "needle"})

    assert result["matches"]
    assert any(match["path"] == "README.md" for match in result["matches"])
    assert all(match["path"] != ".env" for match in result["matches"])


def test_search_repo_timeout_returns_structured_partial_result(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("needle in readme\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))

    monkeypatch.setattr("patchbay.workspace.context.shutil.which", lambda name: "/usr/bin/rg")

    def fake_run(*args, **kwargs):
        error = subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))
        error.stdout = f"{tmp_path / 'README.md'}:1:needle in readme\n"
        raise error

    monkeypatch.setattr("patchbay.workspace.context.subprocess.run", fake_run)

    result = context.search_repo({"query": "needle", "timeout_ms": 1000})

    assert result["timed_out"] is True
    assert result["timeout_ms"] == 1000
    assert result["matches"][0]["path"] == "README.md"
    assert "delegate the broad repository search" in result["suggested_next"]


def test_read_binary_file_is_rejected(tmp_path):
    (tmp_path / "data.bin").write_bytes(b"abc\x00def")

    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="binary"):
        context.read_file({"file_path": "data.bin"})


def test_load_context_includes_agents_and_selected_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text("src rules\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("secret\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.load_context({
        "target_path": "src/app.py",
        "selected_paths": ["src/app.py", ".env"],
        "include_ai_bridge": False,
        "include_git": False,
    })

    assert result["agents_files"] == ["AGENTS.md", "src/AGENTS.md"]
    assert result["selected_files"] == ["src/app.py"]
    assert result["skipped_files"][0]["path"] == ".env"
    assert "root rules" in result["text"]
    assert "src rules" in result["text"]
    assert "print('hello')" in result["text"]


def test_skill_inventory_lists_workspace_and_global_without_absolute_paths(tmp_path, monkeypatch):
    home = tmp_path.parent / f"{tmp_path.name}-home"
    repo_skill = write_skill(tmp_path, "repo-skill", description="Repository skill")
    user_skill = home / ".codex" / "skills" / "user-skill" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("name: user-skill\ndescription: User skill\n\nUse globally.\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    context = WorkspaceContext(make_config(tmp_path))
    result = context.list_skills({"include_global_skills": True})
    serialized = json.dumps(result)

    assert result["skill_counts"]["workspace"] == 1
    assert result["skill_counts"]["user"] == 1
    assert {skill["name"] for skill in result["skill_inventory"]} == {"repo-skill", "user-skill"}
    assert "$WORKSPACE/skills/repo-skill/SKILL.md" in serialized
    assert "~/." in serialized
    assert str(tmp_path) not in serialized
    assert str(home) not in serialized
    assert str(repo_skill) not in serialized


def test_open_workspace_includes_skill_inventory_by_default(tmp_path):
    write_skill(tmp_path, "repo-skill", description="Repository skill")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.open_summary({"include_tree": False, "include_global_skills": False})

    assert result["skills"] == ["repo-skill"]
    assert result["skill_inventory"][0]["path"] == "$WORKSPACE/skills/repo-skill/SKILL.md"
    assert result["skill_counts"]["workspace"] == 1


def test_workspace_alias_maps_canonical_path_to_local_copy(tmp_path):
    init_repo(tmp_path)
    canonical = "/operator/canonical/Documents"
    config = make_config(tmp_path)
    config["repositories"]["aliases"] = [
        {
            "canonical": canonical,
            "local": str(tmp_path),
            "description": "Remote copy of canonical Documents workspace",
        }
    ]

    context = WorkspaceContext(config)
    result = context.open_summary({"repo": canonical, "include_tree": False, "include_global_skills": False})

    assert result["root"] == str(tmp_path.resolve())
    assert result["workspace_alias"]["canonical"] == canonical
    assert result["workspace_alias"]["local"] == str(tmp_path.resolve())
    assert result["workspace_alias"]["description"] == "Remote copy of canonical Documents workspace"


def test_list_workspaces_includes_configured_aliases(tmp_path):
    init_repo(tmp_path)
    canonical = "/operator/canonical/Documents"
    config = make_config(tmp_path)
    config["repositories"]["aliases"] = {canonical: str(tmp_path)}

    context = WorkspaceContext(config)
    result = context.list_workspaces({})

    assert any(
        item.get("workspace_alias", {}).get("canonical") == canonical
        and item.get("root") == str(tmp_path.resolve())
        for item in result["workspaces"]
    )


def test_list_workspaces_discovers_repositories_under_configured_roots(tmp_path):
    base = tmp_path / "github"
    retail = base / "RetailMind"
    retail.mkdir(parents=True)
    init_repo(retail)
    (retail / "AGENTS.md").write_text("project rules\n", encoding="utf-8")
    (base / "node_modules" / "IgnoredRepo").mkdir(parents=True)
    (base / "node_modules" / "IgnoredRepo" / "README.md").write_text("ignored\n", encoding="utf-8")

    config = make_config(base)
    config["repositories"]["default"] = str(base)
    config["repositories"]["allowed"] = [str(base)]
    config["repositories"]["discovery_roots"] = [str(base)]
    context = WorkspaceContext(config)

    result = context.list_workspaces({"query": "retail", "discover": True})

    discovered = [item for item in result["workspaces"] if item.get("source") == "discovered"]
    assert result["discovered_count"] == 1
    assert discovered[0]["root"] == str(retail.resolve())
    assert discovered[0]["name"] == "RetailMind"
    assert ".git" in discovered[0]["markers"]
    assert "IgnoredRepo" not in str(result)
    assert result["paths_returned"] == "configured-and-discovered"


def test_open_workspace_missing_name_suggests_discovered_repo(tmp_path):
    base = tmp_path / "github"
    retail = base / "RetailMind"
    retail.mkdir(parents=True)
    init_repo(retail)
    config = make_config(base)
    config["repositories"]["allowed"] = [str(base)]
    config["repositories"]["discovery_roots"] = [str(base)]
    context = WorkspaceContext(config)

    with pytest.raises(ValueError) as error:
        context.open_workspace("RetailMind")

    message = str(error.value)
    assert "Candidate workspace(s)" in message
    assert str(retail.resolve()) in message
    assert "repo_path" in message


def test_load_skill_reads_only_discovered_skill_by_name(tmp_path):
    write_skill(tmp_path, "repo-skill", description="Repository skill")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.load_skill({"name": "repo-skill", "include_global_skills": False})

    assert result["skill"]["name"] == "repo-skill"
    assert result["skill"]["source"] == "workspace"
    assert result["skill"]["path"] == "$WORKSPACE/skills/repo-skill/SKILL.md"
    assert "Use this skill for focused verification." in result["text"]
    assert result["paths_returned"] == "sanitized"


def test_load_skill_requires_disambiguation_for_duplicate_names(tmp_path, monkeypatch):
    home = tmp_path.parent / f"{tmp_path.name}-home"
    write_skill(tmp_path, "workspace-shared", skill_name="shared", description="Workspace shared")
    user_skill = home / ".codex" / "skills" / "user-shared" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("name: shared\ndescription: User shared\n\nUse globally.\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="Multiple skills named shared"):
        context.load_skill({"name": "shared", "include_global_skills": True})

    result = context.load_skill({"name": "shared", "source": "workspace", "include_global_skills": True})

    assert result["skill"]["source"] == "workspace"


def test_skill_inventory_skips_symlinked_skill_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("name: outside-skill\n\nDo not load.\n", encoding="utf-8")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    os.symlink(outside, skills_dir / "outside-skill")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.list_skills({"include_global_skills": False})

    assert result["skill_inventory"] == []


def test_export_context_writes_only_ai_bridge_bundle(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme context\n", encoding="utf-8")

    context = WorkspaceContext(make_config(tmp_path))
    result = context.export_context({
        "title": "Probe Context",
        "selected_paths": ["README.md"],
        "include_ai_bridge": False,
        "include_git": False,
    })

    bundle = tmp_path / ".ai-bridge" / "pro-context.md"
    assert result["path"] == ".ai-bridge/pro-context.md"
    assert bundle.exists()
    assert "Probe Context" in bundle.read_text(encoding="utf-8")
    assert "readme context" in bundle.read_text(encoding="utf-8")


def test_write_handoff_scaffolds_and_appends(tmp_path):
    context = WorkspaceContext(make_config(tmp_path))

    first = context.write_handoff({"plan": "Implement the thing.", "title": "Plan One"})
    second = context.write_handoff({"plan": "Add tests.", "append": True})

    plan = (tmp_path / ".ai-bridge" / "current-plan.md").read_text(encoding="utf-8")
    assert first["path"] == ".ai-bridge/current-plan.md"
    assert second["append"] is True
    assert "Plan One" in plan
    assert "Implement the thing." in plan
    assert "Add tests." in plan
    assert (tmp_path / ".ai-bridge" / "agent-status.md").exists()


def test_write_handoff_blocks_secret_like_content(tmp_path):
    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="Secret-looking content"):
        context.write_handoff({"plan": "password = dummy-secret-for-test"})


def test_write_handoff_rejects_symlinked_ai_bridge(tmp_path):
    outside = tmp_path.parent / "outside-bridge"
    outside.mkdir()
    os.symlink(outside, tmp_path / ".ai-bridge")
    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="parent outside the workspace"):
        context.write_handoff({"plan": "Implement safely."})


def test_read_handoff_status_and_diff(tmp_path):
    context = WorkspaceContext(make_config(tmp_path))
    context.write_handoff({"plan": "Implement safely."})
    (tmp_path / ".ai-bridge" / "implementation-diff.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")

    status = context.read_handoff_status({})
    diff = context.read_handoff_diff({})

    assert ".ai-bridge/current-plan.md" in status["files"]
    assert "Implement safely." in status["text"]
    assert diff["path"] == ".ai-bridge/implementation-diff.patch"
    assert "diff --git" in diff["text"]


def test_write_file_creates_text_file_and_returns_diff(tmp_path):
    context = WorkspaceContext(make_config(tmp_path))

    result = context.write_file({"file_path": "src/app.py", "content": "print('hello')\n"})

    assert result["path"] == "src/app.py"
    assert result["existed"] is False
    assert result["additions"] >= 1
    assert "+print('hello')" in result["diff"]
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('hello')\n"


def test_show_changes_can_scope_status_and_diff_to_path(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("old a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("old b\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "a.txt").write_text("new a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("new b\n", encoding="utf-8")
    context = WorkspaceContext(make_config(tmp_path))

    result = context.show_changes({"file_path": "a.txt", "include_diff": True})

    assert result["path"] == "a.txt"
    assert "a.txt" in result["status"]
    assert "b.txt" not in result["status"]
    assert "a.txt" in result["diff"]
    assert "b.txt" not in result["diff"]


def test_write_file_rejects_blocked_path_and_secret_content(tmp_path):
    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="Path is blocked"):
        context.write_file({"file_path": ".env", "content": "ok"})

    with pytest.raises(ValueError, match="Secret-looking content"):
        context.write_file({"file_path": "notes.txt", "content": "password = fixture-value"})


def test_edit_file_replaces_exact_text_and_returns_diff(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    context = WorkspaceContext(make_config(tmp_path))

    result = context.edit_file({"file_path": "notes.txt", "old_text": "beta", "new_text": "gamma"})

    assert result["path"] == "notes.txt"
    assert result["replacements"] == 1
    assert "-beta" in result["diff"]
    assert "+gamma" in result["diff"]
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_edit_file_rejects_ambiguous_replacement(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    context = WorkspaceContext(make_config(tmp_path))

    with pytest.raises(ValueError, match="matched 2 times"):
        context.edit_file({"file_path": "notes.txt", "old_text": "same", "new_text": "other"})
