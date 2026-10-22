"""Lossless source coordinates with bounded per-line memory at pinned Git objects."""
from __future__ import annotations

import base64
import codecs
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


def read_window(git, commit: str, path: str, *, start_line: int, max_lines: int, max_bytes: int,
                byte_offset: int | None = None) -> dict:
    require(start_line >= 1 and 1 <= max_lines <= 1000 and 1 <= max_bytes <= 262144,
            'INVALID_REQUEST', 'Invalid source window')
    if byte_offset is not None:
        require(type(byte_offset) is int and byte_offset >= 0 and max_bytes <= 65536
                and start_line == 1 and max_lines == 200, 'INVALID_REQUEST',
                'Byte windows require a nonnegative byte_offset, max_bytes <= 65536, and no line cursor')
    entry = git.entries(commit).get(safe_path(path))
    require(entry is not None, 'NOT_FOUND', 'File is absent from the pinned revision', path=path)
    require(entry['mode'] in {'100644', '100755'}, 'UNSUPPORTED_FILE', 'Unsupported pinned file mode', path=path)
    retrieval = {'tool': 'author_read_file', 'arguments': {'path': path, 'byte_offset': 0, 'max_bytes': 65536}}
    h, byte_count, selected = hashlib.sha256(), 0, bytearray()
    decoder, binary = codecs.getincrementaldecoder('utf-8')(), False
    with git.open_blob(entry['oid']) as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            h.update(chunk)
            if byte_offset is not None:
                begin = max(0, byte_offset-byte_count)
                end = min(len(chunk), byte_offset+max_bytes-byte_count)
                if end > begin:
                    selected.extend(chunk[begin:end])
                if not binary:
                    try:
                        decoder.decode(chunk, final=False)
                        binary = b'\0' in chunk
                    except UnicodeDecodeError:
                        binary = True
            byte_count += len(chunk)
    binding = {'commit': commit, 'tree': git.tree(commit), 'path': path, 'blob_oid': entry['oid'],
               'mode': entry['mode'], 'sha256': h.hexdigest(), 'bytes': byte_count, 'producer_complete': True}
    if byte_offset is not None:
        if not binary:
            try: decoder.decode(b'', final=True)
            except UnicodeDecodeError: binary = True
        require(byte_offset <= byte_count, 'INVALID_REQUEST', 'Byte cursor is outside this exact source', bytes=byte_count)
        following = byte_offset+len(selected)
        return {**binding, 'content_encoding': 'base64', 'data_base64': base64.b64encode(selected).decode('ascii'),
                'binary': binary, 'byte_offset': byte_offset, 'returned_bytes': len(selected),
                'chunk_sha256': hashlib.sha256(selected).hexdigest(),
                'next_byte_offset': following if following < byte_count else None,
                'has_more': following < byte_count, 'window_covers_full_file': byte_offset == 0 and following == byte_count}
    chosen, used, total_lines, next_line = [], 0, 0, None
    try:
        with git.open_blob(entry['oid']) as stream:
            for number, text, size, _ in lines(stream, preview_bytes=max_bytes + 1):
                total_lines = number
                if number < start_line:
                    continue
                if len(chosen) >= max_lines or used + size > max_bytes:
                    require(chosen or number != start_line, 'RESOURCE_LIMIT',
                            'One source line exceeds the response budget; read the same source with byte_offset=0',
                            path=path, line=number, next_read=retrieval)
                    if next_line is None:
                        next_line = number
                    continue
                if next_line is None:
                    chosen.append(text)
                    used += size
    except UnicodeDecodeError:
        return {**binding, 'binary': True, 'next_read': retrieval,
                'next_action': 'Read this exact source with byte_offset=0 and max_bytes=65536 for base64 bytes'}
    return {**binding, 'start_line': start_line, 'text': ''.join(chosen),
            'total_lines': total_lines, 'next_start_line': next_line}


def bind_source_retrieval(value: dict, selector: dict) -> dict:
    """Make a read hint actionable without inventing another source version."""
    if isinstance(value.get('next_read'), dict):
        value['next_read']['arguments'].update(selector)
    return value
