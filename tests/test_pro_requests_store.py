import json
import subprocess
from pathlib import Path

from patchbay.pro_requests import ProRequestStore
from patchbay.protocol.context import RequestContext


def init_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# Pro Requests\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Pro Test", "-c", "user.email=pro@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def config(tmp_path: Path, repo: Path) -> dict:
    return {
        "server": {"max_concurrent_jobs": 2, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {"require_git_repo": False, "blocked_globs": []},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "pro_requests": {
            "root": str(tmp_path / "runtime" / "pro-requests"),
            "mirror_enabled": True,
            "mirror_dir": ".ai-bridge/pro-requests",
            "max_report_bytes": 20_000,
            "max_response_bytes": 20_000,
            "max_attachment_bytes": 200,
            "max_attachments_per_request": 2,
        },
        "workers": {"worktree_root": str(tmp_path / "workers")},
    }


def test_pro_request_store_create_read_respond_close_and_mirror(tmp_path):
    repo = init_repo(tmp_path / "repo")
    report = tmp_path / "report.md"
    report.write_text("# Pro Escalation Request\n\nBlocked on design.\n", encoding="utf-8")
    attachment = tmp_path / "test-output.txt"
    attachment.write_text("failed assertion\n", encoding="utf-8")
    store = ProRequestStore(config(tmp_path, repo))

    created = store.create_request(
        repo_path=str(repo),
        title="Blocked design",
        origin_kind="terminal_codex",
        report_path=str(report),
        attachments=[str(attachment)],
        desired_output="Plan and tests",
    )

    request_id = created["id"]
    assert request_id.startswith("proreq_")
    assert created["repo_path_returned"] is False
    assert created["raw_session_ids_returned"] is False
    assert "repo_path_private" not in json.dumps(created)

    listed = store.list_requests(repo_path=str(repo))
    assert listed["requests"][0]["id"] == request_id

    read = store.read_request(request_id=request_id)
    assert "Blocked on design" in read["report_markdown"]
    assert read["attachment_index"][0]["filename"] == "test-output.txt"
    assert read["repo_state_check"]["checked"] is True

    responded = store.respond_request(
        request_id=request_id,
        response_kind="architecture_plan",
        response_markdown="# Response\n\nDo this safely.",
        worker_message_markdown="Implement safely.",
    )
    assert responded["response_stored"] is True
    assert responded["dispatched"] is False
    response = store.response_text(request_id)
    assert "Do this safely" in response["response_markdown"]

    mirror = repo / ".ai-bridge" / "pro-requests" / request_id
    assert (mirror / "report.md").exists()
    assert (mirror / "response.md").exists()
    status = json.loads((mirror / "status.json").read_text(encoding="utf-8"))
    assert status["repo_path_returned"] is False

    closed = store.close_request(request_id=request_id, reason="done")
    assert closed["accepted"] is True
    assert closed["request"]["status"] == "closed"


def test_pro_request_store_rejects_oversized_attachment_and_path_traversal_name(tmp_path):
    repo = init_repo(tmp_path / "repo")
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    large = tmp_path / "large.log"
    large.write_text("x" * 300, encoding="utf-8")
    store = ProRequestStore(config(tmp_path, repo))

    try:
        store.create_request(repo_path=str(repo), title="Large", report_path=str(report), attachments=[str(large)])
    except ValueError as error:
        assert "too large" in str(error)
    else:
        raise AssertionError("oversized attachment accepted")


def test_pro_request_claim_and_takeover(tmp_path):
    repo = init_repo(tmp_path / "repo")
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    store = ProRequestStore(config(tmp_path, repo))
    request_id = store.create_request(repo_path=str(repo), title="Claim", report_path=str(report))["id"]
    ctx_a = RequestContext.from_session("a", {"client_label": "A"}, salt="salt")
    ctx_b = RequestContext.from_session("b", {"client_label": "B"}, salt="salt")

    assert store.claim_request(request_id=request_id, request_context=ctx_a)["accepted"] is True
    refused = store.respond_request(
        request_id=request_id,
        response_kind="analysis",
        response_markdown="answer",
        request_context=ctx_b,
    )
    assert refused["accepted"] is False
    assert refused["takeover_required"] is True

    taken = store.respond_request(
        request_id=request_id,
        response_kind="analysis",
        response_markdown="answer",
        request_context=ctx_b,
        takeover=True,
    )
    assert taken["accepted"] is True
