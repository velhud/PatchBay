# Overlap And Ownership Map

| Shared surface | Owning implementation area | Dependent areas |
| --- | --- | --- |
| Public tool registry and schemas | new Hub capability registry | protocol, Edge handshake, tests, docs |
| Principal/conversation identity | RequestContext and Hub server | store, visibility, groups, workers, Pro Requests |
| Machine generation/workspaces | Hub fleet identity | operations, preflight, worker refs, reassign |
| Operation state and receipts | Hub broker plus Edge journal | every mutating tool |
| Worker truth | WorkerRuntime projection API | list/status/wait, lanes, close, reassign |
| Repository mutation | WorkerRuntime and repo locks | start, integrate, cleanup, batch |
| Group truth | Hub derived projection | manager workflow and final status |
| Migration/deployment | Hub store and CLI | current enrolled fleet and rollback |

Shared authority files, schema migrations, public descriptors, group lifecycle,
and deployment remain main-thread/integrator owned.
