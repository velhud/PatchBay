# Ordinary Chat artifact trial preparation

For the later disposable live coding trials and the defects they exposed, see
[live ordinary-Chat reliability repairs](../repairs/2026-10-21-live-trial-repairs.md).
Those observations do not fill in this preparer's blank qualification templates
or qualify a different runtime, client, account or route.

`scripts/prepare_author_chat_trial.py` creates a disposable source repository,
independent Author home, task, immutable context ZIP, task-scoped author and
reader principals, and blank ordinary-Chat observation files. It is setup only:
it does not invoke ChatGPT, run the configured check, enable live MCP writes, or
qualify an account, client, or route.

Prepare a new results directory from the qualified checkout:

```bash
.venv/bin/python scripts/prepare_author_chat_trial.py \
  --output-dir ./results/client-trial
```

Without `--trial-kit`, the script creates blank templates from the checked-in
reliability register, golden prompt corpus, and evaluator schemas. To copy a
provided kit, pass its directory explicitly with `--trial-kit PATH`.

The script refuses to overwrite an existing output path. It prints fixture IDs
and file locations, never bearer values. `private/principals.json` contains
disposable local tokens and is mode 0600; keep it private. The configured
`unit` check is `python -m unittest discover -s tests` using the current Python
interpreter and has a 30-second limit. Its source scope is limited to `app.py`.
The script records that the check was not run.

`prepared-identities.json` records the repository's current `HEAD`, commit tree,
worktree dirty state and content digest, plus the actual local Author runtime,
catalog, and task check profile identities. These are preparation-time source
identities. They do not establish the deployed source or the active Chat route;
those profile observations stay null until directly observed.

Follow the generated `operator-instructions.md` for the manual artifact flow.
The G01 scenario is read-only and must remain read-only. Any
later mutation requires an explicit operator authorization. Record exact
ordinary-Chat observations, actual tool calls and server receipts before
changing `profile.json`, `qualification-evidence.json`, or `ab-capture.jsonl`.
Keep unobserved values null and unattempted cases `NOT_RUN`. Local preparation,
unit tests, API tests, and Inspector do not establish ordinary Chat execution.
