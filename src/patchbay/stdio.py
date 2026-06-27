"""Stdio MCP transport for PatchBay.

This mirrors CodexPro's useful stdio entry point, but keeps PatchBay's MCP
protocol, tool registry, worker runtime, and session-local tool-mode handling
as the single source of truth.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from patchbay.connector.profiles import normalize_logging_paths
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.protocol.context import RequestContext
from patchbay.protocol.mcp import MCPProtocol
from patchbay.security import internal_log_error
from patchbay.tools.handler import ToolHandler


logger = logging.getLogger(__name__)


def default_config_path() -> Path:
    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parents[2] / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("Config file must contain a YAML object")
    normalize_logging_paths(payload)
    return payload


class StdioMCPServer:
    """Line-delimited JSON-RPC stdio transport."""

    def __init__(self, config: Mapping[str, Any], *, client_label: str = "stdio") -> None:
        self.config = dict(config)
        job_manager = JobManager(self.config)
        job_executor = JobExecutor(self.config, job_manager)
        tool_handler = ToolHandler(self.config, job_manager, job_executor)
        self.protocol = MCPProtocol(self.config, tool_handler)
        self.session_id = f"stdio-{uuid.uuid4()}"
        now = time.monotonic()
        self.session_data: dict[str, Any] = {
            "created_at": now,
            "last_activity": now,
            "client_label": client_label,
        }
        self._session_ref_salt = os.environ.get("PATCHBAY_SESSION_REF_SALT") or uuid.uuid4().hex

    def request_context(self) -> RequestContext:
        self.session_data["last_activity"] = time.monotonic()
        return RequestContext.from_session(
            self.session_id,
            self.session_data,
            salt=self._session_ref_salt,
            active_mcp_sessions=1,
        )

    async def handle_payload(self, payload: Any) -> Any:
        context = self.request_context()
        if isinstance(payload, list):
            responses = []
            for item in payload:
                if not isinstance(item, dict):
                    responses.append(_invalid_request(None, "Batch items must be JSON-RPC objects"))
                    continue
                response = await self.protocol.handle_message(item, context=context)
                if response is not None:
                    responses.append(response)
            return responses if responses else None
        if not isinstance(payload, dict):
            return _invalid_request(None, "JSON-RPC message must be an object")
        return await self.protocol.handle_message(payload, context=context)

    async def handle_line(self, line: str) -> Any:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        return await self.handle_payload(payload)


async def run_stdio(config_path: str | Path, *, client_label: str = "stdio", input_stream=None, output_stream=None) -> int:
    server = StdioMCPServer(load_config(config_path), client_label=client_label)
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            response = await server.handle_line(line)
        except Exception as error:
            logger.error("stdio request failed: %s", internal_log_error(error))
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal processing error"}}
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), file=output_stream, flush=True)
    return 0


def _invalid_request(msg_id: Any, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": message}}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run PatchBay MCP over stdio.")
    parser.add_argument("--config", default=str(default_config_path()), help="Path to config.yaml.")
    parser.add_argument("--client-label", default="stdio", help="Public label for this stdio MCP session.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run_stdio(args.config, client_label=args.client_label))


if __name__ == "__main__":
    raise SystemExit(main())
