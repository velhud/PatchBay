"""Lossless source coordinates with bounded per-line memory at pinned Git objects."""
from __future__ import annotations

import hashlib
import io
import re

from .errors import require
from .util import safe_path


def lines(stream, *, preview_bytes: int, query: str | None = None):
    """Drain huge lines in chunks, retaining only a preview and query overlap.

    newline='' preserves CR/LF/CRLF bytes in returned text while recognizing
    each source line. No source line is silently skipped or renumbered.
    """
    text = io.TextIOWrapper(stream, encoding="utf-8", errors="strict", newline="")
    number, preview, size, overlap, found = 1, [], 0, "", False
    pending_cr = ""

    def append(part):
        nonlocal size, found, overlap
        encoded = part.encode("utf-8")
        remaining = max(0, preview_bytes - size)
        if remaining:
            preview.append(encoded[:remaining].decode("utf-8", "ignore"))
        size += len(encoded)
        if query is not None:
            found = found or query in overlap + part
            overlap = (overlap + part)[-max(1, len(query) - 1):]

    try:
        while True:
            chunk = text.read(16384)
            if not chunk:
                append(pending_cr)
                if size:
                    yield number, "".join(preview), size, found
                return
            chunk = pending_cr + chunk
            pending_cr = "\r" if chunk.endswith("\r") else ""
            if pending_cr:
                chunk = chunk[:-1]
            start = 0
            for ending in re.finditer(r"\r\n|\r|\n", chunk):
                append(chunk[start:ending.end()])
                yield number, "".join(preview), size, found
                number += 1
                preview, size, overlap, found = [], 0, "", False
                start = ending.end()
            append(chunk[start:])
    finally:
        # The Git context manager owns the raw stream.
        text.detach()


def read_window(git, commit: str, path: str, *, start_line: int, max_lines: int, max_bytes: int) -> dict:
    require(start_line >= 1 and 1 <= max_lines <= 1000 and 1 <= max_bytes <= 262144,
            "INVALID_REQUEST", "Invalid source window")
    entry = git.entries(commit).get(safe_path(path))
    require(entry is not None, "NOT_FOUND", "File is absent from the pinned revision", path=path)
    require(entry["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported pinned file mode", path=path)
    h, byte_count = hashlib.sha256(), 0
    with git.open_blob(entry["oid"]) as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
            byte_count += len(chunk)
    chosen, used, total_lines, next_line = [], 0, 0, None
    try:
        with git.open_blob(entry["oid"]) as stream:
            for number, text, size, _ in lines(stream, preview_bytes=max_bytes + 1):
                total_lines = number
                if number < start_line:
                    continue
                if len(chosen) >= max_lines or used + size > max_bytes:
                    require(chosen or number != start_line, "RESOURCE_LIMIT",
                            "One source line exceeds the response budget; use the pinned archive", path=path, line=number)
                    if next_line is None:
                        next_line = number
                    continue
                if next_line is None:
                    chosen.append(text)
                    used += size
    except UnicodeDecodeError:
        return {"commit": commit, "path": path, "sha256": h.hexdigest(), "bytes": byte_count,
                "binary": True, "next_action": "Use the pinned archive for exact binary bytes"}
    return {"commit": commit, "path": path, "sha256": h.hexdigest(), "start_line": start_line,
            "text": "".join(chosen), "total_lines": total_lines, "next_start_line": next_line}
