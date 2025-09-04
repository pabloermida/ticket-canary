import os
import json
import logging
import re
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
        "Você é um engenheiro de suporte técnico especialista. Com base nas informações do ticket, "
        "forneça um objeto JSON com duas chaves: 'resumo_problema' (um resumo curto e claro "
        "do problema do usuário) e 'sugestao_solucao' (uma possível solução ou passos para resolvê-lo)."
    )
    user_text = f"Título: {ticket.title}\nConteúdo: {ticket.content}"
    image_urls = extract_image_urls(getattr(ticket, "htmlcontent", None))

    # Build multi-part user content if images are available
    user_content: Any
    if image_urls:
        parts: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
        for url in image_urls:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        user_content = parts
    else:
        user_content = user_text

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ],
        # Ensure we have some room for JSON output
        "max_tokens": 1000,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        response_content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        ai_summary = json.loads(response_content)
        logging.info(f"AI response for ticket {ticket.id}: {ai_summary}")
        return ai_summary
    except requests.RequestException as e:
        logging.error(f"Error calling OpenAI API: {e}")
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logging.error(f"Failed to parse OpenAI response: {e}")
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

    resumo = ai_summary.get("resumo_problema") or "N/A"
    sugestao = ai_summary.get("sugestao_solucao") or "N/A"

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
            {"type": "TextBlock", "text": "🚨 Novo Chamado na Fila! 🚨", "wrap": True, "weight": "Bolder", "size": "Large"},
            {"type": "TextBlock", "text": f"Contato: {ticket.contact or '(não informado)'}", "wrap": True, "spacing": "Small"},
        ],
        "actions": actions,
    }

    # Empresa (se houver)
    if getattr(ticket, "customer", None):
        card["body"].append({
            "type": "TextBlock",
            "text": f"Empresa: {ticket.customer} (se houver)",
            "wrap": True,
        })

    # Ticket line
    card["body"].append({
        "type": "TextBlock",
        "text": f"Ticket: #{ticket.id}: {ticket.title or '(Sem título)'}",
        "wrap": True,
    })

    # Link section
    card["body"].append({
        "type": "TextBlock",
        "text": "\n👇 Clique para abrir o chamado:",
        "wrap": True,
        "spacing": "Medium",
    })
    if ticket_url:
        # Use markdown link so it displays text and is clickable
        card["body"].append({
            "type": "TextBlock",
            "text": f"[Link para o Chamado]({ticket_url})",
            "wrap": True,
        })
    else:
        card["body"].append({
            "type": "TextBlock",
            "text": "(link não disponível)",
            "wrap": True,
        })

    # Mention-esque line (note: incoming webhooks don't create real mentions)
    card["body"].append({
        "type": "TextBlock",
        "text": "\n@Time de Suporte, alguém pode assumir?",
        "wrap": True,
        "weight": "Bolder",
        "spacing": "Medium",
    })

    # Keep AI details at the end as an optional section
    if content_snippet:
        card["body"].append({"type": "TextBlock", "text": "\nDescrição:", "wrap": True, "weight": "Bolder", "spacing": "Medium"})
        card["body"].append({"type": "TextBlock", "text": content_snippet, "wrap": True, "spacing": "Small"})

    card["body"].extend([
        {"type": "TextBlock", "text": "\nResumo do Problema (IA):", "wrap": True, "weight": "Bolder", "spacing": "Medium"},
        {"type": "TextBlock", "text": resumo, "wrap": True},
        {"type": "TextBlock", "text": "Sugestão de Solução (IA):", "wrap": True, "weight": "Bolder", "spacing": "Medium"},
        {"type": "TextBlock", "text": sugestao, "wrap": True},
    ])

    return card


def build_ai_comment_html(ai_summary: Dict[str, str]) -> str:
    resumo = ai_summary.get('resumo_problema', 'N/A')
    solucao = ai_summary.get('sugestao_solucao', 'N/A')
    return (
        f"<b>Resumo do Problema (IA):</b><br>{resumo}<br><br>"
        f"<b>Sugestão de Solução (IA):</b><br>{solucao}"
    )


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
    lines.append("@Time de Suporte, alguém pode assumir?")
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
    if MODE == "production" or issue.id == "3315": #TODO: remove testing hard code
        comment_html = build_ai_comment_html(ai_summary)
        try:
            update_resp = agi_client.add_comment(issue.id, comment_html)
            logging.info(f"AI comment added to ticket {issue.id} in Agidesk.")
        except Exception as e:
            update_resp = {"error": str(e)}
            logging.error(f"Failed to add comment to ticket {issue.id} in Agidesk: {e}")
    else:
        logging.info(f"Skipping update for ticket {issue.id} (not the test ticket 3315).")
        update_resp = {"status": "skipped, not test ticket"}

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
            period='today', # TODO change to 'last_5_minutes' after testing
            per_page=100,
            fields='id,title,content,htmlcontent,created_at,lists',
        )
        logging.info(f"Found {len(issues)} tickets.")

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
