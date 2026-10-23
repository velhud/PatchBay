# Context loading and module boundaries

Workspace context keeps complete selected source and the applicable instruction
chain. Repeated identical `selected_paths` are loaded once, in first occurrence
order. Distinct selectors remain distinct; this is not a content cache and cannot
hide changed source between requests.

Directory discovery and Python fallback search prune only subtrees proved
excluded by a workspace-relative `directory/**` policy. Ordinary per-file path
validation still runs. Name-only exclusions do not acquire new descendant
semantics, and directory symlinks are not followed.

Line reads retain `split('\n')` coordinates, including the final empty logical
line after a newline and line 1 of an empty file. Oversized lines keep the
existing explicit refusal and continuation behavior. Hashes and byte counts
identify the original source; they do not establish test success.

Small non-worker MCP text results use literal UTF-8 instead of expanding Unicode
to ASCII escapes. Decoded JSON and `structuredContent` are unchanged. Bulk
results retain the existing text summary and exact structured payload. Neither
structured data nor text is free model context; measure both. Byte reductions
do not establish token billing or a particular client's effective context size.

`protocol/tool_catalog.py` owns static descriptors and schemas;
`protocol/mcp_legacy.py` retains dispatch, validation and compatibility exports.
`jobs/event_helpers.py` owns stateless event parsing; `JobExecutor` retains its
method signatures, override hooks, clock boundary and execution authority.
Historical catalog instructions remain compatibility material; the assembled
manager instructions are the effective policy.

Waits return terminal/actionable workers immediately. Handle their reports,
failures and cleanup blockers first. Then use `active_only=true` for remaining
active work. Repeating the same completed selection cannot wait for a new
completion. Quiet work and timeouts still do not imply failure.

Focused verification:

```bash
PYTHONPATH=src python -m pytest tests/test_context_loading_efficiency.py \
  tests/test_workspace_context.py tests/test_job_executor_artifacts.py \
  tests/test_mcp_compact_text_serialization.py tests/test_tool_surface.py \
  tests/test_worker_tool_surface.py tests/test_s03_monitoring.py -q
```

Run the complete matrix in `TESTING.md` for a source candidate. Source tests and
descriptor equality do not refresh or qualify an existing ChatGPT connector.
