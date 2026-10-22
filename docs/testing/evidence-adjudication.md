# Evidence record validation and claim adjudication

`author_requirements_eval.py` validates the original T01–T52 identities, run
record fields, current Git HEAD pin and artifact hashes. Its output now states
`validation_scope=record_structure_commit_pin_artifact_hashes`,
`candidate_binding=git_head_only`, and each row's
`evaluated_status_scope=evidence_record_validation`. It does not observe the
working tree, installed wheel, loaded service, client/model/account/route or
meaning of an arbitrary artifact. `independent_semantic_acceptance` is false.
A supplied row can pass these structural checks while its product claim fails.

Before treating a validated record as capability evidence, a named reviewer must
record a separate decision with the claim, exact subject/build/profile, raw
witnesses, negative controls, missing scope, and reason for ACCEPT/REWORK/NOT_RUN.
Check that the artifact actually exercises the claim. A hash-correct unrelated
text file is not a client observation, a wrapper is not native continuation,
controller attestation is not a canonical execution receipt, and a pass on one
lane does not prove the combined tree/generation. No universal semantic judge
or automatic certificate service is added here.

The S10 controlled negative supplies a matching HEAD and hash-correct irrelevant
artifact, with caller-declared `ordinary_chat` and profile labels. The structural
validator accepts that row; separate integration review rejects its ordinary-Pro
claim because no actual request, ingress, rendered content or client witness
exists. This is a local governance counterexample, not execution of T01.
The original register remains byte-identical and unrun cases are not promoted.

Keep source, effect admission, executor observations, candidate assertions,
controller review, delivery/readback and actual client/native qualification in
separate evidence fields. Preserve failed attempts, excluded cases and expected
baseline failures. A corrected assertion failing on a restored bug is a useful
negative control; it is not another passing regression. Do not add focused and
full-suite counts when they overlap.

A working-tree test run is bound by its frozen byte manifest until those exact
bytes are committed and checked against fetched remote objects. Git HEAD alone
does not provide that binding. Installed/loaded identity and deployment/profile
observations have their own records. Research manifests, transfer hashes, AST
checks and static inventories prove their stated custody scope only.

The negative is exercised both for one supplied row and for all 52 supplied
rows. In the latter case, `overall=PASS` and 52 structural row passes still do
not establish that any operational scenario ran. The original source statuses
stay `NOT_RUN_IN_THIS_RESEARCH`; the separate operational claim is rejected.
The normal no-evidence release check remains INCOMPLETE with 52 NOT_RUN.
