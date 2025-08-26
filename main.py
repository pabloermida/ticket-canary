#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Router de Polling (Agidesk -> OpenAI Responses API -> Teams -> Agidesk)

Pontos principais:
- OpenAI: usa /v1/responses com input no formato de mensagens (content.type="input_text")
  e Structured Outputs via text.format + json_schema (strict=True).
- Mock: lê mock_ticket.json (lista ou objeto). Dá para usar OpenAI real no mock
  (defina OPENAI_STUB_IN_MOCK=0 e OPENAI_API_KEY).
- Agidesk: usa apenas os endpoints/campos confirmados por você:
    GET  /api/v1/issues?per_page=1000&page=1&periodfield=created_at&initialdate=...&finaldate=...
    GET  /api/v1/issues/{id}
    PUT  /api/v1/issues/{id} -> payload {"service":{"actiondescription":"...", "tag":"router:processed"}}
- Filtros em memória: team_id == "1", type in {"Incidente","Requisição"}, sem tag router:processed,
  e (no modo real) created_at nos últimos 5 minutos.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

import requests

# (opcional) carregar .env se existir, sem quebrar caso lib não esteja instalada
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ============ VARS / CONFIG ============

AGIDESK_BASE_URL = os.getenv("AGIDESK_BASE_URL", "").rstrip("/")  # ex: https://SEU_TENANT.agidesk.com
AGIDESK_API_TOKEN = os.getenv("AGIDESK_API_TOKEN", "")

TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")  # Incoming Webhook do Teams

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")  # modelos compatíveis com Responses API

ROUTER_PROCESSED_TAG = os.getenv("ROUTER_PROCESSED_TAG", "router:processed")

POLL_INTERVAL_SEC = 300

MOCK_MODE = os.getenv("MOCK", "0") == "1"
# por padrão, em mock NÃO chamamos HTTP da OpenAI; ajuste para 0 se quiser usar OpenAI real no mock
OPENAI_STUB_IN_MOCK = os.getenv("OPENAI_STUB_IN_MOCK", "1") == "1"


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


# ============ HTTP helpers (uso no modo real) ============

