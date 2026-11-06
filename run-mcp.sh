#!/usr/bin/env bash
# Sobe o servidor MCP (Streamable HTTP) em http://127.0.0.1:7301/mcp.
# Exige REQUEST_STATE_SECRET exportado (ver README). O log de requests sai no stderr.
set -euo pipefail
cd "$(dirname "$0")/servidor-mcp"
exec uv run --frozen python servidor.py
