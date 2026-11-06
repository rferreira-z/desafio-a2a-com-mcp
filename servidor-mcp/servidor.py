"""Servidor MCP da Central de Salas: Streamable HTTP stateless, porta 7301, /mcp.

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo de MRTR na reserva.

MRTR em `reservar_sala`
-----------------------
Rodada 1, intervalo ocupado: a tool NAO chama o cliente (no transporte stateless nao
existe canal de volta). Ela termina a resposta com `InputRequiredResult`:
`inputRequests` com uma unica elicitation em form mode e um `requestState` que leva
o pedido original e as alternativas exatamente como foram oferecidas.

Rodada 2, retry (id JSON-RPC novo, mesmo tools/call): a tool le `inputResponses` e
`requestState` e reconstroi o pedido a partir do proprio estado. O servidor nao
guarda nada em memoria entre as rodadas, entao o retry funciona mesmo depois de o
processo ser reiniciado, e a pergunta respondida e a mesma que foi feita.

Integridade do requestState: `RequestStateSecurity` do SDK, instalado como
middleware de borda. Ele sela todo `requestState` que sai (AES-256-GCM com chave
derivada por HKDF de REQUEST_STATE_SECRET, expiracao de 10 minutos, amarrado ao
metodo, ao nome da tool e ao digest dos argumentos) e verifica todo que volta antes
de a tool rodar. Adulteracao, expiracao ou argumentos divergentes no retry viram
-32602; a tool so ve plaintext que ela mesma produziu.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Literal

import uvicorn
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel, Field, create_model

import dominio
from log import LogDeRequests, log
from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

PORTA = int(os.environ.get("MCP_PORT", "7301"))
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
VALIDADE_REQUEST_STATE_S = 10 * 60  # 10 minutos, dentro da faixa de 5 a 30 exigida
CHAVE_ESCOLHA = "escolha_de_sala"  # chave de inputRequests atribuida pelo servidor
VERSAO_ESTADO = 1
MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _segredo() -> bytes:
    """Le REQUEST_STATE_SECRET do ambiente. Nunca ha segredo no codigo."""
    valor = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not valor:
        sys.exit(
            "REQUEST_STATE_SECRET nao definido. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    try:
        bruto = bytes.fromhex(valor)
    except ValueError:
        bruto = valor.encode()
    if len(bruto) < 32:
        sys.exit(f"REQUEST_STATE_SECRET precisa de no minimo 32 bytes de aleatoriedade (veio {len(bruto)}).")
    return bruto


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=VALIDADE_REQUEST_STATE_S),
)


# ---------------------------------------------------------------- modelos de saida


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ------------------------------------------------------------------ auxiliares


def _validar(sala: str, inicio: str, fim: str) -> dominio.Intervalo:
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.RegraViolada as e:
        raise ToolError(str(e)) from None


def _exigir_elicitation_form(ctx: Context) -> None:
    """Sem capability de elicitation em form mode, nada de elicitation: -32021.

    A capability vem do `_meta` deste request (io.modelcontextprotocol/clientCapabilities),
    nunca de request anterior. Um `elicitation: {}` sem modos conta como form
    (forma anterior aos modos); so `url` nao conta.
    """
    elicitation = ctx.client_capabilities.elicitation if ctx.client_capabilities else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    faltou = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data=MissingRequiredClientCapabilityErrorData(required_capabilities=faltou).model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
    )


def _schema_de_escolha(alternativas: list[str]) -> dict[str, Any]:
    """requestedSchema plano: `sala` restrita as alternativas, na ordem calculada."""
    modelo = create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(alternativas)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return render_elicitation_schema(modelo)


def _pedir_escolha(ctx: Context, pedido: dict[str, str], alternativas: list[str]) -> InputRequiredResult:
    """Termina a resposta pedindo informacao, em vez de chamar o cliente."""
    _exigir_elicitation_form(ctx)
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(message=MENSAGEM_ELICITATION, requested_schema=_schema_de_escolha(alternativas))
    )
    # Tudo que o retry precisa viaja aqui. O middleware do SDK sela este texto na
    # saida; o cliente ve so o token opaco.
    estado = json.dumps({"v": VERSAO_ESTADO, "pedido": pedido, "alternativas": alternativas}, separators=(",", ":"))
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: pergunta}, request_state=estado)


def _reservar(sala: str, pedido: dict[str, str]) -> ReservaOut:
    try:
        reserva = dominio.criar_reserva(sala, pedido["inicio"], pedido["fim"], pedido["responsavel"])
    except dominio.RegraViolada as e:
        raise ToolError(str(e)) from None
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


def _campo(resposta: Any, nome: str) -> Any:
    if isinstance(resposta, dict):
        return resposta.get(nome)
    return getattr(resposta, nome, None)


# ---------------------------------------------------------------------- tools


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    intervalo = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, intervalo)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is not None:
        return _retomar_reserva(ctx)

    pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    intervalo = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, intervalo):
        return _reservar(sala, pedido)
    alternativas = dominio.alternativas(sala, intervalo)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return _pedir_escolha(ctx, pedido, alternativas)


def _retomar_reserva(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Retry do MRTR: o pedido sai do requestState, nao dos argumentos reenviados.

    Quando a tool roda, o SDK ja verificou selo, expiracao e o vinculo do estado
    com o nome da tool e o digest dos argumentos; argumentos divergentes nem chegam
    aqui (-32602). Mesmo assim os valores usados sao os selados.
    """
    try:
        estado = json.loads(ctx.request_state or "")
        pedido: dict[str, str] = estado["pedido"]
        alternativas: list[str] = estado["alternativas"]
        if estado.get("v") != VERSAO_ESTADO:
            raise ValueError("versao")
    except (ValueError, KeyError, TypeError):
        raise MCPError(code=INVALID_PARAMS, message="Invalid or expired requestState") from None

    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None:
        raise MCPError(code=INVALID_PARAMS, message=f"inputResponses sem a resposta para '{CHAVE_ESCOLHA}'")

    acao = _campo(resposta, "action")
    if acao in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado" if acao == "decline" else "cancelado")
    if acao != "accept":
        raise MCPError(code=INVALID_PARAMS, message="inputResponses com action invalida")

    escolhida = (_campo(resposta, "content") or {}).get("sala")
    if escolhida not in alternativas:
        raise MCPError(code=INVALID_PARAMS, message="Escolha fora das alternativas oferecidas")

    intervalo = _validar(escolhida, pedido["inicio"], pedido["fim"])
    if dominio.conflitos(escolhida, intervalo):
        # A alternativa foi ocupada entre a pergunta e o retry: pergunta de novo,
        # com as alternativas de agora para o pedido original.
        novas = dominio.alternativas(pedido["sala"], _validar(pedido["sala"], pedido["inicio"], pedido["fim"]))
        if not novas:
            raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
        return _pedir_escolha(ctx, pedido, novas)
    return _reservar(escolhida, pedido)


# ------------------------------------------------------------------- resource


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas. A primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


# ----------------------------------------------------------------------- app


def criar_app() -> Any:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host=HOST)
    return LogDeRequests(app)


def main() -> None:
    log(f"central-de-salas MCP ouvindo em http://{HOST}:{PORTA}/mcp (politica {dominio.POLITICA_VERSAO})")
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
