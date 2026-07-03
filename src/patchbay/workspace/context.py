"""Workspace context and safe repository inspection helpers."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import difflib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from patchbay.security import public_error_message, redact_text, validate_allowed_path


DEFAULT_BLOCKED_GLOBS = [
    ".git",
    ".git/**",
    "**/.git/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*private_key*",
    "**/*secret*",
    "**/*token*",
    "node_modules",
    "node_modules/**",
    "__pycache__",
    "__pycache__/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".venv",
    ".venv/**",
    "venv",
    "venv/**",
    "dist",
    "dist/**",
    "build",
    "build/**",
    "logs",
    "logs/**",
    "worktrees",
    "worktrees/**",
]


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    requested_root: str = ""
    alias: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class SkillRecord:
    name: str
    source: str
    display_path: str
    abs_path: Path
    description: str = ""


class WorkspaceContext:
    """CodexPro-style workspace inspection with PatchBay policy boundaries."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        security = config.get("security", {})
        if "blocked_globs" in security:
            self.blocked_globs = list(security.get("blocked_globs") or [])
        else:
            self.blocked_globs = list(DEFAULT_BLOCKED_GLOBS)
        self.max_read_bytes = int(security.get("max_read_bytes", 200_000))
        self.max_write_bytes = int(security.get("max_write_bytes", 500_000))
        self.max_search_results = int(security.get("max_search_results", 100))
        self.max_tree_entries = int(security.get("max_tree_entries", 500))
        self.max_skill_count = int(security.get("max_skill_count", 120))
        self.max_skill_bytes = int(security.get("max_skill_bytes", 40_000))
        self.context_dir = str(security.get("context_dir", ".ai-bridge")).strip() or ".ai-bridge"

    def _allowed_roots(self) -> list[str]:
        return self.config.get("repositories", {}).get("allowed") or []

    def _workspace_aliases(self) -> list[Dict[str, str]]:
        raw_aliases = self.config.get("repositories", {}).get("aliases") or []
        if isinstance(raw_aliases, dict):
            raw_aliases = [
                {"canonical": canonical, "local": local}
                for canonical, local in raw_aliases.items()
            ]
        aliases: list[Dict[str, str]] = []
        for raw in raw_aliases:
            if not isinstance(raw, dict):
                continue
            canonical = str(raw.get("canonical") or raw.get("from") or raw.get("source") or "").rstrip("/")
            local = str(raw.get("local") or raw.get("to") or raw.get("target") or "").rstrip("/")
            if not canonical or not local:
                continue
            aliases.append(
                {
                    "canonical": canonical,
                    "local": local,
                    "description": str(raw.get("description") or raw.get("note") or "").strip(),
                }
            )
        return aliases

    def _resolve_workspace_alias(self, requested: str) -> tuple[str, Optional[Dict[str, str]]]:
        raw = str(requested)
        normalized = raw.rstrip("/")
        for alias in self._workspace_aliases():
            canonical = alias["canonical"]
            if normalized == canonical:
                return alias["local"], alias
            prefix = canonical + "/"
            if normalized.startswith(prefix):
                suffix = normalized[len(prefix):]
                return str(Path(alias["local"]) / suffix), alias
        return raw, None

    def open_workspace(self, repo_path: Optional[str] = None) -> Workspace:
        requested = repo_path or self.config.get("repositories", {}).get("default")
        if not requested:
            raise ValueError("No workspace path provided and no default repository configured")

        resolved_requested, alias = self._resolve_workspace_alias(str(requested))
        root = validate_allowed_path(str(resolved_requested), self._allowed_roots())
        if not root.exists():
            raise ValueError(f"Workspace root does not exist: {repo_path or requested}")
        if not root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {repo_path or requested}")

        real_root = root.resolve()
        digest = hashlib.sha256(str(real_root).encode("utf-8")).hexdigest()[:24]
        return Workspace(
            id=f"ws_{digest}",
            root=real_root,
            requested_root=str(requested),
            alias=alias,
        )

    def is_blocked_relative_path(self, rel_path: str) -> bool:
        rel = self._normalize_rel(rel_path)
        if rel in {"", "."}:
            return False
        basename = rel.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(basename, pattern)
            for pattern in self.blocked_globs
        )

    def resolve_path(self, workspace: Workspace, input_path: str = ".") -> tuple[Path, str]:
        raw_path = input_path or "."
        expanded = Path(raw_path).expanduser()
        candidate = expanded if expanded.is_absolute() else workspace.root / expanded
        resolved = Path(os.path.abspath(candidate))

        self._require_under_root(resolved, workspace.root, f"Path escapes workspace root: {raw_path}")
        rel = self._display_path(resolved, workspace.root)
        self._assert_not_blocked(rel)

        if resolved.exists():
            real = resolved.resolve(strict=True)
            self._require_under_root(
                real,
                workspace.root,
                f"Path resolves outside workspace root through a symlink: {raw_path}",
            )
            self._assert_not_blocked(self._display_path(real, workspace.root))

        return resolved, rel

    def resolve_write_path(self, workspace: Workspace, input_path: str) -> tuple[Path, str]:
        raw_path = input_path or "."
        expanded = Path(raw_path).expanduser()
        candidate = expanded if expanded.is_absolute() else workspace.root / expanded
        resolved = Path(os.path.abspath(candidate))

        self._require_under_root(resolved, workspace.root, f"Write path escapes workspace root: {raw_path}")
        rel = self._display_path(resolved, workspace.root)
        self._assert_not_blocked(rel)

        closest_parent = resolved.parent
        while not closest_parent.exists() and closest_parent != closest_parent.parent:
            closest_parent = closest_parent.parent
        real_parent = closest_parent.resolve(strict=True)
        self._require_under_root(
            real_parent,
            workspace.root,
            f"Write path resolves through a parent outside the workspace: {raw_path}",
        )
        self._assert_not_blocked(self._display_path(real_parent, workspace.root))

        if resolved.exists():
            real = resolved.resolve(strict=True)
            self._require_under_root(
                real,
                workspace.root,
                f"Write path resolves outside workspace root through a symlink: {raw_path}",
            )
            self._assert_not_blocked(self._display_path(real, workspace.root))

        return resolved, rel

    def open_summary(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        tree = None
        if args.get("include_tree", True):
            tree = self.repo_tree(
                {
                    "repo": str(workspace.root),
                    "path": ".",
                    "max_depth": args.get("max_depth", 2),
                    "max_entries": args.get("max_entries", 200),
                    "include_hidden": args.get("include_hidden", False),
                }
            )

        agents_files = self.find_agents_files(workspace)
        skills = self.list_skills(
            {
                "repo": str(workspace.root),
                "include_global_skills": args.get("include_global_skills", True),
                "max_skills": args.get("max_skills", self.max_skill_count),
            }
        ) if args.get("include_skills", True) else {
            "skills": [],
            "skill_inventory": [],
            "skill_counts": self._skill_counts([]),
        }
        result = {
            "workspace_id": workspace.id,
            "root": str(workspace.root),
            "git": self.git_summary(workspace),
            "agents_files": agents_files,
            "skills": [item["name"] for item in skills["skill_inventory"]],
            "skill_inventory": skills["skill_inventory"],
            "skill_counts": skills["skill_counts"],
            "blocked_globs_count": len(self.blocked_globs),
            "tree": tree,
        }
        if workspace.alias:
            result["workspace_alias"] = {
                "requested": workspace.requested_root,
                "canonical": workspace.alias["canonical"],
                "local": str(workspace.root),
                "description": workspace.alias.get("description", ""),
            }
        return result

    def list_skills(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        include_global = bool(args.get("include_global_skills", True))
        max_skills = self._bounded_int(args.get("max_skills"), self.max_skill_count, 1, 500)
        records = self._discover_skill_records(workspace, include_global=include_global, max_skills=max_skills)
        inventory = [self._public_skill(record) for record in records]
        counts = self._skill_counts(inventory)
        lines = [
            f"- {item['name']} [{item['source']}]" + (f" - {item['description']}" if item.get("description") else "")
            for item in inventory
        ]
        return {
            "workspace_id": workspace.id,
            "skills": [item["name"] for item in inventory],
            "skill_inventory": inventory,
            "skill_counts": counts,
            "skill_count": len(inventory),
            "paths_returned": "sanitized",
            "include_global_skills": include_global,
            "truncated": len(records) >= max_skills,
            "text": "\n".join(lines) if lines else "No skills discovered.",
        }

    def load_skill(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        requested_source = args.get("source")
        if requested_source and requested_source not in {"workspace", "user", "plugin", "other"}:
            raise ValueError("source must be one of: workspace, user, plugin, other")
        requested_path = str(args.get("path") or "").strip()
        include_global = bool(args.get("include_global_skills", True))
        max_skills = self._bounded_int(args.get("max_skills"), self.max_skill_count, 1, 500)
        max_bytes = self._bounded_int(args.get("max_bytes"), self.max_skill_bytes, 1_000, 100_000)

        records = self._discover_skill_records(workspace, include_global=include_global, max_skills=max_skills)
        matches = [
            record
            for record in records
            if record.name == name
            and (not requested_source or record.source == requested_source)
            and (not requested_path or record.display_path == requested_path)
        ]
        if not matches:
            near = [
                f"{record.name} [{record.source}]"
                for record in records
                if name.lower() in record.name.lower()
            ][:8]
            suffix = f" at {requested_path}" if requested_path else ""
            hint = f". Similar skills: {', '.join(near)}" if near else ""
            raise ValueError(f"Skill not found: {name}{suffix}{hint}")
        if len(matches) > 1:
            choices = "; ".join(f"{record.name} [{record.source}] at {record.display_path}" for record in matches)
            raise ValueError(f"Multiple skills named {name} were found. Pass source and path to choose one: {choices}")

        record = matches[0]
        if record.abs_path.name != "SKILL.md" or record.abs_path.is_symlink():
            raise ValueError(f"Refusing to load non-skill file: {record.display_path}")
        total_bytes = record.abs_path.stat().st_size
        raw = record.abs_path.read_bytes()[:max_bytes]
        text = redact_text(raw.decode("utf-8", errors="replace"))
        truncated = total_bytes > len(raw)
        public = self._public_skill(record)
        truncation_note = "\n\n[truncated: increase max_bytes if more context is required]" if truncated else ""
        display_text = (
            "# Load Skill\n\n"
            f"Name: {public['name']}\n"
            f"Source: {public['source']}\n"
            f"Path: {public['path']}\n"
            f"Bytes: {len(raw)}/{total_bytes}\n\n"
            "```markdown\n"
            f"{text}"
            f"{truncation_note}\n"
            "```"
        )
        return {
            "workspace_id": workspace.id,
            "skill": public,
            "bytes": len(raw),
            "total_bytes": total_bytes,
            "truncated": truncated,
            "text": text,
            "display_text": display_text,
            "paths_returned": "sanitized",
        }

    def repo_tree(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        target, rel = self.resolve_path(workspace, args.get("path") or ".")
        if not target.is_dir():
            raise ValueError(f"Not a directory: {rel}")

        max_depth = max(1, min(int(args.get("max_depth") or 3), 8))
        max_entries = max(1, min(int(args.get("max_entries") or self.max_tree_entries), self.max_tree_entries))
        include_hidden = bool(args.get("include_hidden", False))

        lines = ["." if rel == "." else f"{rel}/"]
        state = {"entries": 0, "truncated": False}

        def walk(directory: Path, depth: int, prefix: str) -> None:
            if depth >= max_depth or state["truncated"]:
                return
            children = []
            for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                child_rel = self._display_path(child.resolve(strict=False), workspace.root)
                if not include_hidden and any(part.startswith(".") for part in child_rel.split("/")):
                    continue
                if self.is_blocked_relative_path(child_rel):
                    continue
                children.append(child)

            for index, child in enumerate(children):
                if state["entries"] >= max_entries:
                    state["truncated"] = True
                    return
                branch = "└── " if index == len(children) - 1 else "├── "
                next_prefix = prefix + ("    " if index == len(children) - 1 else "│   ")
                display_name = f"{child.name}/" if child.is_dir() else child.name
                lines.append(f"{prefix}{branch}{display_name}")
                state["entries"] += 1
                if child.is_dir() and not child.is_symlink():
                    walk(child, depth + 1, next_prefix)

        walk(target, 0, "")
        if state["truncated"]:
            lines.append(f"...[tree truncated after {state['entries']} entries]")

        return {
            "workspace_id": workspace.id,
            "path": rel,
            "text": "\n".join(lines),
            "entries": state["entries"],
            "truncated": state["truncated"],
        }

    def read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        file_path = args.get("file_path")
        if not file_path:
            raise ValueError("file_path is required")
        target, rel = self.resolve_path(workspace, str(file_path))
        self._assert_text_file(target)

        max_bytes = self._bounded_int(args.get("max_bytes"), self.max_read_bytes, 1, self.max_read_bytes)
        size = target.stat().st_size
        digest, total_lines = self._text_file_digest_and_line_count(target)
        start_line = max(1, int(args.get("start_line") or 1))
        end_line = min(total_lines, int(args.get("end_line") or total_lines))
        if end_line < start_line:
            raise ValueError(f"end_line ({end_line}) must be >= start_line ({start_line})")

        width = len(str(end_line))
        selected: list[str] = []
        used_bytes = 0
        returned_end_line = start_line - 1
        next_start_line: int | None = None

        with target.open("r", encoding="utf-8", newline=None) as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number < start_line:
                    continue
                if line_number > end_line:
                    break
                clean_line = line.rstrip("\n").rstrip("\r")
                rendered = f"{str(line_number).rjust(width)} | {redact_text(clean_line)}"
                prefix = "\n" if selected else ""
                rendered_bytes = (prefix + rendered).encode("utf-8")
                if used_bytes + len(rendered_bytes) > max_bytes:
                    if not selected:
                        available = max(0, max_bytes - len(prefix.encode("utf-8")))
                        clipped = self._clip_text_bytes(rendered, available)
                        selected.append(prefix + clipped if prefix else clipped)
                        returned_end_line = line_number
                        next_start_line = line_number + 1 if line_number < end_line else None
                    else:
                        next_start_line = line_number
                    break
                selected.append(prefix + rendered if prefix else rendered)
                used_bytes += len(rendered_bytes)
                returned_end_line = line_number

        numbered = "".join(selected)
        if returned_end_line < start_line:
            returned_end_line = start_line - 1

        result = {
            "workspace_id": workspace.id,
            "path": rel,
            "text": numbered,
            "start_line": start_line,
            "end_line": returned_end_line,
            "requested_end_line": end_line,
            "total_lines": total_lines,
            "bytes": size,
            "sha256": digest,
            "max_bytes_applied": max_bytes,
            "truncated": start_line > 1 or returned_end_line < total_lines,
        }
        if next_start_line:
            result["next_start_line"] = next_start_line
        return result

    def search_repo(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "")
        if not query:
            raise ValueError("query is required")

        workspace = self.open_workspace(args.get("repo"))
        root, _ = self.resolve_path(workspace, args.get("path") or ".")
        regex = bool(args.get("regex", False))
        include_hidden = bool(args.get("include_hidden", False))
        max_results = max(1, min(int(args.get("max_results") or self.max_search_results), self.max_search_results))
        glob = args.get("glob")

        if shutil.which("rg"):
            return self._search_with_rg(workspace, root, query, regex, include_hidden, max_results, glob)
        return self._search_with_python(workspace, root, query, regex, include_hidden, max_results, glob)

    def load_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        target_path = str(args.get("target_path") or ".")
        self.resolve_path(workspace, target_path)
        selected_paths = [str(path) for path in args.get("selected_paths") or []]
        max_file_bytes = min(int(args.get("max_file_bytes") or 60_000), self.max_read_bytes)

        agents = self.read_agents_chain(workspace, target_path)
        selected = []
        skipped = []
        for rel in selected_paths:
            try:
                selected.append(self.read_file({"repo": str(workspace.root), "file_path": rel, "max_bytes": max_file_bytes}))
            except Exception as error:
                skipped.append({"path": rel, "reason": public_error_message(error, default="Path could not be read.", allow_details=True)})

        ai_bridge = None
        if args.get("include_ai_bridge", True):
            ai_bridge = self.read_handoff_status({"repo": str(workspace.root), "create_if_missing": False})

        git = self.git_summary(workspace) if args.get("include_git", True) else None
        diff = self.git_diff(workspace) if args.get("include_diff", False) else None

        sections = [
            "# Codex Context",
            "",
            f"Workspace: {workspace.id}",
            f"Target path: {target_path}",
            "",
            "## AGENTS Instructions",
            "",
            agents["text"],
        ]
        if git is not None:
            sections.extend(["", "## Git Summary", "", "```json", self._safe_json(git), "```"])
        if diff is not None:
            sections.extend(["", "## Git Diff", "", "```diff", diff, "```"])
        if ai_bridge is not None:
            sections.extend(["", "## AI Bridge", "", ai_bridge["text"]])
        if selected:
            file_chunks = []
            for item in selected:
                file_chunks.append(f"### {item['path']}\n\n```text\n{item['text']}\n```")
            sections.extend(["", "## Selected Files", "", "\n\n".join(file_chunks)])
        if skipped:
            sections.extend(["", "## Skipped Files", "", self._safe_json(skipped)])

        return {
            "workspace_id": workspace.id,
            "target_path": target_path,
            "text": "\n".join(sections).strip() + "\n",
            "agents_files": agents["files"],
            "selected_files": [item["path"] for item in selected],
            "skipped_files": skipped,
            "ai_bridge_files": ai_bridge["files"] if ai_bridge else [],
        }

    def export_context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        self.ensure_ai_bridge(workspace)
        context = self.load_context({
            **args,
            "repo": str(workspace.root),
            "include_ai_bridge": args.get("include_ai_bridge", True),
            "include_git": args.get("include_git", True),
            "include_diff": args.get("include_diff", False),
        })
        title = str(args.get("title") or "PatchBay Context Bundle")
        markdown = f"# {title}\n\n{context['text']}"
        path, rel = self._write_ai_bridge_file(workspace, "pro-context.md", markdown, append=False)
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "bytes": path.stat().st_size,
            "selected_files": context["selected_files"],
            "skipped_files": context["skipped_files"],
            "truncated": False,
        }

    def write_handoff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        plan = str(args.get("plan") or "")
        if not plan.strip():
            raise ValueError("plan is required")
        self._assert_safe_write_text(plan)
        self.ensure_ai_bridge(workspace)

        title = str(args.get("title") or "Current Plan")
        agent = str(args.get("agent") or "codex")
        model = str(args.get("model") or "").strip()
        append = bool(args.get("append", False))
        body = [
            f"# {title}",
            "",
            f"Target agent: {agent}",
            *( [f"Model hint: {model}"] if model else [] ),
            "",
            plan.strip(),
            "",
        ]
        path, rel = self._write_ai_bridge_file(workspace, "current-plan.md", "\n".join(body), append=append)
        status_path, status_rel = self._write_ai_bridge_file(
            workspace,
            "agent-status.md",
            "# Agent Status\n\nHandoff plan written. No local implementation agent has reported status yet.\n",
            append=False,
        )
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "status_path": status_rel,
            "bytes": path.stat().st_size,
            "status_bytes": status_path.stat().st_size,
            "agent": agent,
            "append": append,
            "note": "Handoff files were written only under .ai-bridge. No local agent command was executed.",
        }

    def read_handoff_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        if args.get("create_if_missing", False):
            self.ensure_ai_bridge(workspace)

        rel_files = [
            f"{self.context_dir}/current-plan.md",
            f"{self.context_dir}/agent-status.md",
            f"{self.context_dir}/codex-status.md",
            f"{self.context_dir}/decisions.md",
            f"{self.context_dir}/open-questions.md",
            f"{self.context_dir}/execution-log.jsonl",
        ]
        chunks = []
        files = []
        for rel in rel_files:
            try:
                read = self.read_file({"repo": str(workspace.root), "file_path": rel, "max_bytes": 80_000})
                chunks.append(f"--- {rel} ---\n{read['text']}")
                files.append(rel)
            except Exception as error:
                chunks.append(f"--- {rel} ---\n[unreadable: {error}]")
        return {
            "workspace_id": workspace.id,
            "files": files,
            "text": "\n\n".join(chunks),
        }

    def read_handoff_diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        try:
            read = self.read_file({
                "repo": str(workspace.root),
                "file_path": f"{self.context_dir}/implementation-diff.patch",
                "max_bytes": 200_000,
            })
            return {
                "workspace_id": workspace.id,
                "path": read["path"],
                "text": read["text"],
                "bytes": read["bytes"],
            }
        except Exception as error:
            return {
                "workspace_id": workspace.id,
                "path": f"{self.context_dir}/implementation-diff.patch",
                "text": "",
                "bytes": 0,
                "missing": True,
                "message": public_error_message(error, default="Implementation diff is unavailable.", allow_details=True),
            }

    def list_workspaces(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return configured workspaces without exposing arbitrary local paths."""
        workspaces = []
        roots = list(self._allowed_roots())
        default_root = self.config.get("repositories", {}).get("default")
        if default_root and default_root not in roots:
            roots.insert(0, default_root)

        for root in roots[:50]:
            try:
                workspace = self.open_workspace(str(root))
                item = {
                    "workspace_id": workspace.id,
                    "root": str(workspace.root),
                    "default": str(Path(default_root).expanduser().resolve()) == str(workspace.root)
                    if default_root
                    else False,
                    "git": self.git_summary(workspace),
                }
                if workspace.alias:
                    item["workspace_alias"] = {
                        "requested": workspace.requested_root,
                        "canonical": workspace.alias["canonical"],
                        "local": str(workspace.root),
                        "description": workspace.alias.get("description", ""),
                    }
                workspaces.append(item)
            except Exception as error:
                workspaces.append(
                    {
                        "workspace_id": "",
                        "root": str(root),
                        "default": str(root) == str(default_root),
                        "error": public_error_message(error, default="Workspace could not be opened.", allow_details=True),
                    }
                )
        for alias in self._workspace_aliases():
            if alias["canonical"] in roots:
                continue
            try:
                workspace = self.open_workspace(alias["canonical"])
                workspaces.append(
                    {
                        "workspace_id": workspace.id,
                        "root": str(workspace.root),
                        "default": False,
                        "git": self.git_summary(workspace),
                        "workspace_alias": {
                            "requested": alias["canonical"],
                            "canonical": alias["canonical"],
                            "local": str(workspace.root),
                            "description": alias.get("description", ""),
                        },
                    }
                )
            except Exception as error:
                workspaces.append(
                    {
                        "workspace_id": "",
                        "root": alias["local"],
                        "default": False,
                        "workspace_alias": {
                            "requested": alias["canonical"],
                            "canonical": alias["canonical"],
                            "local": alias["local"],
                            "description": alias.get("description", ""),
                        },
                        "error": public_error_message(error, default="Workspace alias could not be opened.", allow_details=True),
                    }
                )
        return {
            "workspaces": workspaces,
            "count": len(workspaces),
            "truncated": len(roots) > 50,
            "paths_returned": "configured-only",
        }

    def workspace_snapshot(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return a review-oriented snapshot of the current workspace."""
        workspace = self.open_workspace(args.get("repo"))
        tree = self.repo_tree(
            {
                "repo": str(workspace.root),
                "path": args.get("path") or ".",
                "max_depth": args.get("max_depth", 3),
                "max_entries": args.get("max_entries", 300),
                "include_hidden": args.get("include_hidden", False),
            }
        )
        status = self.git_status_text({"repo": str(workspace.root)})
        recent_commits = self.git_log(workspace, max_count=max(1, min(int(args.get("max_commits") or 8), 30)))
        handoff = self.read_handoff_status({"repo": str(workspace.root), "create_if_missing": False})
        return {
            "workspace_id": workspace.id,
            "root": str(workspace.root),
            "git": self.git_summary(workspace),
            "git_status": status["text"],
            "recent_commits": recent_commits,
            "tree": tree,
            "ai_bridge": {
                "files": handoff["files"],
                "text": handoff["text"],
            },
            "text": "\n\n".join(
                [
                    "# Workspace Snapshot",
                    f"Workspace: {workspace.id}",
                    "## Git Status",
                    status["text"],
                    "## Recent Commits",
                    recent_commits or "No commits found.",
                    "## Tree",
                    tree["text"],
                    "## AI Bridge",
                    handoff["text"],
                ]
            ),
        }

    def inventory(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Return high-level local capability inventory for ChatGPT orientation."""
        workspace = self.open_workspace(args.get("repo"))
        skills = self.list_skills(
            {
                "repo": str(workspace.root),
                "include_global_skills": args.get("include_global_skills", True),
                "max_skills": args.get("max_skills", self.max_skill_count),
            }
        )
        return {
            "workspace_id": workspace.id,
            "tool_modes": ["worker", "standard", "full", "minimal"],
            "context_dir": self.context_dir,
            "blocked_globs_count": len(self.blocked_globs),
            "git": self.git_summary(workspace),
            "skills": skills["skill_inventory"],
            "skill_counts": skills["skill_counts"],
            "power_tools": {
                "direct_write_default": bool(self.config.get("power_tools", {}).get("direct_write", False)),
                "bash_mode": self.config.get("power_tools", {}).get("bash_mode", "off"),
                "codex_session_read": bool(self.config.get("power_tools", {}).get("codex_session_read", False)),
            },
        }

    def git_status_text(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        porcelain = bool(args.get("porcelain", False))
        file_path = args.get("file_path")
        rel = ""
        if file_path:
            _target, rel = self.resolve_path(workspace, str(file_path))
        command = ["status", "--short", "--branch"] if porcelain else ["status", "--short", "--branch"]
        if rel:
            command.extend(["--", rel])
        text = self._run_git(workspace, command)
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "text": text.strip() or "No git changes.",
            "status_short": text.strip().splitlines()[:200],
            "git": self.git_summary(workspace),
        }

    def git_diff_tool(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        file_path = args.get("file_path")
        staged = bool(args.get("staged", False))
        max_bytes = min(int(args.get("max_bytes") or 200_000), 500_000)
        if file_path:
            _target, rel = self.resolve_path(workspace, str(file_path))
            diff = self.git_diff(workspace, rel_path=rel, staged=staged, max_bytes=max_bytes)
        else:
            diff = self.git_diff(workspace, staged=staged, max_bytes=max_bytes)
            rel = ""
        stats = self.diff_stats(diff)
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "staged": staged,
            "text": diff or "No git diff.",
            "diff": diff,
            **stats,
        }

    def show_changes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        include_diff = bool(args.get("include_diff", True))
        staged = bool(args.get("staged", False))
        max_diff_bytes = min(int(args.get("max_diff_bytes") or 120_000), 500_000)
        file_path = args.get("file_path") or args.get("path")
        rel = ""
        if file_path:
            _target, rel = self.resolve_path(workspace, str(file_path))
        status_args = {"repo": str(workspace.root)}
        if rel:
            status_args["file_path"] = rel
        status = self.git_status_text(status_args)
        diff = self.git_diff(workspace, rel_path=rel or None, staged=staged, max_bytes=max_diff_bytes) if include_diff else ""
        stats = self.diff_stats(diff)
        text_parts = [
            "# Workspace Changes",
            "",
            "## Git Status",
            "",
            status["text"],
            "",
            "## Diff Stats",
            "",
            f"Changed: {stats['changed']}",
            f"Additions: {stats['additions']}",
            f"Deletions: {stats['deletions']}",
        ]
        if include_diff:
            text_parts.extend(["", "## Diff", "", "```diff", diff or "No git diff.", "```"])
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "git": status["git"],
            "status": status["text"],
            "diff": diff,
            "staged": staged,
            **stats,
            "text": "\n".join(text_parts),
        }

    def write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        file_path = args.get("file_path")
        if not file_path:
            raise ValueError("file_path is required")
        content = str(args.get("content") if args.get("content") is not None else "")
        self._assert_safe_write_text(content)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_write_bytes:
            raise ValueError(f"Write content is too large ({len(encoded)} bytes). Limit: {self.max_write_bytes} bytes.")

        target, rel = self.resolve_write_path(workspace, str(file_path))
        create_dirs = bool(args.get("create_dirs", True))
        overwrite = bool(args.get("overwrite", True))

        old_text = ""
        existed = target.exists()
        if existed:
            if not overwrite:
                raise ValueError(f"File already exists and overwrite is false: {rel}")
            self._assert_text_file(target, max(self.max_read_bytes, self.max_write_bytes))
            old_text = target.read_text(encoding="utf-8")
        else:
            if not target.parent.exists() and not create_dirs:
                raise ValueError(f"Parent directory does not exist: {self._display_path(target.parent, workspace.root)}")
            target.parent.mkdir(parents=True, exist_ok=True)

        diff = self.make_unified_diff(old_text, content, rel)
        target.write_text(content, encoding="utf-8")
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "existed": existed,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            **diff,
        }

    def edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        file_path = args.get("file_path")
        old_text = str(args.get("old_text") if args.get("old_text") is not None else "")
        new_text = str(args.get("new_text") if args.get("new_text") is not None else "")
        if not file_path:
            raise ValueError("file_path is required")
        if not old_text:
            raise ValueError("old_text must not be empty")
        self._assert_safe_write_text(new_text)

        target, rel = self.resolve_write_path(workspace, str(file_path))
        self._assert_text_file(target, max(self.max_read_bytes, self.max_write_bytes))
        before = target.read_text(encoding="utf-8")
        occurrences = before.count(old_text)
        if occurrences == 0:
            raise ValueError(f"old_text was not found in {rel}. Read the file and retry with an exact snippet.")

        replace_all = bool(args.get("replace_all", False))
        if replace_all:
            after = before.replace(old_text, new_text)
            replacements = occurrences
        else:
            if occurrences != 1:
                raise ValueError(f"old_text matched {occurrences} times. Provide a more specific old_text or set replace_all=true.")
            after = before.replace(old_text, new_text, 1)
            replacements = 1

        expected = args.get("expected_replacements")
        if expected is not None and int(expected) != replacements:
            raise ValueError(f"Expected {expected} replacements but would perform {replacements}.")

        encoded = after.encode("utf-8")
        if len(encoded) > self.max_write_bytes:
            raise ValueError(f"Edited file is too large ({len(encoded)} bytes). Limit: {self.max_write_bytes} bytes.")
        diff = self.make_unified_diff(before, after, rel)
        target.write_text(after, encoding="utf-8")
        return {
            "workspace_id": workspace.id,
            "path": rel,
            "replacements": replacements,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            **diff,
        }

    def make_unified_diff(self, old_text: str, new_text: str, rel_path: str, max_chars: int = 60_000) -> Dict[str, Any]:
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        additions = sum(1 for line in difflib.ndiff(old_text.splitlines(), new_text.splitlines()) if line.startswith("+ "))
        deletions = sum(1 for line in difflib.ndiff(old_text.splitlines(), new_text.splitlines()) if line.startswith("- "))
        diff = "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        if not diff:
            diff = f"No changes in {rel_path}."
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n...[diff truncated to {max_chars} chars]"
        return {
            "diff": redact_text(diff),
            "additions": additions,
            "deletions": deletions,
            "changed": old_text != new_text,
        }

    def ensure_ai_bridge(self, workspace: Workspace) -> list[str]:
        files = {
            "README.md": "# AI Bridge\n\nShared planning context for ChatGPT, Codex, and local implementation agents.\n\n- current-plan.md: plan produced by ChatGPT or another planning model.\n- agent-status.md: implementation notes, touched files, test results, blockers, and review notes.\n- implementation-diff.patch: final review diff when practical.\n- codex-status.md: legacy Codex-specific status file.\n- decisions.md: stable architectural decisions.\n- open-questions.md: unresolved questions.\n- execution-log.jsonl: append-only handoff and execution events.\n- session-log.jsonl: append-only legacy session events.\n",
            "current-plan.md": "# Current Plan\n\nNo plan written yet.\n",
            "agent-status.md": "# Agent Status\n\nNo implementation agent status written yet.\n",
            "implementation-diff.patch": "",
            "codex-status.md": "# Codex Status\n\nNo Codex status written yet.\n",
            "decisions.md": "# Decisions\n\n",
            "open-questions.md": "# Open Questions\n\n",
            "execution-log.jsonl": "",
            "session-log.jsonl": "",
        }
        created = []
        for name, content in files.items():
            path, rel = self._ai_bridge_path(workspace, name)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                created.append(rel)
        return created

    def find_agents_files(self, workspace: Workspace) -> list[str]:
        candidates = ["AGENTS.override.md", "AGENTS.md", "agents.md", ".agents.md"]
        found: list[str] = []
        for path in workspace.root.rglob("*"):
            if len(found) >= 20:
                break
            rel = self._display_path(path.resolve(strict=False), workspace.root)
            if self.is_blocked_relative_path(rel):
                continue
            if path.is_file() and path.name in candidates:
                found.append(rel)
        return sorted(found)

    def read_agents_chain(self, workspace: Workspace, target_path: str) -> Dict[str, Any]:
        candidates = self._candidate_agent_dirs(target_path)
        files = []
        chunks = []
        seen: set[tuple[int, int]] = set()
        for directory in candidates:
            for name in ["AGENTS.override.md", "AGENTS.md", "agents.md", ".agents.md"]:
                rel = f"{directory}/{name}" if directory else name
                try:
                    path, normalized = self.resolve_path(workspace, rel)
                    if not path.exists() or not path.is_file():
                        continue
                    stat = path.stat()
                    real_key = (stat.st_dev, stat.st_ino)
                    if real_key in seen:
                        continue
                    seen.add(real_key)
                    read = self.read_file({"repo": str(workspace.root), "file_path": normalized, "max_bytes": 60_000})
                    files.append(normalized)
                    chunks.append(f"--- {normalized} ---\n{read['text']}")
                except Exception:
                    continue
        return {
            "files": files,
            "text": "\n\n".join(chunks) if chunks else "No AGENTS.md-style instruction files found for this target path.",
        }

    def git_diff(
        self,
        workspace: Workspace,
        rel_path: str | None = None,
        *,
        staged: bool = False,
        max_bytes: int = 200_000,
    ) -> str:
        if not (workspace.root / ".git").exists():
            return "Not a git repository."
        try:
            command = ["git", "diff"]
            if staged:
                command.append("--staged")
            command.append("--")
            command.append(rel_path or ".")
            completed = subprocess.run(
                command,
                cwd=workspace.root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            raw = completed.stdout if completed.returncode == 0 else completed.stderr
            redacted = redact_text(raw)
            if len(redacted.encode("utf-8")) > max_bytes:
                return redacted.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace") + f"\n...[diff truncated to {max_bytes} bytes]"
            return redacted
        except Exception as error:
            return f"git diff unavailable: {type(error).__name__}"

    def git_log(self, workspace: Workspace, max_count: int = 8) -> str:
        if not (workspace.root / ".git").exists():
            return "Not a git repository."
        return self._run_git(workspace, ["log", f"--max-count={max_count}", "--oneline", "--decorate=short"])

    def git_summary(self, workspace: Workspace) -> Dict[str, Any]:
        if not (workspace.root / ".git").exists():
            return {"is_git_repo": False}
        branch = self._run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
        commit = self._run_git(workspace, ["rev-parse", "--short", "HEAD"])
        status = self._run_git(workspace, ["status", "--short"])
        return {
            "is_git_repo": True,
            "branch": branch.strip() if branch else None,
            "commit": commit.strip() if commit else None,
            "dirty": bool(status.strip()),
            "status_short": status.strip().splitlines()[:100],
        }

    def diff_stats(self, diff: str) -> Dict[str, Any]:
        additions = 0
        deletions = 0
        files: set[str] = set()
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                files.add(line[6:])
            elif line.startswith("--- a/"):
                files.add(line[6:])
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        return {
            "additions": additions,
            "deletions": deletions,
            "changed": bool(diff.strip()) and not diff.startswith("Not a git repository."),
            "files_changed": sorted(files),
        }

    def _discover_skill_records(self, workspace: Workspace, *, include_global: bool, max_skills: int) -> list[SkillRecord]:
        roots = [
            workspace.root / ".codex" / "skills",
            workspace.root / ".agents" / "skills",
            workspace.root / "skills",
        ]
        if include_global:
            home = Path.home()
            roots.extend(
                [
                    home / ".codex" / "skills",
                    home / ".agents" / "skills",
                    home / ".codex" / "plugins" / "cache",
                ]
            )

        skill_files: list[Path] = []
        seen_roots: set[Path] = set()
        for root in roots:
            root = root.expanduser()
            if root in seen_roots or not root.exists() or not root.is_dir() or root.is_symlink():
                continue
            seen_roots.add(root)
            max_depth = 9 if self._path_has_part(root, "plugins") and self._path_has_part(root, "cache") else 3
            self._find_skill_files(root, max_depth=max_depth, out=skill_files, max_items=max_skills)
            if len(skill_files) >= max_skills:
                break

        records: list[SkillRecord] = []
        seen_records: set[tuple[str, str, str]] = set()
        for file_path in skill_files[:max_skills]:
            if file_path.is_symlink() or file_path.name != "SKILL.md":
                continue
            if self._skill_file_blocked(workspace, file_path):
                continue
            try:
                preview = file_path.read_bytes()[:16_000].decode("utf-8", errors="replace")
            except OSError:
                preview = ""
            name = self._frontmatter_value(preview, "name") or file_path.parent.name
            description = self._frontmatter_value(preview, "description") or ""
            source = self._skill_source(workspace, file_path)
            display = self._display_skill_path(workspace, file_path)
            key = (source, name, display)
            if key in seen_records:
                continue
            seen_records.add(key)
            records.append(
                SkillRecord(
                    name=name,
                    description=description,
                    source=source,
                    display_path=display,
                    abs_path=file_path,
                )
            )

        return sorted(records, key=lambda record: (self._skill_source_rank(record.source), record.name.lower(), record.display_path))

    def _find_skill_files(self, root: Path, *, max_depth: int, out: list[Path], max_items: int) -> None:
        if len(out) >= max_items or max_depth < 0:
            return
        try:
            children = sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            return
        for child in children:
            if len(out) >= max_items:
                return
            if child.name in {".git", "node_modules", "__pycache__", ".pytest_cache"} or child.is_symlink():
                continue
            if child.is_file() and child.name == "SKILL.md":
                out.append(child)
            elif child.is_dir() and max_depth > 0:
                self._find_skill_files(child, max_depth=max_depth - 1, out=out, max_items=max_items)

    def _skill_file_blocked(self, workspace: Workspace, file_path: Path) -> bool:
        try:
            if file_path.resolve(strict=True).is_relative_to(workspace.root):
                rel = self._display_path(file_path.resolve(strict=True), workspace.root)
                return self.is_blocked_relative_path(rel)
        except OSError:
            return True
        return False

    def _frontmatter_value(self, text: str, key: str) -> str:
        prefix = f"{key.lower()}:"
        for line in text.splitlines()[:80]:
            stripped = line.strip()
            if stripped.lower().startswith(prefix):
                return stripped.split(":", 1)[1].strip().strip("\"'")
        return ""

    def _skill_source(self, workspace: Workspace, file_path: Path) -> str:
        resolved = file_path.resolve(strict=False)
        if resolved.is_relative_to(workspace.root):
            return "workspace"
        parts = set(resolved.parts)
        if ".codex" in parts and "plugins" in parts and "cache" in parts:
            return "plugin"
        try:
            if resolved.is_relative_to(Path.home()):
                return "user"
        except RuntimeError:
            pass
        return "other"

    def _skill_source_rank(self, source: str) -> int:
        return {"workspace": 0, "user": 1, "plugin": 2, "other": 3}.get(source, 3)

    def _display_skill_path(self, workspace: Workspace, file_path: Path) -> str:
        resolved = file_path.resolve(strict=False)
        if resolved.is_relative_to(workspace.root):
            return "$WORKSPACE/" + self._display_path(resolved, workspace.root)
        home = Path.home()
        try:
            if resolved.is_relative_to(home):
                return "~/" + resolved.relative_to(home).as_posix()
        except RuntimeError:
            pass
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
        return f"$OTHER/{digest}/{resolved.name}"

    def _public_skill(self, record: SkillRecord) -> Dict[str, Any]:
        return {
            "name": record.name,
            "description": record.description,
            "source": record.source,
            "path": record.display_path,
        }

    def _skill_counts(self, skills: list[Dict[str, Any]]) -> Dict[str, int]:
        counts = {"total": len(skills), "workspace": 0, "user": 0, "plugin": 0, "other": 0}
        for skill in skills:
            source = str(skill.get("source") or "other")
            counts[source] = counts.get(source, 0) + 1
        return counts

    def _bounded_int(self, value: Any, default: int, lower: int, upper: int) -> int:
        try:
            raw = int(value if value is not None else default)
        except (TypeError, ValueError):
            raw = default
        return max(lower, min(raw, upper))

    def _path_has_part(self, path: Path, part: str) -> bool:
        return part in set(path.parts)

    def _search_with_rg(
        self,
        workspace: Workspace,
        root: Path,
        query: str,
        regex: bool,
        include_hidden: bool,
        max_results: int,
        glob: Optional[str],
    ) -> Dict[str, Any]:
        args = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns", "500"]
        if not regex:
            args.append("--fixed-strings")
        if include_hidden:
            args.append("--hidden")
        if glob:
            args.extend(["-g", glob])
        for pattern in self.blocked_globs:
            args.extend(["-g", f"!{pattern}"])
        args.extend([query, str(root)])

        completed = subprocess.run(args, cwd=workspace.root, capture_output=True, text=True, timeout=10)
        if completed.returncode not in (0, 1):
            raise ValueError("Search failed")

        matches = []
        for line in completed.stdout.splitlines():
            parsed = self._parse_rg_line(workspace, line)
            if not parsed:
                continue
            matches.append(parsed)
            if len(matches) >= max_results:
                break

        return self._format_search_result(workspace, matches, "ripgrep", len(completed.stdout.splitlines()) > len(matches))

    def _search_with_python(
        self,
        workspace: Workspace,
        root: Path,
        query: str,
        regex: bool,
        include_hidden: bool,
        max_results: int,
        glob: Optional[str],
    ) -> Dict[str, Any]:
        import re

        matcher = re.compile(query) if regex else None
        matches = []
        for path in self._iter_files(workspace, root, include_hidden, glob):
            if len(matches) >= max_results:
                break
            try:
                self._assert_text_file(path, self.max_read_bytes)
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    hit = bool(matcher.search(line)) if matcher else query in line
                    if hit:
                        matches.append({
                            "path": self._display_path(path.resolve(strict=True), workspace.root),
                            "line": line_number,
                            "text": redact_text(line[:500]),
                        })
                        if len(matches) >= max_results:
                            break
            except Exception:
                continue
        return self._format_search_result(workspace, matches, "python", len(matches) >= max_results)

    def _iter_files(self, workspace: Workspace, root: Path, include_hidden: bool, glob: Optional[str]) -> Iterable[Path]:
        if root.is_file():
            yield root
            return
        for path in root.rglob("*"):
            rel = self._display_path(path.resolve(strict=False), workspace.root)
            if path.is_dir() or self.is_blocked_relative_path(rel):
                continue
            if not include_hidden and any(part.startswith(".") for part in rel.split("/")):
                continue
            if glob and not fnmatch.fnmatchcase(rel, glob):
                continue
            yield path

    def _parse_rg_line(self, workspace: Workspace, line: str) -> Optional[Dict[str, Any]]:
        file_part, sep, rest = line.partition(":")
        if not sep:
            return None
        line_part, sep, text = rest.partition(":")
        if not sep or not line_part.isdigit():
            return None
        path = Path(file_part).resolve(strict=False)
        rel = self._display_path(path, workspace.root)
        if self.is_blocked_relative_path(rel):
            return None
        return {"path": rel, "line": int(line_part), "text": redact_text(text[:500])}

    def _format_search_result(self, workspace: Workspace, matches: list[Dict[str, Any]], used: str, truncated: bool) -> Dict[str, Any]:
        text = "\n".join(f"{item['path']}:{item['line']}: {item['text']}" for item in matches) or "No matches."
        return {
            "workspace_id": workspace.id,
            "text": text,
            "matches": matches,
            "truncated": truncated,
            "used": used,
        }

    def _assert_text_file(self, path: Path, max_bytes: int | None = None) -> None:
        if not path.is_file():
            raise ValueError(f"Not a file: {path.name}")
        if max_bytes is not None:
            size = path.stat().st_size
            if size > max_bytes:
                raise ValueError(f"File is too large ({size} bytes). Limit: {max_bytes} bytes.")
        sample = path.read_bytes()[:4096]
        if b"\0" in sample:
            raise ValueError("Refusing to read binary file")
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Refusing to read non-UTF-8 text file") from exc

    def _text_file_digest_and_line_count(self, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        newline_count = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                newline_count += chunk.count(b"\n")
                digest.update(chunk)
        return digest.hexdigest(), max(1, newline_count + 1 if size or newline_count else 1)

    def _clip_text_bytes(self, text: str, max_bytes: int) -> str:
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        suffix = " ...[line truncated]"
        suffix_bytes = suffix.encode("utf-8")
        if max_bytes <= len(suffix_bytes):
            return encoded[:max_bytes].decode("utf-8", errors="ignore")
        body = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
        return body + suffix

    def _candidate_agent_dirs(self, target_path: str) -> list[str]:
        normalized = self._normalize_rel(target_path).replace("./", "")
        parts = [] if normalized in {"", "."} else [part for part in normalized.split("/") if part]
        if parts and "." in parts[-1]:
            parts = parts[:-1]
        dirs = [""]
        for index in range(len(parts)):
            dirs.append("/".join(parts[: index + 1]))
        return list(dict.fromkeys(dirs))

    def _ai_bridge_path(self, workspace: Workspace, name: str) -> tuple[Path, str]:
        safe_name = name.strip().lstrip("/").replace("\\", "/")
        if not safe_name or safe_name.startswith("../") or "/../" in f"/{safe_name}/":
            raise ValueError(f"Invalid AI bridge file name: {name}")
        if "/" in safe_name:
            raise ValueError(f"Nested AI bridge file names are not allowed: {name}")
        return self.resolve_write_path(workspace, f"{self.context_dir}/{safe_name}")

    def _write_ai_bridge_file(self, workspace: Workspace, name: str, content: str, append: bool) -> tuple[Path, str]:
        self._assert_safe_write_text(content)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_write_bytes:
            raise ValueError(f"Write content is too large ({len(encoded)} bytes). Limit: {self.max_write_bytes} bytes.")
        path, rel = self._ai_bridge_path(workspace, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if append and path.exists():
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n\n")
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path, rel

    def _assert_safe_write_text(self, content: str) -> None:
        if redact_text(content) != content:
            raise ValueError("Secret-looking content is blocked from handoff/context writes. Use placeholders such as [REDACTED_SECRET].")

    def _run_git(self, workspace: Workspace, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=workspace.root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return completed.stdout if completed.returncode == 0 else ""
        except Exception:
            return ""

    def _assert_not_blocked(self, rel_path: str) -> None:
        if self.is_blocked_relative_path(rel_path):
            raise ValueError(f"Path is blocked by safety rules: {rel_path}")

    def _require_under_root(self, path: Path, root: Path, message: str) -> None:
        if path != root and root not in path.parents:
            raise ValueError(message)

    def _display_path(self, path: Path, root: Path) -> str:
        rel = os.path.relpath(path, root)
        return self._normalize_rel(rel)

    def _normalize_rel(self, rel_path: str) -> str:
        normalized = rel_path.replace(os.sep, "/")
        return "." if normalized == "" else normalized

    def _safe_json(self, value: Any) -> str:
        import json

        return json.dumps(value, indent=2, sort_keys=True)
