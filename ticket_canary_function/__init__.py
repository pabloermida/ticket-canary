import os
import json
import logging
import re
import html
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from textwrap import shorten

import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient

# Import the shared module from the app root. Relative import would fail
# because agidesk.py lives at the repository root, not inside this package.
from agidesk import AgideskAPI, Ticket

# Configuration
AGIDESK_ACCOUNT_ID = os.getenv("AGIDESK_ACCOUNT_ID", "")
AGIDESK_APP_KEY = os.getenv("AGIDESK_APP_KEY", "")
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
FETCH_TIME_SECONDS = int(os.getenv("FETCH_TIME_SECONDS", "300"))
MODE = os.getenv("MODE", "development")
ID_BOARD_SERVICOS = "9"
PROCESSED_IDS_BLOB_NAME = "processed_ids.json"
# Optional: Build a direct ticket URL for card actions
# Default to the known Agidesk customer portal pattern; override via env if needed
AGIDESK_TICKET_URL_TEMPLATE = os.getenv(
    "AGIDESK_TICKET_URL_TEMPLATE",
    "https://cliente.infiniit.com.br/br/painel/atendimento/{id}",
)
# Prefer explicit app setting, but fall back to the platform default setting
AZURE_STORAGE_CONNECTION_STRING = (
    os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    or os.getenv("AzureWebJobsStorage")
)
CONTAINER_NAME = "ticket-canary-state"


def get_blob_client(blob_name: str) -> BlobClient:
    if not AZURE_STORAGE_CONNECTION_STRING:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING is not set.")
    
    blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
    
    try:
        container_client = blob_service_client.get_container_client(CONTAINER_NAME)
        container_client.create_container()
    except Exception:
        pass

    return container_client.get_blob_client(blob_name)


def load_processed_ids() -> set[str]:
    """Load processed ticket IDs from Azure Blob Storage."""
    try:
        blob_client = get_blob_client(PROCESSED_IDS_BLOB_NAME)
        if blob_client.exists():
            downloader = blob_client.download_blob(max_concurrency=1, encoding='UTF-8')
            blob_content = downloader.readall()
            return set(json.loads(blob_content))
    except Exception as e:
        logging.warning(f"Could not read processed IDs file from blob storage: {e}")
    
    return set()


