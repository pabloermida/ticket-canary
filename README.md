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
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4.1-mini"

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
```

3) Rodar localmente (modo de teste por IDs)

Por padrão, `python3 main.py` executa um modo de teste local que busca e processa tickets específicos por ID (ignorando filtros de data) e imprime a resposta da IA para cada um.

IDs padrão: `["3012", "2321", "2207", "3342"]`

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
    "sugestao_solucao": "..."
  }
}
```

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
