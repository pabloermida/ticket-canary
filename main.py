#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Router de Polling (Agidesk -> OpenAI Responses API -> Teams -> Agidesk)
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import requests
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agidesk import AgideskAPI, Ticket

# ============ VARS / CONFIG ============

AGIDESK_ACCOUNT_ID = os.getenv("AGIDESK_ACCOUNT_ID", "")
AGIDESK_APP_KEY = os.getenv("AGIDESK_APP_KEY", "")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

ROUTER_PROCESSED_TAG = os.getenv("ROUTER_PROCESSED_TAG", "router:processed")

POLL_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "300"))
FETCH_TIME_MINUTES = int(os.getenv("FETCH_TIME_MINUTES", "5"))

MOCK_MODE = os.getenv("MOCK", "0") == "1"
OPENAI_STUB_IN_MOCK = os.getenv("OPENAI_STUB_IN_MOCK", "1") == "1"
ID_TIME_SERVICOS = "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ============ TEMPO/FORMATOS ============

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ds_time(dt: datetime) -> str:
    """'YYYY-MM-DD HH:MM:SS' (UTC, sem TZ)"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt_loose(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if not dt.tzinfo:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def within_last_minutes(created_at: str, minutes: int = 5) -> bool:
    dt = parse_dt_loose(created_at)
    return bool(dt) and (now_utc() - dt) <= timedelta(minutes=minutes)


# ============ Mock Agidesk ============

def load_mock_issues_from_file() -> List[Dict[str, Any]]:
    path = os.path.join(os.path.dirname(__file__), "mock_ticket.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    raise RuntimeError("mock_ticket.json deve ser objeto ou lista de objetos")


class MockAgideskClient:
    def __init__(self, mock_issues: List[Dict[str, Any]]):
        if not mock_issues:
            raise RuntimeError("Mock vazio.")
        self._issues = [Ticket.model_validate(issue) for issue in mock_issues]

    def search_tickets(self, **kwargs) -> List[Ticket]:
        return self._issues

    def get_issue(self, issue_id: str) -> Optional[Ticket]:
        for it in self._issues:
            if str(it.id) == str(issue_id):
                return it
        return self._issues[0] if self._issues else None

    def update_issue(self, issue_id: str, update_payload: Dict[str, Any]) -> Dict[str, Any]:
        for it in self._issues:
            if str(it.id) == str(issue_id):
                # Mock update logic is complex, returning simple success
                return {"ok": True, "service": update_payload.get("service", {})}
        return {"ok": True, "service": update_payload.get("service", {})}

# ============ OpenAI (Responses API com Structured Outputs) ============

def openai_ticket_schema_block() -> dict:
    """
    Responses API + strict: True
    - 'type' = "json_schema"
    - 'name' e 'strict' no mesmo nível
    - 'schema' com additionalProperties=False em TODO objeto
    - 'required' deve listar TODAS as chaves de 'properties' para cada objeto
    """
    return {
        "type": "json_schema",
        "name": "ticket_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "version": {"type": "string"},
                "ticket_id": {"type": "string"},
                "summary_for_teams": {"type": "string"},
                "problem": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "summary": {"type": "string"},
                        "explanation": {"type": "string"},
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "hypothesis": {"type": "string"},
                                    "why_chain_of_events": {"type": "string"},
                                    "fix_ideas": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                "required": ["hypothesis", "why_chain_of_events", "fix_ideas"]
                            }
                        }
                    },
                    "required": ["summary", "explanation", "candidates"]
                },
                "remediation": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": "string"},
                                    "owner_team": {"type": "string"},
                                    "sla_minutes": {"type": "number"},
                                    "steps": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    }
                                },
                                # strict exige TODAS as chaves de properties em required
                                "required": ["title", "owner_team", "sla_minutes", "steps"]
                            }
                        }
                    },
                    "required": ["tasks"]
                },
                "debug": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "clarifying_questions": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "signals_to_collect": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    # strict exige TODAS as chaves declaradas
                    "required": ["clarifying_questions", "signals_to_collect"]
                },
                "references": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            # strict exige TODAS as chaves top-level
            "required": [
                "version",
                "ticket_id",
                "summary_for_teams",
                "problem",
                "remediation",
                "debug",
                "references"
            ]
        }
    }


def build_responses_input_from_issue(issue: Ticket) -> List[Dict[str, Any]]:
    """
    Monta 'input' no formato recomendado pelo Responses API:
    lista de mensagens, cada uma com 'content' = [{ "type": "input_text", "text": "..." }]
    """
    system_text = (
        "Você é um engenheiro sênior de suporte e confiabilidade. "
        "Retorne APENAS JSON aderente ao schema (strict)."
    )
    # Sanitiza: remove HTML gigante e limita tamanho do payload
    issue_dict = issue.model_dump(exclude_none=True)
    issue_dict.pop("htmlcontent", None)
    raw_issue = json.dumps(issue_dict, ensure_ascii=False)
    if len(raw_issue) > 120000:
        raw_issue = raw_issue[:120000] + "\n... [TRUNCADO PELO ROUTER]"

    return [
        {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
        {"role": "user", "content": [{"type": "input_text", "text": raw_issue}]},
    ]


def call_openai_structured(issue: Ticket) -> Dict[str, Any]:
    """
    Chama /v1/responses com:
      - input: mensagens (input_text)
      - text.format: json_schema (strict: True)
    Em mock com stub habilitado (default) ou sem API key, retorna um stub local.
    """
    if MOCK_MODE and (OPENAI_STUB_IN_MOCK or not OPENAI_API_KEY):
        # Stub para testes offline (sem HTTP)
        ticket_id = str(issue.id)
        title = issue.title
        return {
            "version": "stub-1",
            "ticket_id": ticket_id,
            "summary_for_teams": f"[MOCK] Resumo técnico para '{title}'.",
            "problem": {
                "summary": "Stub de análise.",
                "explanation": "Conteúdo simulado em modo mock.",
                "candidates": [
                    {
                        "hypothesis": "Hipótese de exemplo",
                        "why_chain_of_events": "Sequência simulada.",
                        "fix_ideas": ["Checar driver/firmware", "Isolar política", "Testar cenário A/B"]
                    }
                ]
            },
            "remediation": {
                "tasks": [
                    {"title": "Coletar logs", "owner_team": "Suporte", "sla_minutes": 30, "steps": ["Event Viewer", "Syslog/WLC"]}
                ]
            },
            "debug": {
                "clarifying_questions": ["Qual versão exata do driver?"],
                "signals_to_collect": ["Event IDs", "Counters AP/WLC"]
            },
            "references": []
        }

    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    payload = {
        "model": OPENAI_MODEL,
        "input": build_responses_input_from_issue(issue),
        # Structured Outputs (forma atual): enviar schema via text.format
        "text": {"format": openai_ticket_schema_block()},
        # Observação: alguns modelos também aceitam "response_format": {...} (legacy).
    }

    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code != 200:
        # Mostra a mensagem detalhada de erro da API (útil para 400)
        try:
            print("[OPENAI ERR]", r.status_code, r.json())
        except Exception:
            print("[OPENAI ERR]", r.status_code, r.text[:1200])
        r.raise_for_status()

    data = r.json()
    # Estrutura Responses: output -> [ { content: [ { type:"output_text", text:"{...}" } ] } ]
    output_list = data.get("output") or []
    if not output_list:
        raise RuntimeError("Resposta da OpenAI sem 'output'.")
    content = output_list[0].get("content") or []
    if not content or not isinstance(content, list) or not content[0].get("text"):
        raise RuntimeError("Resposta da OpenAI sem 'content[0].text'.")
    return json.loads(content[0]["text"])


# ============ Teams Webhook ============

def post_to_teams(markdown_text: str) -> tuple[bool, str]:
    """
    Envia 'markdown_text' ao Teams Incoming Webhook.
    Retorna (ok, info):
      - ok=True  -> sucesso
      - ok=False -> falha (info descreve o motivo)
    Faz 2 tentativas: payload simples {"text": "..."} e MessageCard (Office 365 Connector).
    """
    if not TEAMS_WEBHOOK_URL:
        return (False, "TEAMS_WEBHOOK_URL não configurada")

    # Limites do Teams costumam ser ~28KB; vamos truncar por segurança
    TEAMS_MAX_BYTES = 25000
    text = markdown_text
    if len(text.encode("utf-8")) > TEAMS_MAX_BYTES:
        text = text.encode("utf-8")[:TEAMS_MAX_BYTES - 20].decode("utf-8", errors="ignore") + "\n\n…(truncado)"

    verify_tls = not (os.getenv("TEAMS_INSECURE", "0") == "1")
    timeout = 30

    # 1) payload simples
    variants = [
        ("simple", {"text": text}),
        # 2) MessageCard (alguns webhooks exigem esse formato)
        ("card", {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": "Router AI",
            "themeColor": "0076D7",
            "title": (text.splitlines()[0][:120] if text else "Router AI"),
            "text": text
        }),
    ]

    for kind, payload in variants:
        try:
            r = requests.post(
                TEAMS_WEBHOOK_URL,
                json=payload,
                timeout=timeout,
                verify=verify_tls,
                headers={"Content-Type": "application/json"}
            )
        except requests.RequestException as e:
            # falha de rede/timeout
            return (False, f"[{kind}] Falha de conexão/timeout: {e}")

        body = (r.text or "").strip()
        # Em geral sucesso é 200; alguns retornam "1" no body, outros vazio/OK
        if 200 <= r.status_code < 300:
            return (True, f"[{kind}] status={r.status_code} body={body[:200] or '(vazio)'}")

        # Se não deu certo, log detalhado e tenta a próxima variante
        print(f"[Teams][{kind}] ERRO status={r.status_code}")
        try:
            print(f"[Teams][{kind}] headers={dict(r.headers)}")
        except Exception:
            pass
        print(f"[Teams][{kind}] body={body[:800]}")

    # Se ambas variantes falharam:
    return (False, "Todas as variantes de payload falharam (ver logs acima).")


# ============ Renderizações (blog + formulário) ============

def render_blog(ai: Dict[str, Any], issue: Ticket) -> str:
    md: List[str] = []
    md.append(f"### Ticket {ai.get('ticket_id','?')} — Análise Técnica")
    md.append(f"**Resumo:** {ai.get('summary_for_teams','')}")
    md.append("")
    md.append("#### Contexto")
    md.append(f"- Título: {issue.title}")
    md.append(f"- Criado em: {issue.created_at}")
    issue_dict = issue.model_dump()
    if issue_dict.get("source"):
        md.append(f"- Origem: {issue_dict.get('source')}")
    md.append("")
    prob = ai.get("problem", {}) or {}
    md.append("#### Explicação")
    md.append(prob.get("explanation", ""))
    md.append("")
    md.append("#### Hipóteses (cadeia de eventos) + Ideias de correção")
    for c in prob.get("candidates", []) or []:
        md.append(f"- **Hipótese:** {c.get('hypothesis','')}")
        md.append(f"  - Cadeia: {c.get('why_chain_of_events','')}")
        for idea in c.get("fix_ideas", []) or []:
            md.append(f"  - Correção: {idea}")
    md.append("")
    md.append("#### Procedimentos de Diagnóstico / Correção (ETA)")
    for t in ai.get("remediation", {}).get("tasks", []) or []:
        line = f"- {t.get('title','')} — ETA {t.get('sla_minutes','?')} min"
        if t.get("owner_team"):
            line += f" (time: {t['owner_team']})"
        md.append(line)
        for step in t.get("steps", []) or []:
            md.append(f"  - {step}")
    refs = ai.get("references", []) or []
    if refs:
        md.append("")
        md.append("#### Referências")
        for u in refs:
            md.append(f"- {u}")
    return "\n".join(md)


def render_questions_form(ai: Dict[str, Any]) -> str:
    qs = ai.get("debug", {}).get("clarifying_questions", []) or []
    if not qs:
        return "- (sem perguntas)"
    return "\n".join([f"- [ ] {q}" for q in qs])


# ============ Payload de update (Agidesk) ============

def build_agidesk_update_payload(ai: Dict[str, Any], blog_md: str, form_md: str) -> Dict[str, Any]:
    tasks = ai.get("remediation", {}).get("tasks", []) or []
    tasks_md = "\n".join(
        [f"- [ ] {t.get('title','')} — ETA {t.get('sla_minutes','?')} min" for t in tasks]
    ) or "- (sem tarefas)"
    problem_expl = (ai.get("problem", {}) or {}).get("explanation", "")

    actiondescription = (
        "[ROUTER-AI] Este bloco foi gerado automaticamente pelo router de análise.\n\n"
        "### Resumo técnico\n"
        f"{problem_expl}\n\n"
        "### Perguntas de confirmação\n"
        f"{form_md}\n\n"
        "### Procedimentos sugeridos (checklist)\n"
        f"{tasks_md}\n"
    )
    return {
        "service": {
            "actiondescription": actiondescription,
            "tag": ROUTER_PROCESSED_TAG  # remova esta linha se não quiser tag visível no painel
        }
    }


# ============ Filtros ============

def pass_filters(issue: Ticket) -> bool:
    # no modo real, exigimos janela de 5 min; no mock, liberamos para facilitar testes
    if not MOCK_MODE and not within_last_minutes(issue.created_at or "", FETCH_TIME_MINUTES):
        return False
    if str(issue.team_id) != ID_TIME_SERVICOS:
        return False
    if (issue.type or "").strip() not in {"Incidente", "Requisição"}:
        return False
    # A verificação de tag precisa de um campo 'tag' no modelo Ticket
    # if isinstance(issue.tag, str) and issue.tag.strip().lower() == ROUTER_PROCESSED_TAG.lower():
    #     return False
    return True


# ============ Pipeline de 1 ticket ============

def process_issue(agi_client, issue: Ticket) -> Dict[str, Any]:
    issue_id = str(issue.id)
    try:
        full_issue = agi_client.get_issue(issue_id)
    except Exception:
        full_issue = None

    issue_for_ai = full_issue if full_issue else issue

    ai = call_openai_structured(issue_for_ai)
    blog_md = render_blog(ai, issue)
    form_md = render_questions_form(ai)

    sent, info = post_to_teams(blog_md)
    if sent:
        print("[Teams] ✅", info)
    else:
        print("[Teams] ❌", info)


    update_payload = build_agidesk_update_payload(ai, blog_md, form_md)

    try:
        update_resp = agi_client.update_issue(issue_id, update_payload)
    except Exception as e:
        update_resp = {"error": str(e), "service": update_payload.get("service")}

    return {
        "issue_id": issue_id,
        "ai_structured": ai,
        "update_payload": update_payload,
        "agidesk_update_response": update_resp
    }


# ============ MAIN ============

def main() -> None:
    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL"]
    if not MOCK_MODE:
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
            return

    logging.info(f"--- Starting Ticket Canary (MOCK_MODE={MOCK_MODE}) ---")

    if MOCK_MODE:
        try:
            mock_issues = load_mock_issues_from_file()
        except Exception as e:
            raise SystemExit(f"[ERRO] Falha lendo mock_ticket.json: {e}")
        agi = MockAgideskClient(mock_issues)
        issues = agi.search_tickets()
        selected = [i for i in issues if pass_filters(i)]
        if not selected and issues:
            selected = [issues[0]]

        for i in selected:
            result = process_issue(agi, i)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # modo real: polling
    agi = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    while True:
        try:
            start_time = now_utc()
            initial_date = start_time - timedelta(minutes=FETCH_TIME_MINUTES)
            initial_date_str = ds_time(initial_date)

            issues = agi.search_tickets(
                periodfield='created_at',
                initialdate=initial_date_str,
                team=[ID_TIME_SERVICOS]
            )
            logging.info(f"Found {len(issues)} tickets.")

            selected = [i for i in issues if pass_filters(i)]
            for i in selected:
                result = process_issue(agi, i)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            logging.error(f"An error occurred: {e}")
        
        logging.info(f"Finished check. Waiting for {POLL_INTERVAL_SEC} seconds...")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
