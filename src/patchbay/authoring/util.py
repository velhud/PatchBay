"""Byte identities, strict JSON, path contracts, and durable file writes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from .errors import AuthorError, require


def utc_now() -> float:
    return time.time()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value) -> str:
    return digest(canonical(value))


def strict_json(data: bytes | str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON number: " + value)

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AuthorError("INVALID_JSON", "Input is not an unambiguous UTF-8 JSON value", status=422) from exc


def safe_id(value: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value),
            "INVALID_REQUEST", "Invalid identifier", status=422)
    return value


def safe_path(value: str, *, allow_dot: bool = False) -> str:
    if value == "." and allow_dot:
        return value
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 4096,
            "INVALID_PATH", "Expected a bounded repository-relative path")
    parts = value.split("/")
    require(not value.startswith("/") and "\\" not in value and ":" not in value
            and unicodedata.normalize("NFC", value) == value
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not any(p in {"", ".", ".."} or p.casefold() in {".git", ".hg", ".svn"} for p in parts),
            "INVALID_PATH", "Path must be normalized, relative, and outside version-control internals", path=value)
    require(not any(p.endswith((".", " ")) for p in parts), "INVALID_PATH", "Ambiguous trailing path characters", path=value)
    return value


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "INVALID_PATH", "Refusing a symlink destination")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
