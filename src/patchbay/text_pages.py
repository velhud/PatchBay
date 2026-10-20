"""Stream whole source lines without allocating an unbounded physical line."""
from pathlib import Path


def bounded_lines(path: Path, max_line_bytes: int):
    """Yield (number, text-or-None, byte-count) including oversized lines.

    None means the entire physical line was drained without retaining it. A
    caller must report that line as too large rather than skip its suffix.
    """
    with path.open('rb') as handle:
        number = 0
        while True:
            chunk = handle.readline(max_line_bytes + 3)
            if not chunk:
                return
            number += 1
            count = len(chunk)
            retained = chunk if count <= max_line_bytes + 2 else None
            tail = chunk[-2:]
            while not chunk.endswith(b'\n'):
                chunk = handle.readline(max_line_bytes + 3)
                if not chunk:
                    break
                count += len(chunk)
                tail = (tail + chunk)[-2:]
                retained = None
            if tail.endswith(b'\n'):
                count -= 1
                if tail == b'\r\n':
                    count -= 1
            if count > max_line_bytes:
                retained = None
            if retained is not None:
                retained = retained[:-1] if retained.endswith(b'\n') else retained
            text = retained.decode('utf-8') if retained is not None else None
            yield number, text, count
