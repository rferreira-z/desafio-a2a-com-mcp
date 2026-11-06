# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do módulo Desenvolvimento de Agentes com IA. São dois processos
Python separados, que conversam por HTTP:

| Processo | Porta | Endpoint | Papel |
|---|---|---|---|
| `servidor-mcp/` | 7301 | `POST /mcp` (Streamable HTTP, stateless, JSON) | Servidor MCP: 3 tools, 1 resource, MRTR na reserva |
| `agente/` | 7300 | `GET /.well-known/agent-card.json`, `POST /a2a` (JSON-RPC 2.0) | Servidor A2A v1.0 por fora, host MCP por dentro |

Stack: SDK oficial do MCP `mcp==2.3.0` (revisão `2026-07-28`), `a2a-sdk==1.3.0` (A2A v1.0),
`uvicorn` e `starlette`. As versões estão travadas em cada `pyproject.toml` e em cada `uv.lock`.
Não há LLM no caminho de execução: o agente interpreta um texto de formato fixo com regex e
decide por regra.

## Como rodar

Pré-requisitos: Python 3.11 ou superior e [uv](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`). O validador roda com o `python3` do sistema,
na versão 3.10 ou superior, sem dependência externa.

```bash
git clone <url-deste-fork> desafio-a2a-com-mcp
cd desafio-a2a-com-mcp
```

**Terminal 1, servidor MCP.** A chave que protege o `requestState` vem de
`REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Gere uma chave sua e nunca a
versione:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
./run-mcp.sh
```

O log de cada request (método, id, capabilities, traceparent e status HTTP) sai no stderr
deste terminal. O processo se recusa a subir sem a variável ou com menos de 32 bytes.

**Terminal 2, agente A2A:**

```bash
./run-agente.sh
```

Na primeira execução, cada script cria o `.venv` do seu processo a partir do `uv.lock`
(`uv run --frozen`).

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O card fica em `curl http://localhost:7300/.well-known/agent-card.json`. Variáveis
opcionais: `MCP_PORT` e `MCP_HOST` no servidor; `AGENT_PORT`, `AGENT_HOST`,
`AGENT_PUBLIC_URL` e `MCP_URL` no agente. Os valores padrão são os do enunciado.

## Onde a ponte acontece

A ponte inteira mora em [`agente/executor.py`](agente/executor.py).

- **input_required vira TASK_STATE_INPUT_REQUIRED.** `_aplicar_resultado` (linha 199)
  recebe o resultado cru do `tools/call`. O cliente MCP chama com
  `allow_input_required=True` e sem callback que responda sozinho, então o `input_required`
  chega intacto ao agente. Quando o resultado é `InputRequiredResult`, `_pausar` (linha 227)
  faz duas coisas:
  - guarda em `self._pausas[task_id]` (linha 253) a chave de `inputRequests`, o enum da
    elicitation, os argumentos originais e o `requestState` opaco;
  - publica `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...`, na ordem do enum
    (linha 262).

  O estado pausado é indexado pela Task, então duas Tasks pausadas nunca trocam de
  `requestState`. O `requestState` nunca é aberto e nunca sai no card, no artifact nem nas
  mensagens.
- **O requestState volta para o servidor.** Isso acontece em `_retomar` (linha 155),
  quando chega um `SendMessage` com `taskId` e `escolha=<x>`:
  - uma escolha fora do enum deixa a Task em `INPUT_REQUIRED` e repete a lista (linha 167);
  - `escolha=recusar` vira `action: decline`;
  - uma escolha válida vira `action: accept`.

  Em seguida o agente repete o mesmo `tools/call reservar_sala`, com
  `input_responses={chave: resposta}` e `request_state=pausa.request_state`, ecoado sem
  modificação (linhas 178 e 179). O SDK numera cada request, então o retry sai com um id
  JSON-RPC novo. No stderr do servidor aparece o par, por exemplo `id=6` e depois
  `id=7 ... retry=sim`.

Do lado do servidor, o outro extremo da ponte está em
[`servidor-mcp/servidor.py`](servidor-mcp/servidor.py):

- `reservar_sala` termina a resposta pedindo informação (`_pedir_escolha`, linha 163). Ela não
  chama o cliente: no transporte stateless não há canal de volta.
- `_retomar_reserva` (linha 233) reconstrói o pedido a partir do `requestState` e conclui.

## Decisões técnicas

**Proteção do requestState.** O servidor usa o utilitário do próprio SDK,
`RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)`. O `MCPServer` o instala como
middleware de borda:

- **Selo na saída.** Todo `requestState` que sai é selado com AES-256-GCM. A chave é
  derivada do segredo por HKDF-SHA256, e o token tem o formato `v1.<kid|nonce|ciphertext>`.
  Além de assinado, o estado é cifrado, então o cliente não consegue lê-lo.
- **Verificação na volta.** Todo `requestState` que volta é verificado antes de a tool rodar:
  integridade (tag GCM), expiração e vínculo com o método, o nome da tool e o digest dos
  argumentos.
- **Recusa.** Qualquer falha nessa verificação vira `-32602 Invalid or expired requestState`
  com HTTP 400. O motivo real vai só para o log do servidor: `seal`, `expired` ou
  `request binding`.

Detalhes que completam a proteção:

- **Validade:** 10 minutos, dentro da faixa de 5 a 30 exigida.
- **Argumentos adulterados no retry:** são rejeitados pelo vínculo com o digest dos
  argumentos. Mesmo assim, a tool usa os valores selados, não os reenviados.
- **Chave:** vem só do ambiente. Não há segredo no código. Uma chave com menos de 32 bytes
  impede o processo de subir.

**O que viaja no requestState.** Antes de ser selado, o estado é
`{"v":1,"pedido":{sala,inicio,fim,responsavel},"alternativas":[...]}`. O servidor não guarda
nada em memória entre o `input_required` e o retry. Ele reconstrói o pedido e valida a escolha
contra as alternativas que ofereceu de fato, então um retry apresentado depois de reiniciar o
processo funciona. Se a sala escolhida tiver sido ocupada nesse meio tempo, o servidor pergunta
de novo, com as alternativas atuais, e o agente volta a pausar a Task.

Por que não usei os resolvers do SDK (`Resolve`/`Elicit`) no MRTR: o resolver recalcula a
pergunta a cada rodada, e o SDK só aceita a resposta se a pergunta renderizada for idêntica à
original. Depois de um restart, as reservas em memória somem, as alternativas recalculadas
mudam e a pergunta é refeita em vez de concluir a reserva. Devolver o `InputRequiredResult` à
mão, com a pergunta selada no estado, segue a letra do enunciado ("reconstrói o pedido original
a partir do próprio requestState") e continua passando pelo mesmo middleware de selo do SDK.

**Capability e erros de protocolo.**

- **`_meta` incompleto.** A checagem de `_meta` é feita pelo SDK em cada request, sem herdar
  nada de request anterior: sem `protocolVersion` ou sem `clientCapabilities`, a resposta é
  `-32602` com HTTP 400.
- **Elicitation em form mode.** A tool só monta a elicitation se o `_meta` daquele request
  declarar elicitation em form mode. Sem isso, responde `-32021` com
  `data.requiredCapabilities = {"elicitation":{"form":{}}}` e HTTP 400.
- **Header que não bate com o corpo.** É recusado com `-32020`, pelo SDK.

**Estado das Tasks.** As Tasks ficam no `InMemoryTaskStore` do `a2a-sdk`, que guarda id,
contextId, status, histórico e artifacts. O estado pausado do MRTR (`Pausa`) fica num dicionário
do executor, indexado por `task_id`. As reservas criadas ficam em memória no servidor MCP e não
sobrevivem a um restart, como o enunciado permite. O SDK do A2A garante que uma Task em estado
terminal não volta a `WORKING` e recusa um `SendMessage` para ela.

**Agente como host MCP.**

- **Descoberta e política a cada Task.** No início de cada Task, o agente faz `tools/list`
  (descoberta em runtime: os argumentos enviados saem do `inputSchema` descoberto) e
  `resources/read politica://uso`. Do resource ele extrai a versão da primeira linha, que vai
  no campo `politica` do artifact.
- **Envelope de cada request.** Todo request leva protocolVersion, clientInfo e
  `clientCapabilities = {"elicitation":{"form":{}}}` no `_meta`. Os headers
  `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` são espelhados pelo SDK.
- **Trace.** O agente guarda o trace-id do header `traceparent` da chamada A2A junto com a
  Task. Cada request MCP leva no `_meta` esse mesmo trace-id, com um span-id novo.

**Mapeamento de resultado para estado da Task:**

| Resultado MCP | Estado da Task |
|---|---|
| `complete` com `reservado=true` | `TASK_STATE_COMPLETED`, com o artifact `reserva` |
| `complete` com `reservado=false` (decline) | `TASK_STATE_CANCELED` |
| `isError: true` | `TASK_STATE_FAILED`, com o texto da tool na mensagem de status |
| `input_required` | `TASK_STATE_INPUT_REQUIRED` |

### Onde os SDKs divergiram do contrato (com evidência)

1. **`A2A-Version` ausente.** O `a2a-sdk` 1.3.0 segue a regra da spec de tratar header
   ausente como `0.3` e recusa o request com `-32009`
   (`a2a/utils/version_validator.py`: *"If the header is missing or empty, it is interpreted
   as constants.PROTOCOL_VERSION_0_3"*). O validador e os exemplos de wire mandam v1.0 sem
   esse header. Em vez de reescrever o binding, usei o ponto de extensão do próprio SDK:
   `ContextoV1`, um `ServerCallContextBuilder` em `agente/agente.py`, assume `1.0` quando o
   header falta. Um header explícito com outra versão continua sendo recusado.
2. **Capability anunciada pelo cliente MCP.** O `mcp` 2.3.0 anuncia
   `{"elicitation":{"form":{},"url":{}}}` sempre que existe `elicitation_callback`
   (`mcp/client/session.py`, `_build_capabilities`). Ele também sobrescreve o que o chamador
   põe em `_meta`. O agente não sabe repassar URL mode, e o enunciado pede
   `{"elicitation":{"form":{}}}`. Por isso `agente/cliente_mcp.py` estreita o anúncio para
   form, sobrescrevendo apenas esse método. O callback existe só para habilitar a
   capability: com `allow_input_required=True`, ele nunca é chamado.
3. **Prefixo nos erros de execução.** O `mcp` 2.3.0 prefixa os erros de tool com
   `Error executing tool <nome>:`, como o enunciado prevê. O agente repassa o texto da tool
   sem alterar, então a Task FAILED mostra, por exemplo,
   `Error executing tool reservar_sala: Sala inexistente: sala-delorean`.

## Saída do validador

Última execução, com os dois processos recém-iniciados (código de saída 0):

```text
trace-id desta execucao: 5f0f36e2f2e26b2ae2502047ec3f9adf
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Trecho do stderr do servidor MCP na mesma execução. O trace-id do validador chega em
`tools/list`, `resources/read` e `tools/call`, nessa ordem:

```text
mcp method=tools/list id=1 caps={"elicitation":{"form":{}}} traceparent=00-5f0f36e2f2e26b2ae2502047ec3f9adf-e43084498d2153dc-01 http=200
mcp method=resources/read id=2 uri=politica://uso caps={"elicitation":{"form":{}}} traceparent=00-5f0f36e2f2e26b2ae2502047ec3f9adf-f845eb376d2c5a6f-01 http=200
mcp method=tools/call id=3 name=reservar_sala caps={"elicitation":{"form":{}}} traceparent=00-5f0f36e2f2e26b2ae2502047ec3f9adf-e5f64659b5afa10d-01 http=200
```
