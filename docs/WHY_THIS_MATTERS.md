# Why This Project Matters

Codex and ChatGPT are both useful for software work, but many users move awkwardly between them. They may start in ChatGPT web/Pro, switch to local Codex for execution, then manually copy context, diffs, or session notes back and forth.

`codex-mcp-wrapper` exists to remove that manual bridge. It exposes local workspace context and Codex CLI workflows through a local Streamable HTTP MCP server so ChatGPT or another MCP-compatible client can inspect allowed repos, delegate work to local Codex, and review results without importing the whole repository into a chat by hand.

The project focuses on two combined workflows:

- ChatGPT as a direct workspace coder through bounded read/search/git/context tools;
- ChatGPT as a controller for local Codex plan/apply/resume jobs.

The long-term goal is not to replace Codex or ChatGPT. The goal is to make them work together as one local-control development platform for owned or authorized repositories, including issue triage, pull request review, release preparation, documentation, test generation, security hardening, and larger implementation jobs.
