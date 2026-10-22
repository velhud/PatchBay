"""PROPOSED DESIRED-BEHAVIOR REGRESSIONS; NOT RUN BY THIS AUDIT.
Scope: local CI aggregation only; request() never reaches the network.
Acceptance references: S07-T25, T26 and T30.
"""
from __future__ import annotations

from copy import deepcopy

from patchbay.authoring.publication.github import GitHubClient


class _FixtureClient(GitHubClient):
    def __init__(self, statuses: list[dict], checks: list[dict]) -> None:
        super().__init__("fixture/project", token="not-a-credential")
        self.statuses = statuses
        self.checks = checks
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, suffix: str, data=None):
        assert method == "GET", "This fixture authorizes no writes."
        self.calls.append((method, suffix))
        if "/check-runs?" in suffix:
            return {"check_runs": deepcopy(self.checks)}
        if "/statuses?" in suffix:
            return deepcopy(self.statuses)
        raise AssertionError(f"Unexpected request: {method} {suffix}")


def _green_status() -> dict:
    return {
        "context": "unit",
        "state": "success",
        "created_at": "2026-10-22T10:05:00Z",
        "updated_at": "2026-10-22T10:05:00Z",
    }


def test_same_name_queued_check_not_hidden_by_green_status() -> None:
    client = _FixtureClient(
        [_green_status()],
        [{"id": 10, "name": "unit", "status": "queued", "conclusion": None,
          "started_at": None, "completed_at": None, "app": {"id": 7}}],
    )
    result = client.ci("a" * 40, ["unit"])
    assert result["state"] != "passed"


def test_same_name_failed_check_not_hidden_by_later_status() -> None:
    client = _FixtureClient(
        [_green_status()],
        [{"id": 10, "name": "unit", "status": "completed", "conclusion": "failure",
          "started_at": "2026-10-22T10:00:00Z",
          "completed_at": "2026-10-22T10:04:00Z", "app": {"id": 7}}],
    )
    result = client.ci("a" * 40, ["unit"])
    assert result["state"] != "passed"


def test_unambiguous_status_success_remains_usable() -> None:
    client = _FixtureClient([_green_status()], [])
    result = client.ci("a" * 40, ["unit"])
    assert result["state"] == "passed"
    assert result["commit"] == "a" * 40
