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
- Apenas imprime o resumo/sugestão da IA por ticket. Não envia cartões ao Teams e não escreve comentários no Agidesk.

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
