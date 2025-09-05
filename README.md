# ticket-canary

## Executar localmente (via variáveis de ambiente)

Passo a passo para rodar a função localmente usando `python3 main.py` e variáveis exportadas no shell.

1) Criar e ativar a venv, depois instalar dependências

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

2) Exportar variáveis obrigatórias (exemplos)

```bash
export AGIDESK_ACCOUNT_ID="infiniit"            # Seu tenant Agidesk
export AGIDESK_APP_KEY="SEU_TOKEN"              # API Key do Agidesk
export TEAMS_WEBHOOK_URL="https://...incomingwebhook..."  # Webhook do Teams

# OpenAI (usado para resumo/sugestão no card do Teams)
# Agora usando a Responses API (\`/v1/responses\`) com \`text.format: {type: json_object}\` para saída estruturada.
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-5-nano"

# Modo de execução: development = leitura + Teams; production = também escreve comentário no Agidesk
export MODE="development"

# Janela de busca de tickets (segundos)
export FETCH_TIME_SECONDS="300"

# Armazenamento para estado (IDs processados)
# Opção A: usar Azurite local (precisa do emulador rodando)
export AzureWebJobsStorage="UseDevelopmentStorage=true"
# OU Opção B: usar uma connection string real
# export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=..."

# (Opcional) Customiza o link do card para abrir o ticket diretamente
export AGIDESK_TICKET_URL_TEMPLATE="https://cliente.infiniit.com.br/br/painel/atendimento/{id}"

# (Opcional) Enviar mensagens ao Teams no modo de teste por IDs
# Define estilo do Teams: "card" (padrão) ou "text"
export LOCAL_TEST_SEND_TEAMS=0
export TEAMS_MESSAGE_STYLE="card"
export OPENAI_ENABLE_WEB_SEARCH=0   # opcional: ferramenta de pesquisa web (somente com text.format = {type: text})
export OPENAI_TEXT_DEBUG=0          # opcional: força saída em texto simples para debug

Notas sobre Web Search e Debug
- O recurso de pesquisa web da Responses API não é compatível com modo JSON (text.format = {type: json_object/json_schema}).
- Para usar Web Search, defina o formato como texto (text.format = {type: text}) ou desative a exigência de JSON no código.
- Neste projeto, quando OPENAI_ENABLE_WEB_SEARCH=1 estiver ativo, a ferramenta só será ligada quando o formato for texto; em JSON, ela permanece desativada para evitar erros.
- Para depuração rápida, defina OPENAI_TEXT_DEBUG=1 para forçar formato texto e mapear a resposta crua para `resumo_problema` (e `sugestao_solucao` vazio). Útil quando o JSON do modelo vem inválido ou vazio.
```
# (Opcional) Limite de tokens de saída (Responses API)
export OPENAI_MAX_OUTPUT_TOKENS=1200

3) Rodar localmente (modo de teste por IDs)

Por padrão, `python3 main.py` executa um modo de teste local que busca e processa tickets específicos por ID (ignorando filtros de data) e imprime a resposta da IA para cada um.

IDs padrão: `["3012", "2321", "2207", "3342, 3505"]`

```bash
python3 main.py
```

Para customizar os IDs, defina `LOCAL_TEST_TICKET_IDS` (JSON ou CSV):

```bash
export LOCAL_TEST_TICKET_IDS='["3012","2321","2207","3342"]'
# ou
export LOCAL_TEST_TICKET_IDS='3012,2321,2207,3342'
```

Saída esperada (exemplo simplificado por ticket):

```json
{
  "ticket_id": "3012",
  "title": "Problema no acesso",
  "ai_summary": {
    "resumo_problema": "...",
    "sugestao_solucao": "...",
    "sugestao_solucao_lista": ["opcional: itens em lista"],
    "sugestao_solucao_lista_ordenada": ["opcional: passos numerados"]
  }
}
```

Observação: quando a sugestão contiver listas/etapas, o modelo retorna também os arrays opcionais
`sugestao_solucao_lista` (não ordenada) e/ou `sugestao_solucao_lista_ordenada` (ordenada). O comentário
enviado ao Agidesk usa diretamente esses campos estruturados para renderizar as listas corretamente.

Observações do modo de teste
- Ignora janelas/períodos de busca; usa `get_issue` por ID.
- Não grava estado em Blob e não exige Azurite.
- Por padrão é somente leitura: imprime o resumo/sugestão da IA e não envia ao Teams nem escreve comentários no Agidesk.
- Opcional: defina `LOCAL_TEST_SEND_TEAMS=1` para enviar notificações ao Teams durante o teste local (requer `TEAMS_WEBHOOK_URL`). Use `TEAMS_MESSAGE_STYLE=text` para mensagem simples ou deixe `card` para Adaptive Card.

