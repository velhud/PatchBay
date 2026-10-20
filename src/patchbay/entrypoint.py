"""Lazy CLI split: author mode must not initialize the legacy Codex worker graph."""
from __future__ import annotations
import sys


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "author":
        from patchbay.authoring.interfaces.cli import main as author_main
        return author_main(arguments[1:])
    from patchbay.cli import main as legacy_main
    return legacy_main(arguments)
