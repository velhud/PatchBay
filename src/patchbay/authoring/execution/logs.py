"""Bounded on-disk log prefixes plus small diagnostic tails and cursor reads."""
from __future__ import annotations

import os
from pathlib import Path

from ..errors import require
from ..util import digest


class LogSpool:
    def __init__(self, path: Path, limit: int, tail_bytes: int = 16384):
        require(limit >= 0 and tail_bytes >= 0, "INVALID_REQUEST", "Negative log budget")
        self.path, self.limit, self.tail_bytes = path, limit, tail_bytes
        self.stream = path.open("xb")
        self.observed, self.stored, self.tail = 0, 0, bytearray()
        self.closed_result = None

    def write(self, data: bytes) -> None:
        self.observed += len(data)
        remaining = max(0, self.limit - self.stored)
        if remaining:
            portion = data[:remaining]
            self.stream.write(portion)
            self.stream.flush()
            self.stored += len(portion)
        if self.tail_bytes:
            self.tail.extend(data[-self.tail_bytes:])
            if len(self.tail) > self.tail_bytes:
                del self.tail[:-self.tail_bytes]

    def close(self) -> dict:
        if self.closed_result is not None:
            return self.closed_result
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        # Bound the UTF-8 representation too: replacement characters can expand bytes.
        text = bytes(self.tail).decode("utf-8", "replace")
        encoded = text.encode("utf-8")[-self.tail_bytes:] if self.tail_bytes else b""
        tail = encoded.decode("utf-8", "ignore")
        self.closed_result = {"file": self.path.name, "observed_bytes": self.observed,
                              "stored_bytes": self.stored, "truncated": self.observed > self.stored,
                              "tail": tail}
        return self.closed_result


def read_page(path: Path, *, offset: int = 0, limit: int = 65536) -> dict:
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 262144,
            "INVALID_REQUEST", "Invalid log cursor or byte limit")
    require(path.is_file() and not path.is_symlink(), "NOT_FOUND", "Log content is unavailable", status=404)
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(limit)
        more = bool(stream.read(1))
    return {"offset": offset, "next_offset": offset + len(data), "has_more": more,
            "bytes": len(data), "text": data.decode("utf-8", "replace")}
