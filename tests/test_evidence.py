import json

from patchbay.evidence import EvidenceRecorder


def test_mcp_transcripts_are_private_opt_in_runtime_evidence(tmp_path):
    config = {
        "logging": {
            "private_evidence_dir": str(tmp_path / "private-evidence"),
            "store_mcp_transcripts": True,
        }
    }
    recorder = EvidenceRecorder(config)

    recorder.record_mcp_event(
        client_ref="client/test",
        owner_ref="owner/test",
        direction="request",
        message={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "codex_worker_start",
                "arguments": {"name": "Worker", "brief": "full original brief"},
            },
        },
    )
    recorder.record_mcp_event(
        client_ref="client/test",
        owner_ref="owner/test",
        direction="response",
        response={"jsonrpc": "2.0", "id": 1, "result": {"worker_id": "wrk_1"}},
        status_code=200,
    )

    files = list((tmp_path / "private-evidence" / "mcp").glob("*/*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert rows[0]["direction"] == "request"
    assert rows[0]["message"]["params"]["arguments"]["brief"] == "full original brief"
    assert rows[1]["direction"] == "response"
    assert rows[1]["response"]["result"]["worker_id"] == "wrk_1"


def test_mcp_transcripts_are_disabled_by_default(tmp_path):
    recorder = EvidenceRecorder({"logging": {"private_evidence_dir": str(tmp_path / "private-evidence")}})

    recorder.record_mcp_event(
        client_ref="client/test",
        owner_ref=None,
        direction="request",
        message={"params": {"arguments": {"brief": "not stored"}}},
    )

    assert not (tmp_path / "private-evidence").exists()


def test_legacy_response_body_flag_does_not_store_request_body(tmp_path):
    recorder = EvidenceRecorder(
        {
            "logging": {
                "private_evidence_dir": str(tmp_path / "private-evidence"),
                "log_response_bodies": True,
            }
        }
    )

    recorder.record_mcp_event(
        client_ref="client/test",
        owner_ref=None,
        direction="request",
        message={"params": {"arguments": {"brief": "not stored by response flag"}}},
    )
    recorder.record_mcp_event(
        client_ref="client/test",
        owner_ref=None,
        direction="response",
        response={"result": {"ok": True}},
    )

    files = list((tmp_path / "private-evidence" / "mcp").glob("*/*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["direction"] == "response"
