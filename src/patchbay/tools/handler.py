"""Compatibility-preserving tool handler corrections.

The original handler remains in :mod:`handler_legacy`; this subclass keeps the
public routing surface while dispatching the complete stored Pro response and
allowing an explicit model/reasoning policy for newly created workers.
"""
from __future__ import annotations

from typing import Any, Dict

from .handler_legacy import *  # noqa: F401,F403 - preserve public module API
from . import handler_legacy as _legacy


class ToolHandler(_legacy.ToolHandler):
    async def _codex_pro_request_dispatch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        target = str(args.get("target") or "origin_worker")
        manifest, refusal = self.pro_request_store.mark_dispatch_requested(
            request_id=args["request_id"],
            target=target,
            request_context=self.current_request_context(),
            takeover=bool(args.get("takeover", False)),
        )
        if refusal:
            return {"accepted": False, "request_id": manifest["id"], **refusal}

        response_record = self.pro_request_store.dispatch_response_text(args["request_id"])
        response_text = str(response_record.get("response_markdown") or "")
        if not response_text:
            result = {
                "accepted": False,
                "note": "This Pro Request has no stored response to dispatch.",
            }
            public = self.pro_request_store.finish_dispatch(
                request_id=args["request_id"],
                accepted=False,
                target=target,
                dispatch_result=result,
                request_context=self.current_request_context(),
            )
            return {**result, "request": public}

        message = response_text
        if str(args.get("message_source") or "worker_message_markdown") == "worker_message_markdown":
            worker_message = str(
                ((manifest.get("response") or {}).get("worker_message_markdown")) or ""
            ).strip()
            if worker_message:
                message = worker_message

        staleness = self.pro_request_store.staleness_check(manifest)
        if staleness.get("warning"):
            message = (
                f"Repository state warning from PatchBay: {staleness['warning']}\n\n{message}"
            )

        if target == "new_worker":
            worker_result = await self.worker_runtime.start_worker(
                name=args.get("new_worker_name") or "Pro Solution Implementer",
                brief=message,
                repo_path=(manifest.get("workspace") or {}).get("repo_path_private")
                or self.default_repo,
                workspace_mode=args.get("workspace_mode", "isolated_write"),
                model=args.get("model", ""),
                reasoning_effort=args.get("reasoning_effort", ""),
                request_context=self.current_request_context(),
            )
        else:
            origin = manifest.get("origin") or {}
            worker_name = origin.get("worker_name")
            if not worker_name:
                worker_result = {
                    "accepted": False,
                    "note": "This Pro Request has no origin worker to dispatch to.",
                }
            else:
                worker_result = await self.worker_runtime.message_worker(
                    worker=worker_name,
                    message=message,
                    repo_path=(manifest.get("workspace") or {}).get("repo_path_private")
                    or self.default_repo,
                    request_context=self.current_request_context(),
                    takeover=bool(args.get("takeover", False)),
                    takeover_reason=args.get("takeover_reason", ""),
                )

        accepted = bool(worker_result.get("accepted"))
        public = self.pro_request_store.finish_dispatch(
            request_id=args["request_id"],
            accepted=accepted,
            target=target,
            dispatch_result=worker_result,
            request_context=self.current_request_context(),
        )
        return {
            "accepted": accepted,
            "dispatched": accepted,
            "request": public,
            "dispatch_result": worker_result,
            "repo_state_check": staleness,
            "dispatch_text_bytes": len(message.encode("utf-8")),
            "dispatch_text_complete": True,
            "note": (
                "Dispatch used the complete stored response (or the explicitly supplied worker message). "
                "It never applies worker results to the base checkout and never commits."
            ),
        }
