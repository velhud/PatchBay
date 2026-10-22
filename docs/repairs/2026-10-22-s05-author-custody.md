# S05 Author custody and authority

S05 reviews package/source transport, Git reads, check admission and cancellation,
and publication recovery. It extends the existing Author owners. It introduces
no worker, model call, new execution backend or alternative package validator.
The trial's candidate-context and nonpublishing review repairs remain in place.

## Findings and dispositions

| Request | Implemented behavior |
| --- | --- |
| CR01 / F01 | Publication journals `push_attempted` before the subprocess. Typed Git timeout/I/O and failures after that point remain `publication_unknown`. Same-key recovery reads the retained remote/ref before current-policy approval checks and never repeats a possible push. Confirmed past effects carry `reconciled_previous_attempt` and `historical`; recovery starts no downstream CI. A moved ref remains explicit uncertainty. Proven pre-effect refusals stay blocked with `external_effect_possible=false`. |
| CR02 / F02 | Manual check admission compares the captured policy digest inside the run-insertion transaction. Final candidate acceptance also rechecks it before atomic automatic-check admission. Policy updates still block queued old-policy runs; started results remain historical. A new-policy retry under an old key is an explicit conflict (or current-policy denial), never a new run disguised as the old one. Inspect the retained operation to recover its original evidence. |
| CR03 / F03 | Git object consumers declare complete or intentional prefix reads. Complete reads require exact advertised size, EOF and successful producer exit. Owned pipe reads have finite deadlines and bounded diagnostics; teardown reaps only the owned producer. Diffs label truncation/timeouts as partial and give narrower-path guidance; a nonzero producer cannot masquerade as an empty clean diff. |
| CR04 / F04 | Version 2 contexts contain a complete presence inventory but load payloads only for selected regular files. Omitted `sha256`/`bytes` are null with `content_metadata=not_materialized`; Git OID/mode/type still identify presence. Small draft sealing exports changed baseline paths only. New-file-only sealing uses explicit identity-only context. |
| CR05 / F05 | The existing `author_read_file` has one bounded raw-byte route for immutable context or exact unsealed draft version. Base64 pages carry source/chunk identities and byte continuation. Huge lines and binary-source hints point to that same read-only tool and source selector. No draft archive/export mutation is needed. |
| CR06 / F06 | CLI and authenticated HTTP share a verified regular file descriptor and bounded rehashed delivery. CLI uses a synced sibling temporary and atomic no-clobber hard-link publication, then syncs the destination directory. Failed copies leave no partial final name. HTTP verifies before successful headers and owns its descriptor through verification cancellation and response termination. Existing reference-graph scope authorization precedes bytes. |
| CR07 / F07 (optional, adopted) | Version 2 prepared copies have an external completion marker bound to context/inventory/helper. Interrupted, sparse or foreign preparation fails before packaging. Deliberate deletion from a complete prepared tree remains supported. `--manual-tree` explicitly acknowledges a complete manual tree, including missing-file deletions. The output-inside-edited guard runs before traversal even for a nonexistent output. |
| CR08 / F08 | Check cancellation rechecks principal-bound lease token/epoch/expiry and grant inside the cancellation-intent transaction. A takeover that wins first fences the old caller. A cancellation already committed remains a historical idempotent result after takeover; it does not resurrect work or create another signal. Known rejected cancellation requests are terminal for their key. |
| CR09 / F09 (optional stronger revalidation declined) | Dirty capture reports `capture_consistency=per_file_stable_reads_and_unchanged_head` and `globally_atomic=false`. Selected files, selected absences and parent directories must remain quiescent during capture for coupled edits. This is an explicit weaker guarantee, not a filesystem snapshot. A retained Git ref still recovers original bytes after a lost DB response. |

The preparation marker directly prevents accidental multi-file deletions after
an interrupted copy without banning intentional deletion. Stronger dirty capture
would require a different filesystem consistency contract; a final stat pass
would still not prove a globally atomic snapshot. S05 therefore documents the
actual guarantee rather than adding a second snapshot service.

Older runtimes may have already terminalized a lost-push acknowledgement as
`blocked` without a durable push-attempt marker. S05 does not automatically reopen
or reinterpret those terminal records. Preserve them and obtain separately
authorized exact remote readback; a missing old marker is not proof that no push
occurred, and a fresh key must not be used to force a replacement effect.

## Exact source retrieval

Use the same selector on every page:

```json
{"draft_id":"DRAFT_ID","draft_version":1,"path":"src/file.py","byte_offset":0,"max_bytes":65536}
```

Or use `context_id` alone. `byte_offset` and `next_byte_offset` count raw bytes;
`max_bytes` is decoded bytes, at most 65,536. Omit line cursors in byte mode.
Decode `data_base64` and concatenate bytes before decoding UTF-8: multibyte
characters may cross a page boundary. Each page binds commit, tree, path, blob
OID, mode, total byte count, full SHA-256, chunk SHA-256 and returned range.
`producer_complete` means the full producer was checked; it does not mean the
page covers the full file. Check `has_more`, `next_byte_offset` and
`window_covers_full_file`. An empty page at exact EOF is valid; beyond EOF fails.

