import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

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
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
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
    system_text = (
        "Você é um engenheiro de suporte técnico especialista. Com base nas informações do ticket, "
        "forneça um objeto JSON com duas chaves: 'resumo_problema' (um resumo curto e claro "
        "do problema do usuário) e 'sugestao_solucao' (uma possível solução ou passos para resolvê-lo)."
    )
    user_text = f"Título: {ticket.title}\nConteúdo: {ticket.content}"
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ],
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


def build_ai_comment_html(ai_summary: Dict[str, str]) -> str:
    resumo = ai_summary.get('resumo_problema', 'N/A')
    solucao = ai_summary.get('sugestao_solucao', 'N/A')
    return (
        f"<b>Resumo do Problema (IA):</b><br>{resumo}<br><br>"
        f"<b>Sugestão de Solução (IA):</b><br>{solucao}"
    )


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

    notification_text = f"New ticket received: ID {issue.id} - {issue.title}"
    if notify_teams(notification_text):
        logging.info(f"✅ Notification for ticket {issue.id} sent to Teams")
    else:
        logging.error(f"❌ Failed to send notification for ticket {issue.id} to Teams")

    ai_summary = call_openai_simplified(issue)
    
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

    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL", "AZURE_STORAGE_CONNECTION_STRING"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return

    logging.info("--- Starting Ticket Canary ---")

    agi = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    
    try:
        processed_ids = load_processed_ids()
        issues = agi.search_tickets(
            forecast='inbox',
            period='today',
            per_page=100,
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