def save_processed_ids(processed_ids: set[str]):
    """Save the set of processed ticket IDs to Azure Blob Storage."""
    try:
        blob_client = get_blob_client(PROCESSED_IDS_BLOB_NAME)
        blob_client.upload_blob(json.dumps(list(processed_ids)), overwrite=True, encoding='UTF-8')
    except Exception as e:
        logging.error(f"Failed to save processed IDs to blob storage: {e}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ds_time(dt: datetime) -> str:
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


def within_last_seconds(created_at: str, seconds: int = 300) -> bool:
    dt = parse_dt_loose(created_at)
    return bool(dt) and (now_utc() - dt) <= timedelta(seconds=seconds)


def call_openai_simplified(ticket: Ticket) -> Dict[str, Any]:
    """Call OpenAI (Responses API) to summarize a ticket with optional image context.

    - Model from `OPENAI_MODEL` (default: gpt-5)
    - text.format = json_schema to get a structured JSON payload
    - HTTPS images are attached as `input_image`; non-HTTPS URLs are listed inline in text
    """

    def extract_image_urls(html_content: Optional[str]) -> List[str]:
        if not html_content:
            return []
        urls: List[str] = []
        try:
            urls.extend(re.findall(r"<img[^>]+src=\"([^\"]+)\"", html_content, flags=re.IGNORECASE))
            urls.extend(re.findall(r"<img[^>]+src='([^']+)'", html_content, flags=re.IGNORECASE))
        except re.error:
            pass
        return [u for u in urls if u.startswith("http://") or u.startswith("https://")]

    system_text = (
        "Você é um Analista de Suporte N2 dos clientes da Infraestrutura da Infiniit (infiniit.com.br). "
        "Leia atentamente os dados do ticket e responda SOMENTE com um objeto JSON contendo: "
        "'resumo_problema' (string), 'sugestao_solucao' (string) e 'sugestao_solucao_lista' (array de strings). "
        "Sempre inclua o array; quando não houver itens, retorne []. Priorize uma lista prática, com 8–15 itens claros e acionáveis. "
        "Regras e escopo: 1) Infraestrutura (redes, Windows/Linux Server, VMware, backup/Veeam, firewalls, Azure/M365, monitoramento, segurança). "
        "2) Próximas ações N2 (hipóteses, comandos/verificações, logs, validações). "
        "3) Cite recursos públicos quando pertinente (nome e link); não invente fontes. "
        "4) Considere imagens como evidência auxiliar. 5) Linguagem objetiva em PT-BR. "
        "6) Se a entrada for incompatível com análise de infraestrutura, retorne 'resumo_problema' vazio e 'sugestao_solucao' = 'Entrada incompatível com análise de infraestrutura.'. "
        "7) Saída estritamente em JSON válido, sem texto extra."
    )

    user_text = f"Título: {ticket.title}\nConteúdo: {ticket.content}"
    raw_image_urls = extract_image_urls(getattr(ticket, "htmlcontent", None))
    https_images: List[str] = [u for u in raw_image_urls if u.startswith("https://")]
    non_https_images: List[str] = [u for u in raw_image_urls if not u.startswith("https://")]
    if non_https_images:
        user_text += "\n\nImagens (URLs):\n" + "\n".join(non_https_images)

    parts: List[Dict[str, Any]] = [{"type": "input_text", "text": user_text}]
    for url in https_images:
        parts.append({"type": "input_image", "image_url": url})

    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    def _dbg_enabled() -> bool:
        return os.getenv("OPENAI_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

    try:
        token_limit = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "1200"))
    except Exception:
        token_limit = 1200

    text_spec_json_schema = {
        "format": {
            "type": "json_schema",
            "name": "TicketInfraSummary",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "resumo_problema": {"type": "string"},
                    "sugestao_solucao": {"type": "string"},
                    "sugestao_solucao_lista": {"type": "array", "minItems": 5, "items": {"type": "string"}},
                },
                "required": [
                    "resumo_problema",
                    "sugestao_solucao",
                    "sugestao_solucao_lista"
                ],
            },
        }
    }

    def _safe_excerpt(obj: Any, n: int = 4000) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False)[:n]
        except Exception:
            try:
                return str(obj)[:n]
            except Exception:
                return ""

    def _extract_structured_from_text(txt: str) -> Optional[Dict[str, Any]]:
        # Try strict JSON first
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        # Heuristic extraction: pull known fields with regex
        try:
            rp = None
            ss = None
            lst = None
            lst_ord = None
            m = re.search(r'"resumo_problema"\s*:\s*"(.*?)"', txt, re.DOTALL)
            if m:
                rp = m.group(1).strip()
            m = re.search(r'"sugestao_solucao"\s*:\s*"(.*?)"', txt, re.DOTALL)
            if m:
                ss = m.group(1).strip()
            m = re.search(r'"sugestao_solucao_lista"\s*:\s*\[(.*?)\]', txt, re.DOTALL)
            if m:
                arr_text = '[' + m.group(1) + ']'
                try:
                    lst = json.loads(arr_text)
                except Exception:
                    lst = None
            if any(v is not None for v in (rp, ss, lst)):
                return {
                    "resumo_problema": rp or "",
                    "sugestao_solucao": ss or "",
                    "sugestao_solucao_lista": lst or [],
                }
        except Exception:
            pass
        return None

    def post_and_parse(payload: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        if _dbg_enabled() and r.status_code >= 400:
            try:
                logging.error(f"OpenAI error body: {r.text}")
            except Exception:
                pass
        r.raise_for_status()
        data = r.json()
        out = data.get("output")
        if isinstance(out, list):
            for item in out:
                if isinstance(item, dict) and item.get("type") == "message":
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                                txt = part.get("text", "{}")
                                try:
                                    return json.loads(txt)
                                except Exception:
                                    return {"resumo_problema": "Error: Could not parse AI JSON.", "sugestao_solucao": "N/A"}
        if isinstance(data.get("output_text"), str):
            try:
                return json.loads(data["output_text"])  # type: ignore[index]
            except Exception:
                return {"resumo_problema": "Error: Could not parse AI JSON.", "sugestao_solucao": "N/A"}
        return {"resumo_problema": "Error: No AI content.", "sugestao_solucao": "N/A"}

    def _extract_bullets_from_text(txt: str, max_items: int = 20) -> list[str]:
        items: list[str] = []
        try:
            for raw in txt.splitlines():
                line = raw.strip()
                m = re.match(r"^(?:[-*•–—]\s+|\d+[\.)]\s+)(.+)$", line)
                if m:
                    val = m.group(1).strip()
                    if val and val not in items:
                        items.append(val)
                        if len(items) >= max_items:
                            break
        except Exception:
            pass
        return items

    def post_and_text(payload: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        if _dbg_enabled() and r.status_code >= 400:
            try:
                logging.error(f"OpenAI error body: {r.text}")
            except Exception:
                pass
        r.raise_for_status()
        data = r.json()
        # Try Responses shapes for raw text
        out = data.get("output")
        if isinstance(out, list):
            for item in out:
                if isinstance(item, dict) and item.get("type") == "message":
                    content = item.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                                txt = str(part.get("text") or "").strip()
                                # Try to parse JSON if the model still produced it
                                structured = _extract_structured_from_text(txt)
                                if isinstance(structured, dict):
                                    return {
                                        **structured,
                                        "sugestao_solucao_lista": structured.get("sugestao_solucao_lista") or [],
                                    }
                                bullets = _extract_bullets_from_text(txt)
                                return {"resumo_problema": txt or "", "sugestao_solucao": "", "sugestao_solucao_lista": bullets}
        if isinstance(data.get("output_text"), str):
            txt = data.get("output_text", "")
            structured = _extract_structured_from_text(txt)
            if isinstance(structured, dict):
                return {
                    **structured,
                    "sugestao_solucao_lista": structured.get("sugestao_solucao_lista") or [],
                }
            bullets = _extract_bullets_from_text(txt)
            return {"resumo_problema": txt or "", "sugestao_solucao": "", "sugestao_solucao_lista": bullets}
        return {"resumo_problema": "", "sugestao_solucao": "", "sugestao_solucao_lista": []}

    def _valid_structured(ai: Dict[str, Any]) -> bool:
        try:
            rp = str(ai.get("resumo_problema", "")).strip()
            ss = str(ai.get("sugestao_solucao", "")).strip()
            if not rp:
                return False
            if rp.startswith("Error:"):
                return False
            return True
        except Exception:
            return False

    # Primary attempt
    payload = {
        "model": OPENAI_MODEL,
        "text": text_spec_json_schema,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
            {"role": "user", "content": parts},
        ],
        "max_output_tokens": token_limit,
    }
    try:
        result = post_and_parse(payload)
        if _dbg_enabled():
            # Attach non-sensitive request/response metadata
            meta = {
                "path": "schema",
                "model": OPENAI_MODEL,
                "format": "json_schema",
                "token_param": "max_output_tokens",
                "token_limit": token_limit,
                "user_text_parts": 1,
                "user_image_parts": len(https_images),
                "images_https": len(https_images),
                "images_non_https": len(non_https_images),
            }
            result.setdefault("debug_meta", meta)
        if _valid_structured(result):
            return result
        # Fallback to plain text format if structured result is empty/invalid
        is_gpt5 = (OPENAI_MODEL or "").strip().lower().startswith("gpt-5")
        payload_text = {
            "model": OPENAI_MODEL,
            "text": {"format": {"type": "text"}, "verbosity": "high"},
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
                {"role": "user", "content": parts},
            ],
            "max_output_tokens": token_limit,
            "tool_choice": "none",
        }
        if is_gpt5:
            payload_text["reasoning"] = {"effort": "low"}
        text_result = post_and_text(payload_text)
        if _dbg_enabled():
            meta = {
                "path": "text_fallback",
                "model": OPENAI_MODEL,
                "format": "text",
                "token_param": "max_output_tokens",
                "token_limit": token_limit,
                "user_text_parts": 1,
                "user_image_parts": len(https_images),
                "images_https": len(https_images),
                "images_non_https": len(non_https_images),
            }
            text_result.setdefault("debug_meta", meta)
        return text_result
    except requests.HTTPError as e:
        body = getattr(getattr(e, 'response', None), 'text', '') or str(e)
        if "max_completion_tokens" in body:
            payload2 = dict(payload)
            payload2.pop("max_output_tokens", None)
            payload2["max_completion_tokens"] = token_limit
            result = post_and_parse(payload2)
            if _dbg_enabled():
                meta = {
                    "path": "schema_compat_token",
                    "model": OPENAI_MODEL,
                    "format": "json_schema",
                    "token_param": "max_completion_tokens",
                    "token_limit": token_limit,
                    "user_text_parts": 1,
                    "user_image_parts": len(https_images),
                    "images_https": len(https_images),
                    "images_non_https": len(non_https_images),
                }
                result.setdefault("debug_meta", meta)
            if _valid_structured(result):
                return result
            payload_text2 = {
                "model": OPENAI_MODEL,
                "text": {"format": {"type": "text"}, "verbosity": "high"},
                "input": [
                    {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
                    {"role": "user", "content": parts},
                ],
                "max_completion_tokens": token_limit,
                "tool_choice": "none",
            }
            if is_gpt5:
                payload_text2["reasoning"] = {"effort": "low"}
            text_result2 = post_and_text(payload_text2)
            if _dbg_enabled():
                meta = {
                    "path": "text_fallback_compat_token",
                    "model": OPENAI_MODEL,
                    "format": "text",
                    "token_param": "max_completion_tokens",
                    "token_limit": token_limit,
                    "user_text_parts": 1,
                    "user_image_parts": len(https_images),
                    "images_https": len(https_images),
                    "images_non_https": len(non_https_images),
                }
                text_result2.setdefault("debug_meta", meta)
                text_result2["debug_response_excerpt"] = _safe_excerpt(body)
            return text_result2
        # Generic fallback to text on other HTTP errors
        payload_text3 = {
            "model": OPENAI_MODEL,
            "text": {"format": {"type": "text"}, "verbosity": "high"},
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_text}]},
                {"role": "user", "content": parts},
            ],
            "max_output_tokens": token_limit,
            "tool_choice": "none",
        }
        if is_gpt5:
            payload_text3["reasoning"] = {"effort": "low"}
        text_result3 = post_and_text(payload_text3)
        if _dbg_enabled():
            meta = {
                "path": "text_fallback_http_error",
                "model": OPENAI_MODEL,
                "format": "text",
                "token_param": "max_output_tokens",
                "token_limit": token_limit,
                "user_text_parts": 1,
                "user_image_parts": len(https_images),
                "images_https": len(https_images),
                "images_non_https": len(non_https_images),
            }
            text_result3.setdefault("debug_meta", meta)
            text_result3["debug_response_excerpt"] = _safe_excerpt(body)
        return text_result3
    except Exception:
        pass
    return {"resumo_problema": "Error: Could not process AI response.", "sugestao_solucao": "N/A"}


