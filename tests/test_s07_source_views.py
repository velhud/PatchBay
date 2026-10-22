"""Manager views preserve protected literals and label transformed evidence."""
import hashlib
from pathlib import Path

import pytest
from test_audit_regressions_20261019 import completed_worker, complete


@pytest.mark.asyncio
async def test_workspace_and_retained_source_transformation_identity(tmp_path):
    _, manager, runtime, job = await completed_worker(tmp_path)
    source = 'author = resolve_user()\npassword = "fixture-protected-literal"\nlease_token = lease_token + 1\n'
    root = Path(job.worktree_path); (root / "evidence.py").write_text(source)
    complete(manager, job)
    public = await runtime.inspect_worker(worker="Luna", view="report")
    assert public["report_evidence_basis"]["canonical_execution_receipt"] is False
    target = (await runtime.inspect_worker(worker="Luna", view="integration_preview"))["review_target"]
    for extra in ({}, {"candidate_id": target["candidate_id"]}):
        page = await runtime.inspect_worker(worker="Luna", view="file", file_path="evidence.py", **extra)
        assert "fixture-protected-literal" not in page["text"]
        assert "lease_token = lease_token + 1" in page["text"]
        assert page["representation_changed"] and page["source_fidelity"] == "transformed"
        assert page["returned_text_sha256"] == hashlib.sha256(page["text"].encode()).hexdigest()
        assert page.get("raw_sha256", page.get("sha256")) == hashlib.sha256(source.encode()).hexdigest()
        assert page["exact_source_bytes"] is False
