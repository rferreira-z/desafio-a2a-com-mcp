"""O agente como host MCP: um cliente HTTP de verdade para o servidor de salas.

Tres cuidados de protocolo moram aqui:

- Todo request leva no `_meta` a versao de protocolo, a capability de elicitation
  em form mode e o `traceparent` da Task. Nada e inferido de request anterior.
- `tools/call` usa `allow_input_required=True`: o SDK devolve o `input_required`
  cru em vez de resolver a elicitation sozinho. Nao ha callback que responda pelo
  usuario; quem responde e o cliente A2A, numa mensagem futura.
- O `requestState` e tratado como opaco: e guardado e ecoado byte a byte.
"""

from __future__ import annotations

import secrets
from typing import Any

from contextlib import AsyncExitStack

import mcp_types as types
from mcp.client import Client
from mcp.client.session import ClientSession

from log import log

PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = types.Implementation(name="agente-central-de-salas", version="1.0.0")


class Trace:
    """W3C trace context de uma Task: o trace-id fica, o span-id muda a cada request."""

    def __init__(self, trace_id: str, flags: str = "01") -> None:
        self.trace_id = trace_id
        self.flags = flags

    @classmethod
    def do_header(cls, valor: str | None) -> Trace | None:
        partes = (valor or "").strip().split("-")
        if len(partes) == 4 and len(partes[1]) == 32 and len(partes[2]) == 16 and partes[1] != "0" * 32:
            try:
                int(partes[1], 16)
            except ValueError:
                return None
            return cls(partes[1].lower(), partes[3])
        return None

    @classmethod
    def novo(cls) -> Trace:
        return cls(secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


async def _sem_callback(*_: Any, **__: Any) -> types.ErrorData:
    # Nunca e chamado: com allow_input_required=True o SDK devolve o input_required
    # cru. Existe so para o SDK habilitar o caminho de elicitation do cliente.
    return types.ErrorData(code=types.INVALID_REQUEST, message="o agente nao responde elicitation sozinho")


class _SessaoSoForm(ClientSession):
    """Declara elicitation SO em form mode: {"elicitation": {"form": {}}}.

    O SDK (mcp 2.3.0, mcp/client/session.py, `_build_capabilities`) declara
    `form` e `url` sempre que ha elicitation_callback, e sobrescreve o que o
    chamador puser em `_meta`. Este agente nao sabe repassar elicitation em URL
    mode, entao declarar `url` seria anunciar uma capability que ele nao tem.
    Aqui so se estreita o anuncio; o resto do envelope continua sendo do SDK.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        caps = super()._build_capabilities(version)
        if caps.elicitation is not None:
            caps.elicitation = types.ElicitationCapability(form=types.FormElicitationCapability())
        return caps


class _ClienteSoForm(Client):
    async def _build_session(self, exit_stack: AsyncExitStack) -> ClientSession:
        sessao = await super()._build_session(exit_stack)
        sessao.__class__ = _SessaoSoForm  # mesma classe base, so muda o anuncio de capability
        return sessao


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._client = _ClienteSoForm(
            url,
            mode=PROTOCOLO,  # versao fixada: sem probe, sem handshake, sem sessao
            client_info=CLIENT_INFO,
            elicitation_callback=_sem_callback,
            cache=None,  # tools/list e resources/read sempre vao ao servidor
        )

    async def abrir(self) -> None:
        await self._client.__aenter__()

    async def fechar(self) -> None:
        await self._client.__aexit__(None, None, None)

    @staticmethod
    def _meta(trace: Trace) -> dict[str, Any]:
        # O SDK carimba protocolVersion, clientInfo e clientCapabilities em todo
        # request (e espelha MCP-Protocol-Version, Mcp-Method e Mcp-Name nos
        # headers). Repetimos os campos aqui para o envelope ser explicito mesmo
        # se o carimbo mudar; o trace context e por nossa conta.
        return {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO.model_dump(exclude_none=True),
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
            "traceparent": trace.traceparent(),
        }

    async def listar_tools(self, trace: Trace) -> list[types.Tool]:
        meta = self._meta(trace)
        log(f"mcp -> tools/list traceparent={meta['traceparent']}")
        params = types.PaginatedRequestParams.model_validate({"_meta": meta})
        resultado = await self._client.session.list_tools(params=params)
        return list(resultado.tools)

    async def ler_resource(self, uri: str, trace: Trace) -> str:
        meta = self._meta(trace)
        log(f"mcp -> resources/read uri={uri} traceparent={meta['traceparent']}")
        resultado = await self._client.session.read_resource(uri, meta=meta)
        return "".join(getattr(c, "text", "") for c in resultado.contents)

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: Trace,
        input_responses: dict[str, types.ElicitResult] | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        meta = self._meta(trace)
        tipo = "retry" if request_state is not None else "inicial"
        log(f"mcp -> tools/call name={nome} ({tipo}) traceparent={meta['traceparent']}")
        return await self._client.session.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=meta,
            allow_input_required=True,
        )