3.1) Incluir comentários no Agidesk (opcional, via flag)

Para testar localmente a inclusão de comentários no Agidesk para os IDs fornecidos, habilite explicitamente a escrita com a variável `LOCAL_TEST_WRITE_COMMENTS=1` (ou execute com `MODE=production`).

Exemplo:

```bash
export AGIDESK_ACCOUNT_ID="infiniit"
export AGIDESK_APP_KEY="SEU_TOKEN"
export OPENAI_API_KEY="sk-..."              # opcional, mas recomendado
export LOCAL_TEST_TICKET_IDS='3012,2321,2207,3342'  # seus IDs de teste
export LOCAL_TEST_WRITE_COMMENTS=1           # habilita escrita de comentários
python3 main.py
```

Com `LOCAL_TEST_WRITE_COMMENTS=1` ativo (ou `MODE=production`), o script irá:
- Buscar cada ticket por ID;
- Gerar o resumo/sugestão da IA;
- Adicionar um comentário no Agidesk com o conteúdo da IA (HTML simples).

Observações de segurança
- Esse modo faz escrita real no Agidesk. Use em um ambiente/tenant de testes ou com IDs de tickets de teste.
- Caso não queira escrever comentários, deixe `LOCAL_TEST_WRITE_COMMENTS` desativado (padrão) e/ou `MODE=development`.

3.2) Enviar mensagens ao Teams (opcional, independente do comentário)

Para enviar notificações ao Teams no modo de teste por IDs, ative `LOCAL_TEST_SEND_TEAMS=1`. Isso não exige habilitar escrita de comentários.

Exemplo (apenas Teams, sem comentários):

```bash
export TEAMS_WEBHOOK_URL="https://...incomingwebhook..."
export AGIDESK_ACCOUNT_ID="infiniit"
export AGIDESK_APP_KEY="SEU_TOKEN"
export LOCAL_TEST_TICKET_IDS='3012,2321,2207,3342'
export LOCAL_TEST_SEND_TEAMS=1
# opcional: forçar texto simples ao invés de card
# export TEAMS_MESSAGE_STYLE=text
python3 main.py
```

Para enviar Teams e também comentar no Agidesk, combine com `LOCAL_TEST_WRITE_COMMENTS=1` (ou `MODE=production`).

4) Rodar o pipeline completo (timer) localmente

Se preferir executar o fluxo completo (busca por período, Teams, etc.), use:

```bash
RUN_TIMER=1 python3 main.py
```

Observações
- As variáveis podem ser definidas via `export` (tomam precedência) ou via `local.settings.json` (o `main.py` lê automaticamente).
- Notificações do Teams usam Adaptive Cards via Incoming Webhook. Se o post do card falhar, há fallback para mensagem de texto simples.
- O link do card pode ser customizado via `AGIDESK_TICKET_URL_TEMPLATE`.

## Análise de Imagens (IA)

- Quando o ticket contém imagens no HTML (`htmlcontent` com tags `<img src="...">`), o sistema extrai as URLs e as envia junto com o texto para a OpenAI.
- Funciona tanto no modo local por IDs (`python3 main.py`) quanto no pipeline com timer (`RUN_TIMER=1 python3 main.py`).
- Somente links `http(s)` são considerados. Se a URL for privada/expirada, a análise da imagem pode não ocorrer.
- A busca do timer já inclui `fields='id,title,content,htmlcontent,created_at,lists'` para garantir que `htmlcontent` chegue na análise.

## Template de mensagem do Teams

Este projeto agora suporta um modelo de mensagem em texto para o Teams, conforme abaixo:

```
🚨 Novo Chamado na Fila! 🚨

Contato: [Nome do Contato]
Empresa: [Nome da Empresa]  (se houver)
Ticket: #[ID do Ticket]: [Título do Chamado]

👇 Clique para abrir o chamado:
[Link para o Chamado]

@Time de Suporte, alguém pode assumir?
```

- Para enviar este texto no lugar do Adaptive Card, defina: `export TEAMS_MESSAGE_STYLE="text"`.
- Por padrão (`TEAMS_MESSAGE_STYLE` ausente ou diferente de `text`), um Adaptive Card é enviado, mas com o conteúdo reorganizado para refletir o mesmo template e um botão de "Abrir no Agidesk".
- Observação: menções reais (@) não são suportadas por Incoming Webhooks do Teams; a linha com "@Time de Suporte" é apenas texto e não dispara notificação de menção.