def notify_teams(message: str) -> bool:
    if not TEAMS_WEBHOOK_URL:
        logging.error("TEAMS_WEBHOOK_URL is not configured.")
        return False
    payload = {"text": message}
    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=30)
        if response.status_code == 200:
            logging.info("Message sent to Teams successfully.")
            return True
        else:
            logging.error(f"Error sending to Teams: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        logging.error(f"Connection error with Teams: {e}")
        return False


def notify_teams_adaptive(card: Dict[str, Any], fallback_message: Optional[str] = None) -> bool:
    """Send an Adaptive Card to Teams via Incoming Webhook.

    Teams webhooks accept Adaptive Cards wrapped as an attachment.
    Falls back to sending plain text if posting the card fails.
    """
    if not TEAMS_WEBHOOK_URL:
        logging.error("TEAMS_WEBHOOK_URL is not configured.")
        return False

    wrapper = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }

    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=wrapper, timeout=30)
        if response.status_code == 200:
            logging.info("Adaptive Card sent to Teams successfully.")
            return True
        else:
            logging.error(f"Error sending Adaptive Card to Teams: {response.status_code} - {response.text}")
            # Fallback to a simple text notification with the card's title, if available
            if fallback_message:
                return notify_teams(fallback_message)
            title = next((b.get("text") for b in card.get("body", []) if isinstance(b, dict) and b.get("type") == "TextBlock"), None)
            return notify_teams(title or "New notification (fallback)")
    except requests.RequestException as e:
        logging.error(f"Connection error with Teams (Adaptive Card): {e}")
        return False


