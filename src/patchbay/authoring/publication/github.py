"""Narrow GitHub PR/check transport. Ambiguous writes are reconciled by exact identity."""
from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from ..errors import AuthorError, require
from ..util import canonical, strict_json


class GitHubClient:
    def __init__(self, repository: str, *, token: str | None = None):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "INVALID_REQUEST", "Expected an explicit GitHub owner/repository")
        self.repository = repository
        self.token = token if token is not None else os.environ.get("PATCHBAY_AUTHOR_GITHUB_TOKEN")
        self.prefix = "https://api.github.com/repos/" + repository

    def request(self, method: str, suffix: str, data=None):
        require(suffix.startswith("/") and not suffix.startswith("//"), "INVALID_REQUEST", "Invalid GitHub resource")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "PatchBay-Author/0.1"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if method != "GET":
            require(self.token, "PUBLICATION_BLOCKED", "GitHub write credentials are not configured for this publisher")
            headers["Content-Type"] = "application/json"
        request = Request(self.prefix + suffix, data=canonical(data) if data is not None else None, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(16 * 1024**2 + 1)
                require(len(body) <= 16 * 1024**2, "RESOURCE_LIMIT", "GitHub response exceeded its bounded page size")
                return strict_json(body) if body else {}
        except HTTPError as exc:
            raise AuthorError("GITHUB_ERROR", "GitHub rejected the request", method=method, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AuthorError("PUBLICATION_UNKNOWN" if method != "GET" else "GITHUB_UNAVAILABLE",
                              "GitHub transport outcome could not be confirmed", method=method) from exc

    def find_pull_request(self, branch: str, base: str) -> dict | None:
        owner = self.repository.split("/", 1)[0]
        query = urlencode({"state": "all", "head": owner + ":" + branch, "base": base, "per_page": 100})
        rows = self.request("GET", "/pulls?" + query)
        require(isinstance(rows, list) and len(rows) < 100, "PUBLICATION_UNKNOWN", "PR lookup was not complete")
        exact = [row for row in rows if row.get("head", {}).get("ref") == branch and row.get("base", {}).get("ref") == base
                 and row.get("head", {}).get("repo", {}).get("full_name", "").casefold() == self.repository.casefold()]
        open_rows = [row for row in exact if row.get("state") == "open"]
        require(len(open_rows) <= 1, "PUBLICATION_UNKNOWN", "More than one PR matches the exact task branch")
        if open_rows:
            return open_rows[0]
        require(not exact, "PUBLICATION_BLOCKED", "The task's previous PR was closed or merged; create a new authorized task instead of duplicating it")
        return None

    @staticmethod
    def compact(pr: dict) -> dict:
        return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"],
                "draft": pr.get("draft"), "head_sha": pr.get("head", {}).get("sha")}

    def ensure_draft(self, branch: str, base: str, commit: str, *, title: str, body: str) -> dict:
        existing = self.find_pull_request(branch, base)
        if existing:
            require(existing.get("head", {}).get("sha") == commit, "PUBLICATION_UNKNOWN", "PR head is not the intended published candidate")
            self.request("PATCH", "/pulls/" + str(existing["number"]), {"title": title[:240], "body": body})
            return self.compact(existing)
        try:
            created = self.request("POST", "/pulls", {"title": title[:240], "body": body, "head": branch, "base": base, "draft": True})
        except AuthorError:
            # A lost create response must not create a second PR.
            observed = self.find_pull_request(branch, base)
            if observed and observed.get("head", {}).get("sha") == commit:
                return self.compact(observed)
            raise
        require(created.get("head", {}).get("sha") == commit, "PUBLICATION_UNKNOWN", "New PR head has not resolved to the intended candidate")
        return self.compact(created)

    def ci(self, commit: str, required: list[str]) -> dict:
        if not required:
            return {"state": "not_configured", "required": [], "results": [], "missing": []}
        statuses, checks = [], []
        for endpoint, target, key in (("statuses", statuses, None), ("check-runs", checks, "check_runs")):
            complete = False
            for page in range(1, 51):
                data = self.request("GET", "/commits/" + quote(commit, safe="") + "/" + endpoint + "?per_page=100&page=" + str(page))
                require(not key or isinstance(data, dict) and isinstance(data.get(key), list),
                        "GITHUB_ERROR", "Unexpected CI response")
                rows = data[key] if key else data
                require(isinstance(rows, list), "GITHUB_ERROR", "Unexpected CI response")
                target.extend(rows)
                if len(rows) < 100:
                    complete = True
                    break
            require(complete, "CI_UNKNOWN", "CI pagination exceeded the supported observation budget")
        # Status history is reduced only within one namespace/context/producer.
        # Every returned check-run remains an obligation (including visible
        # retries without timestamps). Legacy name policies require all sources.
        newest = {}
        for row in statuses:
            require(isinstance(row, dict) and isinstance(row.get("context"), str), "GITHUB_ERROR", "Malformed status row")
            creator = row.get("creator") or {}
            producer = creator.get("id") or creator.get("login") or "unknown"
            key = (row["context"], str(producer))
            current = newest.get(key)
            stamp = row.get("updated_at") or row.get("created_at") or ""
            value = {"name": row["context"], "state": row.get("state"), "source": "status", "producer": producer, "id": row.get("id")}
            if current is None or stamp > current[0]:
                newest[key] = (stamp, [value])
            elif stamp == current[0]:
                current[1].append(value)
        obligations = [value for _, values in newest.values() for value in values]
        for row in checks:
            require(isinstance(row, dict) and isinstance(row.get("name"), str), "GITHUB_ERROR", "Malformed check-run row")
            name = row.get("name")
            outcome = row.get("conclusion") if row.get("status") == "completed" else "pending"
            obligations.append({"name": name, "state": outcome, "source": "check_run",
                                "producer": (row.get("app") or {}).get("id", "unknown"), "id": row.get("id")})
        missing = [name for name in required if not any(row["name"] == name for row in obligations)]
        results = [row for row in obligations if row["name"] in required]
        failed = any(row["state"] in {"failure", "error", "cancelled", "timed_out", "action_required", "startup_failure", "stale"} for row in results)
        passed = not missing and all(row["state"] == "success" for row in results)
        return {"commit": commit, "state": "failed" if failed else "passed" if passed else "pending", "required": required, "results": results, "missing": missing}
