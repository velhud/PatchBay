# Author Mode operations and qualification

Author Mode stores accepted implementation state independently from the
manager/worker runtime. Follow the deployment guide before serving a real
client. The implementation and recovery model is defined by the source and
tests; this documentation describes the operator sequence and the evidence
needed to rely on it.

- [Prepared ordinary-Chat trial](ordinary-chat-trial.md): reproducible disposable
  fixture and private evidence templates; actual client observations stay unrun.
- [Linux evidence](../testing/linux-qualification-2026-10-20.md): published
  candidate results, repaired failures and remaining deployment/lab boundaries.
- [Operations](operations.md): profile, authentication, ingress, recovery,
  backups, upgrades, rollback and retention.
- [Qualification](qualification.md): runnable checks and the Linux lab fault
  matrix, with explicit unrun states.
- [Golden prompts](golden-prompts.json): frozen semantic scenarios for later
  ordinary-Chat A/B trials. These are not automated model tests.

The evaluator scripts are deliberately conservative:

```bash
python scripts/author_interface_eval.py
python scripts/author_requirements_eval.py --json
```

The interface evaluator measures the complete Hub V2 tool registry and
instructions, plus deterministic synthetic reader/operator/author catalog,
version, instruction, request, result and output-schema profiles. Synthetic
qualification fixtures are explicitly not live qualifications. Its optional
capture mode compares actual ordinary-Chat A/B records only when records carry
both variant identity and an observed ordinary-Chat profile; absent or
non-ordinary-Chat outcomes stay `NOT_RUN`. UTF-8 byte sizes do not establish
token counts or model quality. See [qualification](qualification.md) and the
[source-pinned coverage map](coverage-map.md).

The requirements evaluator reads the frozen source register at
`reliability-acceptance-tests.json` (52 cases, SHA-256
`f05b0ec4c533f5c119237ad05ad9d96d16506014b19a35abad9b6b574ab58600`). It
preserves each original ID, title, requirement flag and source status.
`evaluated_status` is a separate evidence disposition. Missing operational
runs remain `NOT_RUN`; source presence is not execution evidence.
