#!/usr/bin/env bash
# Sobe o agente A2A em http://127.0.0.1:7300 (card no well-known, JSON-RPC em /a2a).
# Espera o servidor MCP em http://127.0.0.1:7301/mcp (MCP_URL para mudar).
set -euo pipefail
cd "$(dirname "$0")/agente"
exec uv run --frozen python agente.py
