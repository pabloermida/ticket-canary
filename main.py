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

POLL_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "300"))
FETCH_TIME_SECONDS = int(os.getenv("FETCH_TIME_SECONDS", "300"))  # 5 minutes in seconds

MODE = os.getenv("MODE", "development")  # Default to 'development' for safety

ID_TIME_SERVICOS = "1"

PROCESSED_IDS_FILE = "processed_ids.json"


def load_processed_ids() -> set[str]:
    """
    Carrega os IDs dos tickets processados do arquivo JSON.
    """
    if not os.path.exists(PROCESSED_IDS_FILE):
        return set()
    
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"Não foi possível ler o arquivo de IDs processados: {e}")
        return set()


def save_processed_ids(processed_ids: set[str]):
    """
    Salva o conjunto de IDs de tickets processados no arquivo JSON.
    """
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(processed_ids), f)


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


def within_last_seconds(created_at: str, seconds: int = 300) -> bool:
    dt = parse_dt_loose(created_at)
    return bool(dt) and (now_utc() - dt) <= timedelta(seconds=seconds)


# ============ OpenAI (Responses API com Structured Outputs) ============

def call_openai_simplified(issue: Ticket) -> Dict[str, str]:
    """
    Chama a API da OpenAI para obter um resumo e uma solução para o ticket usando o modo JSON.
    """
    system_text = (
        "You are an expert technical support engineer. Based on the ticket information, "
        "provide a JSON object with two keys: 'problem_summary' (a short, clear summary "
        "of the user's problem) and 'suggested_solution' (a possible solution or steps to solve it)."
    )
    
    issue_data = {
        "title": issue.title,
        "content": issue.content,
        "created_at": issue.created_at
    }
    user_text = json.dumps(issue_data, ensure_ascii=False)

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
        
        response_json = json.loads(response_content)
        return {
            "problem_summary": response_json.get("problem_summary", ""),
            "suggested_solution": response_json.get("suggested_solution", "")
        }
    except requests.RequestException as e:
        logging.error(f"Error calling OpenAI API: {e}")
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logging.error(f"Failed to parse OpenAI response: {e}")
    
    return {
        "problem_summary": "Error: Could not process AI response.",
        "suggested_solution": "Could not retrieve a valid solution from the AI."
    }


# ============ Teams Webhook ============

def notify_teams(message: str) -> bool:
    """
    Envia notificação para o Microsoft Teams.
    Retorna True se sucesso, False se falha.
    """
    if not TEAMS_WEBHOOK_URL:
        logging.error("TEAMS_WEBHOOK_URL não configurada")
        return False

    payload = {"text": message}

    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=30)
        if response.status_code == 200:
            logging.info("Mensagem enviada ao Teams com sucesso")
            return True
        else:
            logging.error(f"Erro ao enviar para Teams: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        logging.error(f"Erro de conexão com Teams: {e}")
        return False


# ============ Payload de update (Agidesk) ============

def build_agidesk_update_payload(ai_summary: Dict[str, str]) -> Dict[str, Any]:
    problem_summary = ai_summary.get("problem_summary", "")
    suggested_solution = ai_summary.get("suggested_solution", "")

    actiondescription = (
        f"**Resumo do Problema (IA):**\n{problem_summary}\n\n"
        f"**Solução Sugerida (IA):**\n{suggested_solution}\n"
    )

    return {
        "service": {
            "actiondescription": actiondescription,
        }
    }


# ============ Pipeline de 1 ticket ============

def process_issue(agi_client, issue: Ticket) -> Optional[Dict[str, Any]]:
    """
    Processa um ticket se passar na validação.
    Retorna None se o ticket não passar na validação.
    """
    # Validação do ticket
    if not within_last_seconds(issue.created_at or "", FETCH_TIME_SECONDS):
        logging.debug(f"Ticket {issue.id} fora da janela de tempo")
        return None
    
    if str(issue.team_id) != ID_TIME_SERVICOS:
        logging.debug(f"Ticket {issue.id} não é do time de serviços")
        return None

    notification_text = f"Novo ticket recebido: ID {issue.id} - {issue.title}"
    if notify_teams(notification_text):
        logging.info(f"✅ Notificação para o ticket {issue.id} enviada ao Teams")
    else:
        logging.error(f"❌ Falha ao enviar notificação para o ticket {issue.id} ao Teams")

    # 2. Obtém o resumo da IA
    ai_summary = call_openai_simplified(issue)
    
    # 3. Atualiza o ticket no Agidesk com o conteúdo da IA
    update_payload = build_agidesk_update_payload(ai_summary)
    
    try:
        update_resp = agi_client.update_issue(issue.id, update_payload)
        logging.info(f"Ticket {issue.id} atualizado no Agidesk com resumo da IA.")
    except Exception as e:
        update_resp = {"error": str(e)}
        logging.error(f"Falha ao atualizar ticket {issue.id} no Agidesk: {e}")

    return {
        "issue_id": issue.id,
        "ai_summary": ai_summary,
        "agidesk_update_response": update_resp
    }


# ============ MAIN ============

def main() -> None:
    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        logging.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return

    logging.info("--- Starting Ticket Canary ---")

    processed_ids = load_processed_ids()
    agi = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    while True:
        try:
            start_time = now_utc()
            initial_date = start_time - timedelta(seconds=FETCH_TIME_SECONDS)
            
            issues = agi.search_tickets(
                forecast='teams',
                periodfield='created_at',
                initialdate=initial_date,
                per_page=100,
                team=[ID_TIME_SERVICOS],
                fields='id,title,content,contact,contacts,responsible_id,priority,type,team_id'
            )
            logging.info(f"Found {len(issues)} tickets.")

            if MODE == "development":
                logging.info("--- MODO DE DESENVOLVIMENTO ATIVADO (SOMENTE LEITURA) ---")
                logging.info(f"Encontrados {len(issues)} tickets (sem processamento):")
                for issue in issues:
                    print(f"  - ID: {issue.id}, Título: {issue.title}")
            elif MODE == "production":
                logging.info("--- MODO DE PRODUÇÃO ATIVADO (LEITURA E ESCRITA) ---")
                current_run_processed_ids = set()
                for issue in issues:
                    if str(issue.id) in processed_ids:
                        continue

                    result = process_issue(agi, issue)
                    if result:
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        current_run_processed_ids.add(str(issue.id))
                
                if current_run_processed_ids:
                    save_processed_ids(current_run_processed_ids)
                    logging.info(f"Salvo {len(current_run_processed_ids)} novos IDs processados.")

                processed_count = len(current_run_processed_ids)
                if processed_count == 0 and issues:
                    logging.info("Nenhum ticket novo foi processado (todos já foram vistos ou foram filtrados)")
                elif processed_count > 0:
                    logging.info(f"Processados {processed_count} novos tickets")
            else:
                logging.error(f"Modo '{MODE}' inválido. Use 'development' ou 'production'.")
                
        except Exception as e:
            logging.error(f"An error occurred: {e}")
        
        logging.info(f"Finished check. Waiting for {POLL_INTERVAL_SEC} seconds...")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
