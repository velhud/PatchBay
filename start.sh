#!/bin/bash
# Start the Codex MCP Server

cd "$(dirname "$0")"

echo "Starting Codex MCP Server..."
python3 server.py
