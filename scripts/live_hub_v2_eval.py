#!/usr/bin/env python3
"""Run the opt-in local Hub V2 consequential evaluator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patchbay.hub.live_v2 import run_live_hub_v2_eval_sync  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the disposable local Hub V2 evaluator.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep disposable state for debugging.")
    args = parser.parse_args()

    report = run_live_hub_v2_eval_sync(keep_temp=args.keep_temp)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['name']}: {report['status']}")
        for check in report["checks"]:
            marker = "PASS" if check["passed"] else "FAIL"
            print(f"[{marker}] {check['name']}")
        if report.get("error"):
            print(f"Error: {report['error']['type']}: {report['error']['message']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
