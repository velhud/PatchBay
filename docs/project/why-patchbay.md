# Why PatchBay Exists

ChatGPT and local Codex are useful in different places. ChatGPT often has the richest context: a long conversation, project memory, uploaded or generated files, product decisions, and several related threads. Local Codex has the repository, git state, tools, credentials already configured for Codex, and the real execution environment.

Without PatchBay, the human becomes the transport layer. You copy a brief from ChatGPT into Codex, copy files or snippets back, paste diffs into the chat, ask for a revision, then repeat. That workflow loses context, wastes time, and makes multi-step agent work feel smaller than it should.

PatchBay exists to remove that manual bridge. It exposes a local Streamable HTTP MCP server so ChatGPT can open approved repositories, brief named Codex workers, import ChatGPT-generated files or zips as local worker context, inspect reports and diffs, pass context between workers, and apply accepted work without leaving the chat loop.

The product is powerful because it combines:

- ChatGPT as the high-context control surface for planning, memory, product reasoning, and conversation continuity;
- local Codex as the execution engine that works against the real repo and local environment;
- durable named workers that can continue across turns and PatchBay restarts;
- artifact transfer so ChatGPT-generated packages can become Codex source material without manual file handling;
- reviewable local integration through worker diffs, integration previews, and normal git workflows.

PatchBay makes powerful ChatGPT-to-Codex work explicit, inspectable, and usable. The security and power-boundary docs describe the controls around that power; they are operational boundaries, not the product promise.

The long-term goal is not to replace Codex or ChatGPT. The goal is to make them operate as one local-control development system for owned or authorized repositories: investigations, implementation loops, issue triage, pull request review, release preparation, documentation, test generation, and larger multi-worker jobs.
