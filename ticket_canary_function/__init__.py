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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
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
    """Call OpenAI to summarize the ticket. Includes images if present.

    If the ticket contains HTML content with <img src="..."> tags, extract the
    image URLs and pass them to the model alongside the text using multi-part
    message content. Falls back to text-only if no images are found.
    """

    def extract_image_urls(html_content: Optional[str]) -> List[str]:
        if not html_content:
            return []
        # Match src="..." or src='...' within <img ...> tags
        urls: List[str] = []
        try:
            urls.extend(re.findall(r"<img[^>]+src=\"([^\"]+)\"", html_content, flags=re.IGNORECASE))
            urls.extend(re.findall(r"<img[^>]+src='([^']+)'", html_content, flags=re.IGNORECASE))
        except re.error:
            pass
        # Keep http(s) only to avoid data URIs or unsupported schemes
        return [u for u in urls if u.startswith("http://") or u.startswith("https://")]

    system_text = (
        "Você é um Analista de Suporte N2 dos clientes da Infraestrutura da Infiniit (infiniit.com.br). "
        "Leia atentamente os dados do ticket e responda SOMENTE com um objeto JSON contendo: "
        "'resumo_problema' (string) e 'sugestao_solucao' (string). Quando a sugestão contiver itens em lista/etapas, também preencha "
        "'sugestao_solucao_lista' (array de strings para lista não ordenada) e/ou 'sugestao_solucao_lista_ordenada' (array de strings para passos numerados). "
        "Regras e escopo: "
        "1) Foque em análise de infraestrutura (redes, Windows/Linux Server, virtualização/VMware, backup/Veeam, firewalls, Azure/M365, monitoramento e segurança). "
        "2) Forneça diagnóstico e próxima ação acionável em nível N2: hipóteses, comandos/verificações, logs a coletar, e validações passo a passo. "
        "3) Quando pertinente, faça referência a recursos públicos abertos (nome do recurso e URL de documentação oficial, KBs de fornecedor, CVEs, guias). NÃO invente fontes; se não puder confirmar um link específico, cite apenas o nome do recurso e marque como sugestivo. "
        "4) Se houver imagens, considere-as como evidência auxiliar. "
        "5) Não exponha dados sensíveis além do que foi fornecido; mantenha linguagem objetiva e profissional em PT-BR. "
        "6) Se o conteúdo for incompatível com análise de infraestrutura (ex.: assunto comercial, financeiro, sem dados técnicos, ou não relacionado a TI), retorne 'resumo_problema' como string vazia e 'sugestao_solucao' com a frase: 'Entrada incompatível com análise de infraestrutura.'. "
        "7) Saída estritamente em JSON válido, sem texto extra, sem comentários, sem campos adicionais."
    )
    user_text = f"Título: {ticket.title}\nConteúdo: {ticket.content}"
    image_urls = extract_image_urls(getattr(ticket, "htmlcontent", None))

    # Build multi-part user content if images are available (Responses API types)
    user_content: Any
    if image_urls:
        parts: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
        for url in image_urls:
            parts.append({"type": "input_image", "image_url": {"url": url}})
        user_content = parts
    else:
        user_content = user_text

    # Responses API endpoint
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    def _openai_debug() -> bool:
        v = os.getenv("OPENAI_DEBUG", "").strip().lower()
        return v in {"1", "true", "yes", "on"}

    # Default token parameter for Responses API
    token_param_name = "max_output_tokens"

    def make_payload(response_format: Dict[str, Any], content: Any) -> Dict[str, Any]:
        # Build Responses API input
        if isinstance(content, list):
            user_parts = content
        else:
            user_parts = [{"type": "text", "text": str(content)}]
        input_payload = [
            {"role": "system", "content": [{"type": "text", "text": system_text}]},
            {"role": "user", "content": user_parts},
        ]
        payload: Dict[str, Any] = {
            "model": OPENAI_MODEL,
            "response_format": response_format,
            "input": input_payload,
            "temperature": 0,
        }
        payload[token_param_name] = 1000
        return payload

    def parse_response(resp_json: Dict[str, Any]) -> Dict[str, Any]:
        # 1) Shortcut present in some SDKs
        if isinstance(resp_json, dict) and isinstance(resp_json.get("output_text"), str):
            try:
                return json.loads(resp_json["output_text"])  # type: ignore[index]
            except Exception:
                pass
        # 2) General Responses API structure: output -> message -> content -> text
        out = resp_json.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                            try:
                                return json.loads(part.get("text", "{}"))
                            except Exception:
                                continue
        # 3) Fallback to chat-completions shape if present
        try:
            content = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            return json.loads(content)
        except Exception:
            return {"resumo_problema": "Error: Could not parse AI response.", "sugestao_solucao": "N/A"}

    response_format_object = {"type": "json_object"}

    def post_and_parse(payload: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        if r.status_code >= 400 and _openai_debug():
            try:
                logging.error(f"OpenAI error body: {r.text}")
            except Exception:
                pass
        r.raise_for_status()
        return parse_response(r.json())

    def try_with_auto_token_param(content: Any) -> Optional[Dict[str, Any]]:
        nonlocal token_param_name
        # First try with current token_param_name
        try:
            payload = make_payload(response_format_object, content)
            return post_and_parse(payload)
        except requests.HTTPError as e:
            body = getattr(getattr(e, 'response', None), 'text', '') or str(e)
            if _openai_debug():
                logging.debug(f"OpenAI HTTPError: {body}")
            # Adjust token param if model hints
            if "max_tokens" in body and token_param_name != "max_tokens":
                token_param_name = "max_tokens"
                try:
                    payload2 = make_payload(response_format_object, content)
                    return post_and_parse(payload2)
                except Exception:
                    pass
            if "max_completion_tokens" in body and token_param_name != "max_completion_tokens":
                token_param_name = "max_completion_tokens"
                try:
                    payload3 = make_payload(response_format_object, content)
                    return post_and_parse(payload3)
                except Exception:
                    pass
            raise

    try:
        # Attempt: json_object with user_content (may include images)
        ai_summary = try_with_auto_token_param(user_content)
        if ai_summary is not None:
            if _openai_debug():
                logging.debug(f"AI response for ticket {ticket.id}: {ai_summary}")
            return ai_summary
    except requests.HTTPError as e1:
        # If images not supported or content must be string, retry as text-only
        err_txt = getattr(getattr(e1, 'response', None), 'text', '') or str(e1)
        if _openai_debug():
            logging.debug(f"OpenAI HTTPError on first attempt: {err_txt}")

        text_only = user_text
        if image_urls:
            text_only += "\n\nImagens (URLs):\n" + "\n".join(image_urls)

        try:
            ai_summary = try_with_auto_token_param(text_only)
            if ai_summary is not None:
                if _openai_debug():
                    logging.debug(f"AI response (json_object, text-only) for ticket {ticket.id}: {ai_summary}")
                return ai_summary
        except Exception as e2:
            logging.error(f"Error calling OpenAI API after fallback: {e2}")
    except Exception as e:
        logging.error(f"Error calling OpenAI API: {e}")
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
    - 'sugestao_solucao_lista' (unordered)
    - 'sugestao_solucao_lista_ordenada' (ordered)
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
    ol_items = ai_summary.get("sugestao_solucao_lista_ordenada")

    def _clean_ol_item(s: Any) -> str:
        txt = str(s) if s is not None else ""
        try:
            return re.sub(r"^\s*\d+[\.)]?\s*", "", txt)
        except re.error:
            return txt

    def _clean_ul_item(s: Any) -> str:
        txt = str(s) if s is not None else ""
        try:
            return re.sub(r"^\s*(?:[-*•–—])\s*", "", txt)
        except re.error:
            return txt

    parts: list[str] = []
    parts.append("<b>Resumo do Problema:</b><br>" + esc(resumo_text).replace("\n", "<br>"))
    parts.append("<br><br><b>Sugestão:</b><br>" + esc(solucao_text).replace("\n", "<br>"))

    # Ordered list (steps)
    if isinstance(ol_items, list) and ol_items:
        cleaned = [_clean_ol_item(item) for item in ol_items]
        parts.append("<ol>" + "".join(f"<li>{esc(item)}</li>" for item in cleaned) + "</ol>")

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
