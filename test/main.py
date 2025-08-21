"""
Router de Polling (Agidesk -> OpenAI -> Teams -> Agidesk)

Assume:
- Autenticação via tokens em variáveis de ambiente
- Watermark persistido (KV/arquivo) para evitar retrabalho
- Campos customizados no Agidesk:
  - router_processed (bool) -> formfield_id = FF_ROUTER_PROCESSED
  - router_processed_at (datetime) -> formfield_id = FF_ROUTER_TS
- “Source = email” filtrável no dataset/consulta
"""

from datetime import datetime, timedelta, timezone
import time, json, os

# ---------------------------
# Config
# ---------------------------
CFG = {
  "AGI_BASE": os.getenv("AGI_BASE"),                 # ex: https://{ACCOUNT}.agidesk.com/api/v1
  "AGI_TOKEN": os.getenv("AGI_TOKEN"),
  "AGI_DATASET": "serviceissues",
  "PER_PAGE": 5,
  "FF_ROUTER_PROCESSED": "1234",                     # formfield_id real no teu ambiente
  "FF_ROUTER_TS": "5678",
  "TEAMS_TEAM_ID": os.getenv("TEAMS_TEAM_ID"),
  "TEAMS_CHANNEL_ID": os.getenv("TEAMS_CHANNEL_ID"),
  "MS_GRAPH_TOKEN": os.getenv("MS_GRAPH_TOKEN"),
  "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
  "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
  "POLL_INTERVAL_SEC": 300                           # 5 minutos
}

STATE = {
  "last_seen_updated_at": os.getenv("LAST_SEEN_UPDATED_AT")  # persistir fora em prod
}

# ---------------------------
# Utilidades
# ---------------------------
def now_utc():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ds_time(dt):
    """Datasets do Agidesk geralmente usam 'YYYY-MM-DD HH:MM:SS' sem TZ"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def persist_watermark(ts_iso):
    # TODO: gravar em KV/arquivo/secret
    STATE["last_seen_updated_at"] = ts_iso

def load_watermark():
    # TODO: ler de KV/arquivo/secret
    return STATE.get("last_seen_updated_at")

# ---------------------------
# Agidesk – Client (esboço)
# ---------------------------
def agi_fetch_recent_tickets(initial_dt, final_dt, per_page, source="email"):
    """
    Usa dataset `serviceissues` com janela de tempo e filtro de origem (se disponível).
    Adapte: alguns ambientes trazem `origin` / `source` como campo ou extrafield.
    """
    params = {
      "per_page": str(per_page),
      "page": "1",
      "periodfield": "updated_at",
      "initialdate": ds_time(initial_dt),
      "finaldate": ds_time(final_dt),
      "metadata": "",
      "extrafield": "all",
      "extrafielddetails": ""
    }
    # + filtro por origem (depende do dataset; ex. origin=email, channel=email etc.)
    # params["origin"] = "email"   # se o dataset suportar
    # return http_get_json(f"{CFG['AGI_BASE']}/datasets/{CFG['AGI_DATASET']}", headers=auth, params=params)
    return dataset_mock()  # MOCK para pseudocódigo

def agi_get_ticket_details(service_id):
    # Em muitos casos dá para obter tudo pelo dataset; se precisar, detalhe via endpoint /services/{id}
    # return http_get_json(f"{CFG['AGI_BASE']}/services/{service_id}?extrafield=all&extrafielddetails", headers=auth)
    return details_mock(service_id)  # MOCK

def agi_already_processed(ticket) -> bool:
    tags = ticket.get("tags", [])
    ff = ticket.get("formfields", {})
    processed = ("router:processed" in tags) or (ff.get(CFG["FF_ROUTER_PROCESSED"]) is True)
    return processed

def agi_update_ticket_processed(service_id, ai_json, blog_markdown):
    """
    Atualiza ticket com:
      - descrição do problema / sumário
      - tarefas (com SLAs/estimativas)
      - questões adicionais (checklist)
      - flags/ tags / campos router_processed
      - nota interna com a postagem do blog (ou link)
    """
    formfields = {
      CFG["FF_ROUTER_PROCESSED"]: True,
      CFG["FF_ROUTER_TS"]: iso(now_utc())
    }

    # Exemplo de como mapear tasks para “checklist” via custom fields:
    # (em produção: ou cria campos dinâmicos, ou guarda JSON em um campo texto longo)
    checklist_md = "\n".join([f"- [ ] {t['title']} (ETA {t.get('sla_minutes','?')} min)" 
                              for t in ai_json["remediation"]["tasks"]])

    internal_note = (
      f"AI Analysis v{ai_json.get('version','1.0')}\n"
      f"Summary: {ai_json['problem']['summary']}\n\n"
      f"Proposed tasks:\n{checklist_md}\n\n"
      f"Blog post preview:\n---\n{blog_markdown[:2000]}\n---"  # corta para não estourar limite
    )

    payload = {
      "service": {
        "formfields": formfields,
        "tags": ["router:processed","ai:triaged"],
        "internal_note": internal_note,
        # Exemplos de campos “de negócio” (depende do teu schema):
        "category": ai_json["classification"]["category"],
        "subcategory": ai_json["classification"]["sub_category"],
        "priority": ai_json["classification"]["severity"],   # mapear S1..S4 -> prioridade do Agidesk
        # Se tiver campo de descrição do problema:
        "description": ai_json["problem"]["explanation"]
      }
    }
    # http_put_json(f"{CFG['AGI_BASE']}/services/{service_id}", headers=auth, json=payload)
    return True  # pseudo

# ---------------------------
# OpenAI – Structured Output
# ---------------------------
def build_openai_prompt(ctx):
    """
    Instruções principais:
      - Fazer triagem baseada em documentação do fabricante + busca na web
      - Gerar JSON no schema abaixo
      - Sugerir tarefas com estimativa (min)
      - Perguntas adicionais para debugging
      - Hipóteses de causa com sequência de manipulações (updates, etc.)
      - Produzir um 'blog post' a partir do JSON (texto longo separado)
    """
    return f"""
