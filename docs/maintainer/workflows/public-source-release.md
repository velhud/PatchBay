# Publishing a source update

1. Fetch the authorized remote and inspect current refs, concurrent changes and
   applicable repository instructions. Preserve local work before bringing the
   change onto the current public baseline. Do not overwrite another release.
2. Review the complete intended tree and staged diff, not just new code. Exclude
   runtime state, private config, credentials/capability URLs, raw logs, HARs,
   model transcripts, personal/global instructions, local host paths and private
   repository names. Use generic examples in docs/tests. Retain third-party
   notices and licensing; use an appropriate public project identity for new
   commit metadata rather than a private machine/account identity.
3. Inspect recent public commits, including messages, author/committer metadata,
   removed file versions and branches within the release's audit scope. Run a
   credential scanner as well as semantic review for personal material: a
   secret-pattern scan cannot recognize every private instruction or identity.
   Review binary metadata when binary artifacts are present.
4. Run the relevant matrix in [TESTING](../../../TESTING.md) on the actual
   combined source. Record exact exits, skips and capability boundaries. Keep
   test logs private; publish a bounded result rather than raw host diagnostics.
   For execution changes, qualify a disposable real native job when authorized.
5. Review the explicit staged allowlist and final commit tree. Check dependency
   or lockfile changes, diff whitespace, source identity and commit metadata.
   Push only to the authorized public ref with ordinary non-forced semantics;
   independently verify the remote ref and commit files afterward.

A forward cleanup removes personal material from the current tree but does
not erase earlier published commits, caches, clones, release assets or forks.
If history contains real credentials, handle the affected credentials through
the owner's authorized incident process; history rewriting alone is not
revocation. Removing historical personal data requires a separately coordinated
history rewrite, backup, collaborator cutover and any necessary hosting support.
Do not silently force-push or claim that a normal cleanup commit purges history.

Keep the private audit report outside the public repository. A public cleanup
note should describe the generic change without reproducing removed personal
values or advertising private runtime locations.
