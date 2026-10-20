"""Run with python -m patchbay.authoring without importing legacy services."""
from .interfaces.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