Você é um engenheiro de suporte de nível 2/3.
Entrada do ticket (resumo + histórico + campos de formulário):
{json.dumps(ctx, ensure_ascii=False, indent=2)}

Tarefas:
1) Leia o contexto e normalize sinais (códigos, mensagens).
2) Consulte mentalmente documentação típica de fabricantes e padrões; se necessário, utilize referência a "parecido com" histórico comum de incidentes (simulado).
3) Produza **apenas** o JSON no SCHEMA abaixo (sem texto fora do JSON).
4) Todas as tarefas devem ter `sla_minutes` estimado.
5) Proponha perguntas adicionais úteis para debugging; priorize aquelas ligadas a sequência de ações que poderiam ter causado o problema (ex.: upgrades, trocas de certificado, mudança de DNS, janelas de manutenção).
6) Liste sinais/telemetria úteis para confirmar hipótese.
7) Inclua `notify` com resumo para publicação no Teams.

IMPORTANTE:
- Respeite o `json_schema` entregue. Não adicione campos.
- Tudo deve ser factual/útil para quem está atendendo.

"""
def openai_schema():
    """
    JSON Schema mínimo para structured output.
    """
    return {
      "name": "ticket_analysis",
      "schema": {
        "type": "object",
        "properties": {
          "version": {"type": "string"},
          "ticket_id": {"type": "string"},
          "classification": {
            "type": "object",
            "properties": {
              "category": {"type": "string"},
              "sub_category": {"type": "string"},
              "severity": {"type": "string"}  # S1/S2/S3/S4
            },
            "required": ["category","sub_category","severity"]
          },
          "problem": {
            "type": "object",
            "properties": {
              "summary": {"type": "string"},
              "explanation": {"type": "string"},
              "likely_causes": {"type": "array", "items":{"type":"string"}},
              "affected_components": {"type": "array", "items":{"type":"string"}}
            },
            "required": ["summary","explanation"]
          },
          "remediation": {
            "type": "object",
            "properties": {
              "tasks": {
                "type": "array",
                "items": {
                  "type":"object",
                  "properties": {
                    "title":{"type":"string"},
                    "owner_team":{"type":"string"},
                    "sla_minutes":{"type":"number"},
                    "steps":{"type":"array","items":{"type":"string"}}
                  },
                  "required":["title","sla_minutes"]
                }
              }
            },
            "required": ["tasks"]
          },
          "debug": {
            "type":"object",
            "properties":{
              "clarifying_questions":{"type":"array","items":{"type":"string"}},
              "signals_to_collect":{"type":"array","items":{"type":"string"}}
            },
            "required":["clarifying_questions"]
          },
          "notify": {
            "type":"object",
            "properties":{
              "teams_channel":{"type":"string"},
              "message_summary":{"type":"string"}
            },
            "required":["message_summary"]
          }
        },
        "required": ["ticket_id","classification","problem","remediation","debug","notify"]
      },
      "strict": True
    }

def call_openai_structured(prompt, schema, ticket_id):
    """
    Chamada com response_format JSON Schema (pseudocódigo).
    """
    # resp = http_post_json("https://api.openai.com/v1/responses", headers=..., json={
    #   "model": CFG["OPENAI_MODEL"],
    #   "input": prompt,
    #   "response_format": {"type":"json_schema","json_schema": schema}
    # })
    # ai_json = json.loads(resp["output"][0]["content"][0]["text"])
    ai_json = {
      "version":"1.0","ticket_id":ticket_id,
      "classification":{"category":"Acesso","sub_category":"SSO","severity":"S2"},
      "problem":{"summary":"Falha 504 no login", "explanation":"Possível intermitência no IdP",
                 "likely_causes":["timeout IdP","rede"], "affected_components":["Auth Gateway"]},
      "remediation":{"tasks":[
        {"title":"Reiniciar conector SSO","owner_team":"Infra","sla_minutes":30,"steps":["acessar painel","reiniciar"]},
        {"title":"Divulgar status no canal #ops","owner_team":"Suporte","sla_minutes":10,"steps":["postar aviso","monitorar"]}
      ]},
      "debug":{
        "clarifying_questions":[
          "Houve atualização recente do IdP/metadata SAML?",
          "Mudança de DNS/TTL nas últimas 24h?",
          "Picos de 5xx correlacionados no gateway?"
        ],
        "signals_to_collect":["latência IdP","erros 5xx por minuto","healthcheck do conector"]
      },
      "notify":{"teams_channel":"ops-status","message_summary":"S2 • Login SSO intermitente – 2 ações propostas"}
    }
    return ai_json

def render_blog(ai_json, ticket, docs_links=None):
    """
    Converte o JSON em um 'post' (Markdown) para o Teams (ou wiki):
    """
    md = []
    md.append(f"### Análise de Ticket {ai_json['ticket_id']} — {ai_json['classification']['severity']}")
    md.append(f"**Resumo**: {ai_json['problem']['summary']}")
    md.append("")
    md.append("**Contexto**:")
    md.append(f"- Solicitante: {ticket.get('requester','?')}")
    md.append(f"- Criado: {ticket.get('created_at','?')}")
    md.append("")
    md.append("**Hipótese/Explicação**:")
    md.append(ai_json["problem"]["explanation"])
    md.append("")
    md.append("**Ações sugeridas (com ETA):**")
    for t in ai_json["remediation"]["tasks"]:
        md.append(f"- {t['title']} — ETA {t.get('sla_minutes','?')} min")
        if t.get("steps"):
            for step in t["steps"]:
                md.append(f"  - {step}")
    md.append("")
    md.append("**Perguntas para depuração:**")
    for q in ai_json["debug"]["clarifying_questions"]:
        md.append(f"- {q}")
    if docs_links:
        md.append("\n**Referências do fabricante / histórico semelhante:**")
        for link in docs_links:
            md.append(f"- {link}")
    return "\n".join(md)

# ---------------------------
# Teams – postagem
# ---------------------------
def teams_post_markdown(md_text):
    """
    Publica no canal de Teams via Microsoft Graph.
    """
    # http_post_json(f"https://graph.microsoft.com/v1.0/teams/{CFG['TEAMS_TEAM_ID']}/channels/{CFG['TEAMS_CHANNEL_ID']}/messages",
    #                headers=graph_headers(), json={"body":{"contentType":"html","content": md_to_html(md_text)}})
    return True

# ---------------------------
# Orquestração principal
# ---------------------------
def main_loop():
    last_seen = load_watermark() or iso(now_utc() - timedelta(hours=1))

    while True:
        try:
            initial = datetime.fromisoformat(last_seen.replace("Z","+00:00")) - timedelta(seconds=60)
            final = now_utc()

            tickets_ds = agi_fetch_recent_tickets(initial, final, CFG["PER_PAGE"], source="email")
            tickets = sorted(tickets_ds["data"], key=lambda x: x["updated_at"])

            for t in tickets:
                if agi_already_processed(t):
                    last_seen = t["updated_at"]; persist_watermark(last_seen); continue

                full = agi_get_ticket_details(t["id"])
                ctx = {
                  "ticket": {
                    "id": t["id"],
                    "title": t.get("title"),
                    "description": t.get("description"),
                    "created_at": t.get("created_at"),
                    "requester": t.get("requester"),
                    "formfields": full.get("formfields", {}),
                    "history": full.get("history", [])
                  }
                }

                prompt = build_openai_prompt(ctx)
                schema = openai_schema()
                ai_json = call_openai_structured(prompt, schema, ticket_id=str(t["id"]))

                blog = render_blog(ai_json, t, docs_links=full.get("docs_links"))
                teams_post_markdown(blog)

                agi_update_ticket_processed(service_id=t["id"], ai_json=ai_json, blog_markdown=blog)

                # avança watermark de forma monotônica
                last_seen = max(last_seen, t["updated_at"])
                persist_watermark(last_seen)

        except Exception as e:
            # log e backoff simples
            print("ERROR:", e)
            time.sleep(30)

        # espera até o próximo ciclo
        time.sleep(CFG["POLL_INTERVAL_SEC"])

# ---------------------------
# Mocks para este pseudocódigo
# ---------------------------
def dataset_mock():
    return {
      "data":[
        {"id":"AGI-101","title":"Timeout 504","description":"Falha no login",
         "updated_at":"2025-08-21T14:10:00Z","created_at":"2025-08-21T14:00:00Z",
         "tags":[], "requester":"ana@empresa.com"}
      ],
      "metadata":{"total":1,"page":1,"per_page":5}
    }

def details_mock(service_id):
    return {
      "id": service_id,
      "formfields": {"impact":"multiuser","urgency":"alta"},
      "history": ["created","email_add"], 
      "docs_links": ["https://fabricante.example.com/sso/timeouts"]
    }

# ---------------------------
# Start
# ---------------------------
if __name__ == "__main__":
    main_loop()
