# Affected Surface And Ripple Map

| Surface | Expected change | Main risk | Required protection |
|---|---|---|---|
| Job executor | Observe exact session terminal events while process runs | False completion from unrelated or partial data | Exact session binding and strict event parsing |
| Result capture | Build result from final session message when stdout is incomplete | Exposing raw/private transcript | Bounded extraction and existing redaction |
| Process cleanup | Gracefully stop wrapper after semantic completion | Killing active work or child processes too early | Cleanup only after terminal event; process-tree tests |
| Job manager | Atomic terminal transition semantics | Cancel/complete state flip | First durable terminal decision wins |
| Restart reconciliation | Recover completed sessions left running in durable state | Scanning unrelated sessions | Exact persisted session ID only |
| Worker status/report | Expose completed state and source diagnostics | Public schema churn | Additive compact fields only if needed |
| Hub projection | Receive corrected Edge state | Duplicate Hub inference | No Hub-side completion detector |
| Repo locks/worktrees | Release after semantic terminalization | Release while Codex still mutates files | `task_complete` required before release; wrapper cleanup first for write workers |
| Logging | Record terminal source and cleanup outcome | Leaking local paths/session content | Metadata only; no raw transcript |
| Configuration | Optional wrapper-exit grace and observer cadence | Misread as task timeout | Names and docs explicitly say post-completion cleanup |

## Special Write-Worker Boundary

For an isolated write worker, PatchBay must not expose integration-ready state
while a lingering wrapper could still mutate the worktree. After
`task_complete`, PatchBay captures the report, requests wrapper termination,
confirms process cleanup, and only then releases mutation locks and publishes
the completed/integrable state. The semantic completion timestamp may precede
the public completed transition by the short cleanup grace.

## Unaffected Surfaces

- Availability routing and group pinning.
- Worker model selection and prompting.
- Public manager tool inventory.
- Repository alias discovery.
- OpenAI connector safety behavior.

