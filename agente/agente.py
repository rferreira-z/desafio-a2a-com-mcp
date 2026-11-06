"""Agente Central de Salas: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro.

- Agent Card em GET /.well-known/agent-card.json
- JSON-RPC 2.0 em POST /a2a (SendMessage, GetTask)
- Tasks em memoria (InMemoryTaskStore); estado pausado do MRTR no executor.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

import uvicorn
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import VERSION_HEADER
from starlette.applications import Starlette
from starlette.requests import Request

from cliente_mcp import ClienteMCP
from executor import AgenteDeSalas
from log import log

PORTA = int(os.environ.get("AGENT_PORT", "7300"))
HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
URL_PUBLICA = os.environ.get("AGENT_PUBLIC_URL", f"http://{HOST}:{PORTA}").rstrip("/")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
CAMINHO_A2A = "/a2a"

CARD = AgentCard(
    name="Central de Salas",
    description="Reserva salas de reuniao da Hill Valley Tech.",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    version="1.0.0",
    supported_interfaces=[
        AgentInterface(url=f"{URL_PUBLICA}{CAMINHO_A2A}", protocol_binding="JSONRPC", protocol_version="1.0")
    ],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="reservar-sala",
            name="Reservar sala",
            description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            tags=["salas", "agenda"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            examples=[
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        )
    ],
)


class ContextoV1(DefaultServerCallContextBuilder):
    """Assume A2A-Version 1.0 quando o header nao vem.

    O SDK segue a regra da spec de tratar header ausente como 0.3 e recusa o
    request com -32009. Este endpoint so fala 1.0, os clientes da empresa (e o
    validador) enviam o formato 1.0 sem o header, entao o default aqui e 1.0. Um
    header explicito com outra versao continua sendo recusado pelo SDK.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        headers = contexto.state.setdefault("headers", {})
        if not (headers.get(VERSION_HEADER) or headers.get(VERSION_HEADER.lower())):
            headers[VERSION_HEADER.lower()] = "1.0"
        return contexto


def criar_app() -> Starlette:
    cliente = ClienteMCP(URL_MCP)
    handler = DefaultRequestHandlerV2(
        agent_executor=AgenteDeSalas(cliente),
        task_store=InMemoryTaskStore(),
        agent_card=CARD,
    )

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_: Starlette) -> AsyncIterator[None]:
        await cliente.abrir()
        log(f"central-de-salas A2A ouvindo em {URL_PUBLICA}{CAMINHO_A2A}, MCP em {URL_MCP}")
        try:
            yield
        finally:
            await cliente.fechar()

    rotas = create_agent_card_routes(CARD) + create_jsonrpc_routes(
        handler, rpc_url=CAMINHO_A2A, context_builder=ContextoV1()
    )
    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


def main() -> None:
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
