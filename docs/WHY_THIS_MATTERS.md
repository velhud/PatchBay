# Why This Project Matters

Codex is useful for open-source maintenance, but many maintainers do not work only through one interface. They use local tools, MCP-compatible clients, custom dashboards, scripts, and repository-specific automation.

`codex-mcp-wrapper` exposes Codex CLI workflows through a local Streamable HTTP MCP server so maintainers can connect Codex to their own tools while keeping repository access explicit and local.

The project focuses on maintainer workflows:

- read-only repository analysis;
- isolated worktree-based apply jobs;
- status, result, and diff inspection;
- review and resume workflows;
- safer local automation around owned repositories.

The long-term goal is not to replace Codex. The goal is to make Codex easier to use inside open maintainer workflows, especially for issue triage, pull request review, release preparation, documentation, test generation, and security hardening.
