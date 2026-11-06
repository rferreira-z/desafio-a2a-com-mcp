"""A ponte: onde o MRTR do MCP encontra a Task do A2A.

Fluxo de uma Task:

1. SendMessage sem taskId -> Task nova (SUBMITTED) -> WORKING.
2. tools/list (descoberta em runtime) e resources/read politica://uso (versao).
3. tools/call reservar_sala com os argumentos do pedido.
4. O resultado decide o estado da Task (ver `_aplicar_resultado`):
   - complete sem isError, reservado=true  -> COMPLETED com o artifact `reserva`
   - complete sem isError, reservado=false -> CANCELED
   - complete com isError                  -> FAILED com a mensagem da tool
   - input_required                        -> INPUT_REQUIRED, e o requestState
                                              fica guardado em `self._pausas[task_id]`
5. SendMessage com taskId e `escolha=<x>` -> o agente repete o tools/call original
   com um id JSON-RPC novo, levando inputResponses (mesma chave) e o requestState
   ecoado sem modificacao (ver `_retomar`).

O agente nao conhece regra de sala: ele so traduz protocolo. As alternativas que
ele mostra sao as que vieram no enum da elicitation, e a validacao da escolha e
contra esse mesmo enum.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from mcp.shared.exceptions import MCPError

from cliente_mcp import ClienteMCP, Trace
from log import log

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"
ESCOLHA_RECUSAR = "recusar"

PEDIDO = re.compile(r"^\s*reservar\s+(?P<campos>.+?)\s*$", re.IGNORECASE | re.DOTALL)
CAMPO = re.compile(r"(\w+)=(\S+)")
ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


@dataclass
class Pausa:
    """O que a Task interrompida precisa para retomar. Fica so no agente.

    `request_state` e opaco: nunca e aberto, interpretado ou devolvido ao cliente A2A.
    """

    argumentos: dict[str, Any]
    chave: str  # chave que o servidor atribuiu em inputRequests
    campo: str  # propriedade do requestedSchema a preencher
    alternativas: list[str]  # enum da elicitation, na ordem recebida
    request_state: str
    politica: str
    trace: Trace


def _versao_da_politica(texto: str) -> str:
    primeira = texto.splitlines()[0] if texto else ""
    chave, _, valor = primeira.partition(":")
    if chave.strip().lower() != "versao" or not valor.strip():
        raise ValueError("resource politica://uso sem a linha 'versao:' no topo")
    return valor.strip()


def _texto(resultado: types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, types.TextContent))


def _linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


class AgenteDeSalas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        # Estado pausado por Task: duas Tasks pausadas nunca trocam de requestState.
        self._pausas: dict[str, Pausa] = {}

    # ------------------------------------------------------------ A2A: entrada

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        trace_header = Trace.do_header(self._header(context, "traceparent"))
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)  # TASK_STATE_SUBMITTED
            await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await self._novo_pedido(context.get_user_input(), updater, trace_header or Trace.novo())
            return

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        pausa = self._pausas.get(task.id)
        if pausa is None:
            # Task nao terminal mas sem pausa conhecida (ex.: agente reiniciado).
            await updater.failed(self._msg(updater, "Task sem pedido pendente para continuar."))
            return
        if trace_header is not None:
            pausa.trace = trace_header
        await self._retomar(context.get_user_input(), pausa, updater)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            return
        self._pausas.pop(task.id, None)
        await TaskUpdater(event_queue, task.id, task.context_id).cancel()

    # ------------------------------------------------------- fluxo do pedido

    async def _novo_pedido(self, texto: str, updater: TaskUpdater, trace: Trace) -> None:
        log(f"a2a task={updater.task_id} pedido={texto!r} trace-id={trace.trace_id}")
        await updater.start_work()  # TASK_STATE_WORKING

        casamento = PEDIDO.match(texto)
        campos = dict(CAMPO.findall(casamento.group("campos"))) if casamento else {}
        if not campos:
            await updater.failed(self._msg(updater, f"Pedido invalido. Formato esperado: {FORMATO_PEDIDO}"))
            return

        try:
            # Descoberta em runtime: o agente nao carrega lista fixa de tools.
            tools = {t.name: t for t in await self.mcp.listar_tools(trace)}
            tool = tools.get(TOOL_RESERVA)
            if tool is None:
                await updater.failed(self._msg(updater, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}."))
                return
            propriedades = list((tool.input_schema or {}).get("properties", {}))
            obrigatorios = (tool.input_schema or {}).get("required", [])
            faltando = [p for p in obrigatorios if p not in campos]
            if faltando:
                await updater.failed(
                    self._msg(updater, f"Pedido sem {', '.join(faltando)}. Formato esperado: {FORMATO_PEDIDO}")
                )
                return
            argumentos = {p: campos[p] for p in propriedades if p in campos}

            politica = _versao_da_politica(await self.mcp.ler_resource(URI_POLITICA, trace))
            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, trace)
        except (MCPError, ValueError, OSError) as e:
            await updater.failed(self._msg(updater, f"Falha ao falar com o servidor MCP: {e}"))
            return

        await self._aplicar_resultado(resultado, argumentos, politica, trace, updater)

    async def _retomar(self, texto: str, pausa: Pausa, updater: TaskUpdater) -> None:
        """Continuacao de uma Task pausada: aqui o requestState volta ao servidor."""
        casamento = ESCOLHA.match(texto)
        valor = casamento.group("valor") if casamento else None
        log(f"a2a task={updater.task_id} continuacao={texto!r}")

        if valor == ESCOLHA_RECUSAR:
            resposta = types.ElicitResult(action="decline")
        elif valor in pausa.alternativas:
            resposta = types.ElicitResult(action="accept", content={pausa.campo: valor})
        else:
            # Escolha fora do enum: a Task continua pausada, com a mesma lista.
            await updater.requires_input(self._msg(updater, _linha_alternativas(pausa.alternativas)))
            return

        await updater.start_work()
        try:
            # Mesmo tools/call, id JSON-RPC novo (o SDK numera cada request), mesma
            # chave de inputRequests e o requestState ecoado sem modificacao.
            resultado = await self.mcp.chamar_tool(
                TOOL_RESERVA,
                pausa.argumentos,
                pausa.trace,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except (MCPError, OSError) as e:
            self._pausas.pop(updater.task_id, None)
            await updater.failed(self._msg(updater, f"Falha ao retomar no servidor MCP: {e}"))
            return

        self._pausas.pop(updater.task_id, None)
        await self._aplicar_resultado(resultado, pausa.argumentos, pausa.politica, pausa.trace, updater)

    # --------------------------------------------- MCP -> estado da Task A2A

    async def _aplicar_resultado(
        self,
        resultado: types.CallToolResult | types.InputRequiredResult,
        argumentos: dict[str, Any],
        politica: str,
        trace: Trace,
        updater: TaskUpdater,
    ) -> None:
        if isinstance(resultado, types.InputRequiredResult):
            await self._pausar(resultado, argumentos, politica, trace, updater)
            return

        if resultado.is_error:
            await updater.failed(self._msg(updater, _texto(resultado)))
            return

        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            motivo = dados.get("motivo") or "recusado"
            await updater.cancel(self._msg(updater, f"Reserva nao realizada: {motivo}."))
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,  # versao lida do resource politica://uso
        }
        await updater.add_artifact([new_text_part(json.dumps(reserva, ensure_ascii=False))], name="reserva")
        await updater.complete(
            self._msg(updater, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")
        )
        log(f"a2a task={updater.task_id} concluida reserva={reserva['reserva']} sala={reserva['sala']}")

    async def _pausar(
        self,
        resultado: types.InputRequiredResult,
        argumentos: dict[str, Any],
        politica: str,
        trace: Trace,
        updater: TaskUpdater,
    ) -> None:
        """input_required do MCP -> TASK_STATE_INPUT_REQUIRED do A2A."""
        pedidos = resultado.input_requests or {}
        estado = resultado.request_state
        elicitacao = None
        if len(pedidos) == 1 and estado:
            chave, pedido = next(iter(pedidos.items()))
            if isinstance(pedido, types.ElicitRequest) and getattr(pedido.params, "mode", "form") == "form":
                elicitacao = (chave, pedido.params.requested_schema)
        if elicitacao is None:
            await updater.failed(self._msg(updater, "O servidor MCP pediu uma entrada que o agente nao sabe repassar."))
            return

        chave, schema = elicitacao
        campo, alternativas = self._opcoes(schema)
        if not campo or not alternativas:
            await updater.failed(self._msg(updater, "A elicitation do servidor MCP nao trouxe alternativas."))
            return

        self._pausas[updater.task_id] = Pausa(
            argumentos=argumentos,
            chave=chave,
            campo=campo,
            alternativas=alternativas,
            request_state=estado,
            politica=politica,
            trace=trace,
        )
        await updater.requires_input(self._msg(updater, _linha_alternativas(alternativas)))
        log(f"a2a task={updater.task_id} pausada {_linha_alternativas(alternativas)}")

    # ------------------------------------------------------------- auxiliares

    @staticmethod
    def _opcoes(schema: dict[str, Any]) -> tuple[str | None, list[str]]:
        """Primeira propriedade restrita do requestedSchema: enum, ou const quando ha uma so."""
        for nome, definicao in (schema.get("properties") or {}).items():
            if isinstance(definicao.get("enum"), list):
                return nome, [str(v) for v in definicao["enum"]]
            if "const" in definicao:
                return nome, [str(definicao["const"])]
        return None, []

    @staticmethod
    def _msg(updater: TaskUpdater, texto: str) -> Any:
        return updater.new_agent_message([new_text_part(texto)])

    @staticmethod
    def _header(context: RequestContext, nome: str) -> str | None:
        chamada = getattr(context, "_call_context", None) or getattr(context, "call_context", None)
        headers = (getattr(chamada, "state", None) or {}).get("headers") or {}
        return headers.get(nome)
