"""Regras de sala: dados, politica de uso, conflito e alternativas.

Modulo puro, sem nada de protocolo. O servidor MCP e o unico dono destas regras:
o agente A2A nunca decide conflito, politica ou alternativa.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

FUSO_SP = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class RegraViolada(Exception):
    """Erro de execucao: vira `isError: true` com a mensagem exata."""


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        return self.inicio < outro.fim and outro.inicio < self.fim


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: dict[str, dict] = {s["id"]: s for s in _ler_json("salas.json")}
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

# Persistencia em memoria: reservas criadas valem ate o processo reiniciar.
RESERVAS: list[dict] = _ler_json("reservas.json")


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise RegraViolada(f"Data invalida: {valor}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_SP)
    return dt


def validar(sala: str, inicio: str, fim: str) -> Intervalo:
    """Aplica as validacoes de sala e de politica, na ordem do enunciado."""
    if sala not in SALAS:
        raise RegraViolada(ERRO_SALA.format(sala=sala))
    intervalo = Intervalo(_instante(inicio), _instante(fim))
    if intervalo.fim <= intervalo.inicio:
        raise RegraViolada(ERRO_INTERVALO)
    local_inicio = intervalo.inicio.astimezone(FUSO_SP)
    local_fim = intervalo.fim.astimezone(FUSO_SP)
    dentro = (
        local_inicio.date() == local_fim.date()
        and ABERTURA <= local_inicio.timetz().replace(tzinfo=None)
        and local_fim.timetz().replace(tzinfo=None) <= FECHAMENTO
    )
    if not dentro:
        raise RegraViolada(ERRO_JANELA)
    if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
        raise RegraViolada(ERRO_DURACAO)
    return intervalo


def conflitos(sala: str, intervalo: Intervalo) -> list[dict]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and intervalo.sobrepoe(Intervalo(_instante(r["inicio"]), _instante(r["fim"])))
    ]


def alternativas(sala: str, intervalo: Intervalo) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, no maximo tres,
    por capacidade crescente e, no empate, por id."""
    minima = SALAS[sala]["capacidade"]
    candidatas = [
        s for s in SALAS.values() if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], intervalo)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def _proximo_id() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict:
    intervalo = validar(sala, inicio, fim)
    if conflitos(sala, intervalo):
        # So acontece se a sala escolhida foi ocupada entre a pergunta e o retry.
        raise RegraViolada(ERRO_SEM_ALTERNATIVA)
    reserva = {"id": _proximo_id(), "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return reserva
