#!/bin/bash
# Start PatchBay Server

cd "$(dirname "$0")"

echo "Starting PatchBay Server..."
python3 scripts/start.py "$@"