def http_get_json(url: str, headers: Dict[str, str], params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def http_put_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.put(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json() if r.text.strip() else {}


# ============ Agidesk Client (somente rotas e params confirmados) ============

class AgideskClient:
    """
    GET  /api/v1/issues
         params: per_page, page, periodfield, initialdate, finaldate
    GET  /api/v1/issues/{id}
    PUT  /api/v1/issues/{id}  (payload: {"service":{"actiondescription": "...", "tag": ROUTER_PROCESSED_TAG}})
    """
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def list_recent_issues(self) -> List[Dict[str, Any]]:
        end = now_utc()
        start = end - timedelta(minutes=5)
        params = {
            "per_page": "1000",
            "page": "1",
            "periodfield": "created_at",
            "initialdate": ds_time(start),
            "finaldate": ds_time(end),
        }
        url = f"{self.base_url}/api/v1/issues"
        data = http_get_json(url, headers=self._headers(), params=params)

        raw = data.get("data", data)
        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
            raw = raw[0]
        items = [x for x in raw if isinstance(x, dict)]
        return items

    def get_issue(self, issue_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v1/issues/{issue_id}"
        return http_get_json(url, headers=self._headers(), params={})

    def update_issue(self, issue_id: str, update_payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/v1/issues/{issue_id}"
        return http_put_json(url, headers=self._headers(), payload=update_payload)


# ============ Mock Agidesk (aceita lista) ============

def load_mock_issues_from_file() -> List[Dict[str, Any]]:
    """
    Lê mock_ticket.json na mesma pasta:
    - se for lista: retorna lista filtrando apenas dicts
    - se for dict: embrulha em lista
    """
    path = os.path.join(os.path.dirname(__file__), "mock_ticket.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    raise RuntimeError("mock_ticket.json deve ser objeto ou lista de objetos")


class MockAgideskClient(AgideskClient):
    def __init__(self, base_url: str, token: str, mock_issues: List[Dict[str, Any]]):
        super().__init__(base_url, token)
        if not mock_issues:
            raise RuntimeError("Mock vazio.")
        self._issues = mock_issues

    def list_recent_issues(self) -> List[Dict[str, Any]]:
        return self._issues

    def get_issue(self, issue_id: str) -> Dict[str, Any]:
        for it in self._issues:
            if str(it.get("id")) == str(issue_id):
                return it
        return self._issues[0]

    def update_issue(self, issue_id: str, update_payload: Dict[str, Any]) -> Dict[str, Any]:
        for it in self._issues:
            if str(it.get("id")) == str(issue_id):
                it.setdefault("service_update", {}).update(update_payload.get("service", {}))
                return {"ok": True, "service": it["service_update"]}
        self._issues[0].setdefault("service_update", {}).update(update_payload.get("service", {}))
        return {"ok": True, "service": self._issues[0]["service_update"]}


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




def build_responses_input_from_issue(issue: dict) -> List[Dict[str, Any]]:
    """
    Monta 'input' no formato recomendado pelo Responses API:
    lista de mensagens, cada uma com 'content' = [{ "type": "input_text", "text": "..." }]
    """
    system_text = (
        "Você é um engenheiro sênior de suporte e confiabilidade. "
        "Retorne APENAS JSON aderente ao schema (strict)."
    )
    # Sanitiza: remove HTML gigante e limita tamanho do payload
    issue_copy = dict(issue)
    issue_copy.pop("htmlcontent", None)
    raw_issue = json.dumps(issue_copy, ensure_ascii=False)
    if len(raw_issue) > 120000:
        raw_issue = raw_issue[:120000] + "\n... [TRUNCADO PELO ROUTER]"

    return [
        {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
        {"role": "user", "content": [{"type": "input_text", "text": raw_issue}]},
    ]


def call_openai_structured(issue: Dict[str, Any]) -> Dict[str, Any]:
    """
    Chama /v1/responses com:
      - input: mensagens (input_text)
      - text.format: json_schema (strict: True)
    Em mock com stub habilitado (default) ou sem API key, retorna um stub local.
    """
    if MOCK_MODE and (OPENAI_STUB_IN_MOCK or not OPENAI_API_KEY):
        # Stub para testes offline (sem HTTP)
        ticket_id = str(issue.get("id", "mock"))
        title = issue.get("title", "(sem título)")
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

def render_blog(ai: Dict[str, Any], issue: Dict[str, Any]) -> str:
    md: List[str] = []
    md.append(f"### Ticket {ai.get('ticket_id','?')} — Análise Técnica")
    md.append(f"**Resumo:** {ai.get('summary_for_teams','')}")
    md.append("")
    md.append("#### Contexto")
    md.append(f"- Título: {issue.get('title')}")
    md.append(f"- Criado em: {issue.get('created_at')}")
    if issue.get("source"):
        md.append(f"- Origem: {issue.get('source')}")
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

def pass_filters(issue: Dict[str, Any]) -> bool:
    if not isinstance(issue, dict):
        return False
    # no modo real, exigimos janela de 5 min; no mock, liberamos para facilitar testes
    if not MOCK_MODE and not within_last_minutes(issue.get("created_at", ""), 5):
        return False
    if str(issue.get("team_id")) != "1":
        return False
    if (issue.get("type") or "").strip() not in {"Incidente", "Requisição"}:
        return False
    tag_txt = issue.get("tag")
    if isinstance(tag_txt, str) and tag_txt.strip().lower() == ROUTER_PROCESSED_TAG.lower():
        return False
    return True


# ============ Pipeline de 1 ticket ============

def process_issue(agi_client, issue: Dict[str, Any]) -> Dict[str, Any]:
    issue_id = str(issue.get("id", "mock"))
    try:
        full = agi_client.get_issue(issue_id)
    except Exception:
        full = {}

    issue_for_ai = issue.copy()
    issue_for_ai["details"] = full

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
    print("MOCK_MODE =", MOCK_MODE)

    if MOCK_MODE:
        try:
            mock_issues = load_mock_issues_from_file()
        except Exception as e:
            raise SystemExit(f"[ERRO] Falha lendo mock_ticket.json: {e}")

        agi = MockAgideskClient(AGIDESK_BASE_URL, AGIDESK_API_TOKEN, mock_issues)
        issues = agi.list_recent_issues()
        selected = [i for i in issues if isinstance(i, dict) and pass_filters(i)]
        if not selected and issues:
            selected = [issues[0]]  # processa ao menos 1 no mock

        for i in selected:
            result = process_issue(agi, i)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # modo real: polling
    agi = AgideskClient(AGIDESK_BASE_URL, AGIDESK_API_TOKEN)
    while True:
        try:
            issues = agi.list_recent_issues()
            selected = [i for i in issues if isinstance(i, dict) and pass_filters(i)]
            for i in selected:
                result = process_issue(agi, i)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            print("[ERROR]", e)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