Memory is bounded by chunks and the selected response. Each byte page currently
rehashes the entire bounded Git object; this is an I/O cost, not an indexed or
constant-time range server. Existing line reads remain lossless; a line too
large for its budget returns `RESOURCE_LIMIT` and an actionable `next_read`.
Read-only callers do not acquire leases or create exports.

## Context and helper compatibility

Context/inventory format 2 is distinct from change-manifest format 1, which is
unchanged. Context identity includes its inventory format and selection mode.
Existing format-1 bundles and exact export-key receipts remain immutable and
usable. The new helper supports versions 1 and 2; unsupported critical versions
fail. A retained default export key keeps its pre-upgrade semantic fingerprint.
New keys generate version-2 contexts. `paths=[]` retains its full-export meaning;
use `identity_only=true` (CLI `--identity-only`) for no source payloads. Do not
combine it with a nonempty path selection.

The complete inventory has independent defaults of 100,000 paths and 16 MiB
metadata, while source bytes/member counts keep their existing limits. Exceeding
a budget fails explicitly, without silently dropping paths. Git raw objects,
not `git archive`, provide source: export-ignore/export-subst cannot alter it.
Helper version and SHA-256 describe the producer separately from the application
commit. Omitted presence prevents a create from impersonating an existing file;
replace/delete still require supported preimages and included source.

## Transfer and trusted-local limits

The private blob store is immutable trusted-local storage. Verification and
streaming use the same descriptor, so replacing its pathname cannot substitute
another file. Rehashing withholds the final chunk until identity is verified.
An HTTP interruption can leave partial bytes with a failed response, never a
new canonical complete-artifact receipt. A hostile administrator rewriting an
open inode is outside the immutable-storage guarantee.

CLI publication anchors the final parent directory descriptor and refuses any
existing final leaf, including symlinks. Existing higher-level destination and
private-runtime guards remain. Parent ancestry must be trusted and stable during
selection/open; this is not a hostile-filesystem namespace isolation service.
File and directory fsync establish the declared POSIX durability steps, not an
observed power-loss trial on every filesystem. Unsupported link/fsync behavior
fails instead of falling back to an overwrite. Retry an interrupted transfer to
a new absent final filename; preserve the retained content-addressed source.

## Verification and operator handoff

Run the required full matrix in `TESTING.md`, plus `tests/authoring/test_s05_*`.
The new tests exercise actual Git, SQLite, MCP, authenticated HTTP and helper
owners with controlled failures. Bare Git publication is disposable/local;
synthetic check receipts test persistence and cannot qualify execution.
Admission-only fixtures disable dispatch; their platform substitution does not
exercise or qualify a Linux supervisor. The Linux/WSL2 production guard remains.

The original 72 acceptance items are proposed oracles, not 72 observed failures.
The operator receipt maps them to actual checks or explicit remaining limits.
Local chunk-bound measurements establish buffering, not production throughput.
Native URL expiry, ordinary ChatGPT rendering, strict Docker, long-running
concurrency, native process cleanup and power-loss qualification remain separate
runs. Account usage visibility remains unknown; no quota saving is inferred.

The separate 52-case reliability-register evaluator deliberately returns
`INCOMPLETE` (exit 1) when no operational/client evidence is supplied. Preserve
those `NOT_RUN` dispositions separately from local test/evaluator passes; the
72 S05 proposed oracles are a different register. Neither is completed by
inventing observations or reusing a different runtime's qualification.

No running service, state home, account, connector, browser or route is updated
by source publication. The service owner should:

1. Preserve its Author home and evidence. Install the published package through
   the existing service procedure and restart only the intended service.
2. Refresh tool metadata. Runtime/schema changes invalidate prior write
   qualification; keep the read-only trial read-only. Qualify any intended
   writable client separately on a disposable task.
3. Recover old contexts/operations and check source/candidate separation. Exercise
   selected export, exact draft byte paging, policy-aware review and verified
   download in the intended client. Preserve canonical receipt identities.
4. For an uncertain publication use the original key and retained destination.
   Exact readback records a past effect; it never reapproves a new push. If the
   ref differs or remains unconfirmed, retain the fence and investigate.
5. Run required Linux/Docker/native/client qualification before claiming those
   execution properties for this new runtime. No deployment or trial reset is
   implied by the local checks.

Git producer semantics follow the installed interface and official
[cat-file](https://git-scm.com/docs/git-cat-file) documentation; raw export avoids
[archive attribute transformation](https://git-scm.com/docs/git-archive).
Atomic final-name publication uses the documented
[Python hard-link interface](https://docs.python.org/3.12/library/os.html#os.link).