def build_ticket_url(ticket_id: str) -> Optional[str]:
    """Build a direct link to the ticket if a template is provided.

    Example template: https://{account_id}.agidesk.com/tasks/{id}
    """
    template = AGIDESK_TICKET_URL_TEMPLATE.strip()
    if not template:
        return None
    try:
        return template.format(account_id=AGIDESK_ACCOUNT_ID, id=ticket_id)
    except Exception as e:
        logging.warning(f"Failed to render AGIDESK_TICKET_URL_TEMPLATE: {e}")
        return None


def build_ticket_adaptive_card(ticket: Ticket, ai_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Create an Adaptive Card payload to display a ticket nicely in Teams."""
    created_display = "N/A"
    if ticket.created_at:
        dt = parse_dt_loose(ticket.created_at)
        if dt:
            created_display = f"{ds_time(dt)} UTC"

    # Derive list/board labels, if available
    list_titles = []
    board_titles = set()
    if ticket.lists:
        for tl in ticket.lists.values():
            if getattr(tl, "title", None):
                list_titles.append(tl.title)
            if getattr(tl, "boards", None):
                for b in tl.boards.values():
                    if getattr(b, "title", None):
                        board_titles.add(b.title)

    facts = [
        {"title": "ID", "value": str(ticket.id)},
        {"title": "Criado", "value": created_display},
    ]
    if board_titles:
        facts.append({"title": "Board", "value": ", ".join(sorted(board_titles))})
    if list_titles:
        facts.append({"title": "Lista", "value": ", ".join(sorted(set(list_titles)))})
    # Add customer and contact if available
    if getattr(ticket, "customer", None):
        facts.append({"title": "Cliente", "value": str(ticket.customer)})
    if getattr(ticket, "contact", None):
        facts.append({"title": "Contato", "value": str(ticket.contact)})

    content_snippet = None
    if ticket.content:
        # Trim content to keep the card tidy
        content_snippet = shorten(ticket.content.strip(), width=500, placeholder="…")

    # Be resilient to slight key variations from the model
    def _get_first(d: Dict[str, Any], keys: list[str], default: str = "N/A") -> str:
        for k in keys:
            if k in d and d[k]:
                v = d[k]
                # Normalize non-string payloads
                if isinstance(v, list):
                    if all(isinstance(x, str) for x in v):
                        return "\n".join(f"- {x}" for x in v)
                    return json.dumps(v, ensure_ascii=False)
                if isinstance(v, dict):
                    return json.dumps(v, ensure_ascii=False)
                return str(v)
        return default

    resumo = _get_first(ai_summary, [
        "resumo_problema",
        "resumo",
        "resumo_do_problema",
        "resumoProblema",
    ])
    sugestao = _get_first(ai_summary, [
        "sugestao_solucao",
        "sugestao",
        "sugestao_de_solucao",
        "sugestaoDeSolucao",
        "solucao_sugerida",
    ])

    # Build actions
    actions = []
    ticket_url = build_ticket_url(ticket.id)
    if ticket_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "Abrir no Agidesk",
            "url": ticket_url,
        })

    card: Dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": "🚨 Novo Chamado! 🚨", "wrap": True, "weight": "Bolder", "size": "Large"},
            {"type": "TextBlock", "text": f"**Contato**: {ticket.contact or '(não informado)'}", "wrap": True, "spacing": "Small"},
        ],
        "actions": actions,
    }

    # Empresa (se houver)
    if getattr(ticket, "customer", None):#TODO: make only the ""Empresa"" part bold
        card["body"].append({
            "type": "TextBlock",
            "text": f"**Empresa**: {ticket.customer}",
            "wrap": True,
            "weight": "Bolder",
        })

    # Ticket line
    card["body"].append({
        "type": "TextBlock",
        "text": f"**Ticket**: #{ticket.id}: {ticket.title or '(Sem título)'}",
        "wrap": True,
        "weight": "Bolder",
    })

    # Link section (now encourages using the button instead of inline link)
    # card["body"].append({
    #     "type": "TextBlock",
    #     "text": "\n👇 Clique no botão abaixo para abrir o chamado:",
    #     "wrap": True,
    #     "spacing": "Medium",
    # })
    if not ticket_url:
        card["body"].append({
            "type": "TextBlock",
            "text": "(link não disponível)",
            "wrap": True,
        })

    # Mention-esque line (note: incoming webhooks don't create real mentions)
    # card["body"].append({
    #     "type": "TextBlock",
    #     "text": "\n@Time de Suporte, alguém pode assumir?",
    #     "wrap": True,
    #     "weight": "Bolder",
    #     "spacing": "Medium",
    # })

    # Keep AI details at the end as an optional section
    # if content_snippet:
    #     card["body"].append({"type": "TextBlock", "text": "\nDescrição:", "wrap": True, "weight": "Bolder", "spacing": "Medium"})
    #     card["body"].append({"type": "TextBlock", "text": content_snippet, "wrap": True, "spacing": "Small"})

    # card["body"].extend([
    #     {"type": "TextBlock", "text": "\nResumo do Problema (IA):", "wrap": True, "weight": "Bolder", "spacing": "Medium"},
    #     {"type": "TextBlock", "text": resumo, "wrap": True},
    #     {"type": "TextBlock", "text": "Sugestão de Solução (IA):", "wrap": True, "weight": "Bolder", "spacing": "Medium"},
    #     {"type": "TextBlock", "text": sugestao, "wrap": True},
    # ])

    return card


def build_ai_comment_html(ai_summary: Dict[str, Any]) -> str:
    """Build Agidesk comment HTML from structured AI response.

    Uses the AI's structured fields for lists when present, avoiding heuristic
    parsing. Supports optional arrays:
    - 'sugestao_solucao_lista' (itens sugeridos)
    and the main strings:
    - 'resumo_problema'
    - 'sugestao_solucao'
    """

    def esc(s: Any) -> str:
        return html.escape(str(s)) if s is not None else ""

    resumo_text = ""
    for k in ("resumo_problema", "resumo", "resumo_do_problema", "resumoProblema"):
        if ai_summary.get(k):
            resumo_text = str(ai_summary[k])
            break

    solucao_text = ""
    for k in ("sugestao_solucao", "sugestao", "sugestao_de_solucao", "sugestaoDeSolucao", "solucao_sugerida"):
        if ai_summary.get(k):
            solucao_text = str(ai_summary[k])
            break

    ul_items = ai_summary.get("sugestao_solucao_lista")

    def _clean_ul_item(s: Any) -> str:
        txt = str(s) if s is not None else ""
        try:
            return re.sub(r"^\s*(?:[-*•–—])\s*", "", txt)
        except re.error:
            return txt

    parts: list[str] = []
    parts.append("<b>Resumo do Problema:</b><br>" + esc(resumo_text).replace("\n", "<br>"))
    parts.append("<br><br><b>Sugestão:</b><br>" + esc(solucao_text).replace("\n", "<br>"))

    # Unordered list
    if isinstance(ul_items, list) and ul_items:
        cleaned = [_clean_ul_item(item) for item in ul_items]
        parts.append("<ul>" + "".join(f"<li>{esc(item)}</li>" for item in cleaned) + "</ul>")

    return "".join(parts)


def build_teams_text_message(ticket: Ticket) -> str:
    contact = ticket.contact or "(não informado)"
    customer = getattr(ticket, "customer", None)
    url = build_ticket_url(ticket.id)
    lines = [
        "🚨 Novo Chamado na Fila! 🚨",
        "",
        f"Contato: {contact}",
    ]
    if customer:
        lines.append(f"Empresa: {customer} (se houver)")
    title = ticket.title or "(Sem título)"
    lines.append(f"Ticket: #{ticket.id}: {title}")
    lines.append("")
    lines.append("👇 Clique para abrir o chamado:")
    lines.append(url or "(link não disponível)")
    lines.append("")
    return "\n".join(lines)


def process_issue(agi_client: AgideskAPI, issue: Ticket) -> Optional[Dict[str, Any]]:
    board_found = False
    if issue.lists:
        board_found = any(
            ticket_list.boards and ID_BOARD_SERVICOS in ticket_list.boards
            for ticket_list in issue.lists.values()
        )
    if not board_found:
        logging.debug(f"Ticket {issue.id} does not belong to board {ID_BOARD_SERVICOS}.")
        return None

    # First, get the AI summary to include in the card we post to Teams
    ai_summary = call_openai_simplified(issue)

    # Build and send Teams notification (text template or Adaptive Card)
    try:
        style = os.getenv("TEAMS_MESSAGE_STYLE", "card").lower().strip()
        fallback_text = build_teams_text_message(issue)
        if style == "text":
            if notify_teams(fallback_text):
                logging.info(f"✅ Text message for ticket {issue.id} sent to Teams")
            else:
                logging.error(f"❌ Failed to send text message for ticket {issue.id} to Teams")
        else:
            card = build_ticket_adaptive_card(issue, ai_summary)
            if notify_teams_adaptive(card, fallback_message=fallback_text):
                logging.info(f"✅ Adaptive Card for ticket {issue.id} sent to Teams")
            else:
                logging.error(f"❌ Failed to send Adaptive Card for ticket {issue.id} to Teams")
    except Exception as e:
        logging.error(f"Error building/sending Teams message for ticket {issue.id}: {e}")
    
    update_resp: Dict[str, Any] = {"status": "skipped in development mode"}
    if MODE == "production":
        comment_html = build_ai_comment_html(ai_summary)
        try:
            update_resp = agi_client.add_comment(issue.id, comment_html)
            logging.info(f"AI comment added to ticket {issue.id} in Agidesk.")
        except Exception as e:
            update_resp = {"error": str(e)}
            logging.error(f"Failed to add comment to ticket {issue.id} in Agidesk: {e}")
    else:
        logging.info(f"Skipping Agidesk update for ticket {issue.id} (non-production mode).")
        update_resp = {"status": "skipped, non-production mode"}

    return {
        "issue_id": issue.id,
        "ai_summary": ai_summary,
        "agidesk_update_response": update_resp
    }


def main(timer: func.TimerRequest) -> None:
    utc_timestamp = datetime.now(timezone.utc).isoformat()
    logging.info(f'Python timer trigger function ran at {utc_timestamp}')

    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if not AZURE_STORAGE_CONNECTION_STRING:
        missing_vars.append("AZURE_STORAGE_CONNECTION_STRING or AzureWebJobsStorage")
    if missing_vars:
        logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return

    logging.info("--- Starting Ticket Canary ---")

    agi = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    
    try:
        processed_ids = load_processed_ids()
        issues = agi.search_tickets(
            forecast='inbox',
            periodfield='created_at',
            initialdate=ds_time(now_utc() - timedelta(minutes=5)),
            finaldate=ds_time(now_utc()),
            per_page=100,
            fields='id,title,content,htmlcontent,created_at,lists,customer,customers,contact,contacts,fullcustomer,fullcontact',
        )
        logging.info(f"Found {len(issues)} tickets.")
        if MODE == "development":
            try:
                issues_json = [issue.model_dump(exclude_none=True) for issue in issues]
                logging.info("Search tickets result (JSON): " + json.dumps(issues_json, ensure_ascii=False))
            except Exception as e:
                logging.warning(f"Failed to serialize issues to JSON for logging: {e}")

        new_tickets = [issue for issue in issues if str(issue.id) not in processed_ids]
        logging.info(f"Found {len(new_tickets)} new tickets to process.")

        if MODE == "development":
            logging.info("--- DEVELOPMENT MODE ACTIVATED (Agidesk update disabled) ---")
        elif MODE == "production":
            logging.info("--- PRODUCTION MODE ACTIVATED (READ AND WRITE) ---")
        else:
            logging.error(f"Invalid mode '{MODE}'. Use 'development' or 'production'.")
            return

        for issue in new_tickets:
            result = process_issue(agi, issue)
            if result:
                logging.info(json.dumps(result, ensure_ascii=False, indent=2))
        
        all_ids_from_current_fetch = {str(issue.id) for issue in issues}
        if all_ids_from_current_fetch:
            save_processed_ids(all_ids_from_current_fetch)
            logging.info(f"State file updated with {len(all_ids_from_current_fetch)} IDs from the current fetch.")
    
    except Exception as e:
        logging.error(f"An error occurred in the polling cycle: {e}")
    
    logging.info("--- Ticket Canary finished ---")
