# Pending VM Deployment

Status: operator reminder.

Before experimenting with the new multi-machine hub/edge runtime on the
Scaleway VM, deploy the current stable PatchBay main branch commit:

```text
4da8706 Plan multi-machine hub architecture
```

That commit is the deployable baseline that existed before hub/edge
implementation started. It should be installed on the VM first so the current
single-machine PatchBay improvements are preserved in the running service before
multi-machine experiments begin.

Do not treat this note as a deployment record. It is a reminder for the next VM
deployment step.
