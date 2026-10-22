#!/usr/bin/env python3
"""Explicit passive manifest check. Does not execute Codex, models or canaries."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from patchbay.native_profile import check_installation, read_object


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = check_installation(args.root, read_object(args.manifest), executable=args.executable)
    # New private receipt only; never overwrite prior qualification evidence.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2); handle.write('\n')
    print(json.dumps({key:value for key,value in result.items() if key!='private_binding'}))
    return 0 if result['status']=='matched_supplied_manifest' else 1


if __name__ == '__main__':
    raise SystemExit(main())
