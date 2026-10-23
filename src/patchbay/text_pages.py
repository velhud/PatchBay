"""Stream whole source lines without allocating an unbounded physical line."""
from pathlib import Path


def logical_lines(path: Path, max_line_bytes: int, *, total_lines: int):
    """Keep ``split('\\n')`` coordinates, including the final empty line.

    The caller supplies its independently counted logical line total. Physical
    lines stay streamed and oversized lines retain their rejection sentinel.
    """
    last_line = 0
    for number, text, byte_count in bounded_lines(path, max_line_bytes):
        last_line = number
        yield number, text, byte_count
    if last_line + 1 == total_lines:
        yield total_lines, "", 0


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
