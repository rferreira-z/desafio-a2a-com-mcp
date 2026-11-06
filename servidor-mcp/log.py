"""Registro de cada request MCP no stderr: metodo, id, traceparent e status HTTP.

Fica na borda ASGI, antes do SDK, para registrar inclusive os requests que o SDK
recusa (por exemplo, _meta incompleto com HTTP 400). stderr no lugar do logging
de protocolo, que esta depreciado na revisao 2026-07-28.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any


def log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} {texto}", file=sys.stderr, flush=True)


def _resumo(corpo: bytes) -> str:
    try:
        msg = json.loads(corpo)
    except (ValueError, UnicodeDecodeError):
        return "corpo-invalido"
    if not isinstance(msg, dict):
        return "lote-ou-corpo-nao-objeto"
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    partes = [f"method={msg.get('method')}", f"id={json.dumps(msg.get('id'))}"]
    if "name" in params:
        partes.append(f"name={params['name']}")
    if "uri" in params:
        partes.append(f"uri={params['uri']}")
    if "requestState" in params:
        partes.append("retry=sim")
    if "io.modelcontextprotocol/clientCapabilities" in meta:
        caps = json.dumps(meta["io.modelcontextprotocol/clientCapabilities"], separators=(",", ":"))
    else:
        caps = "ausente"
    partes.append(f"caps={caps}")
    partes.append(f"traceparent={meta.get('traceparent', '-')}")
    return " ".join(partes)


class LogDeRequests:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        while True:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break

        entregue = False

        async def reenviar() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        status = {"http": 0}

        async def enviar(mensagem: dict) -> None:
            if mensagem["type"] == "http.response.start":
                status["http"] = mensagem["status"]
            await send(mensagem)

        resumo = _resumo(corpo)
        try:
            await self.app(scope, reenviar, enviar)
        finally:
            log(f"mcp {resumo} http={status['http']}")
