#!/usr/bin/env python3
"""Doctor command for patchbay connector readiness."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patchbay.connector.status import connector_status, format_doctor_json, format_doctor_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check patchbay connector readiness.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml.")
    parser.add_argument("--public-base-url", help="Optional public tunnel base URL for Server URL preview.")
    parser.add_argument("--reveal-token", action="store_true", help="Include the configured token in the Server URL.")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    status = connector_status(config, public_base_url=args.public_base_url, reveal_token=args.reveal_token)
    print(format_doctor_json(status) if args.json else format_doctor_text(status))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
