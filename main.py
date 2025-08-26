import time
import requests
import os
import logging
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

# Configura logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

AGIDESK_API_URL = os.getenv("AGIDESK_API_URL") 
AGIDESK_API_KEY = os.getenv("AGIDESK_API_KEY") 
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL") 
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  

def fetch_tickets() -> List[Dict]:
    """
    Busca tickets recentes da API do Agidesk.
    """
    headers = {
        "Authorization": f"Bearer {AGIDESK_API_KEY}",
        "Accept": "application/json"
    }
    try:
        response = requests.get(AGIDESK_API_URL, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error(f"Erro ao buscar tickets: {e}")
        return [] 

def notify_teams(ticket: Dict):
    """
    Envia notificação para o Microsoft Teams sobre um novo ticket.
    """
    message = {
        "text": f"Novo ticket:\n- ID: {ticket['id']}\n- Título: {ticket.get('title', 'Sem título')}"
    }

    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=message, timeout=10)
        if response.status_code == 200:
            logging.info(f"Ticket #{ticket['id']} enviado ao Teams")
        else:
            logging.error(f"Erro ao enviar para Teams: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        logging.error(f"Erro de conexão com Teams: {e}")

def main():
    if not AGIDESK_API_KEY or not TEAMS_WEBHOOK_URL or not AGIDESK_API_URL:
        logging.critical("Variáveis de ambiente ausentes! Verifique .env")
        return

    logging.info("Iniciando Agidesk -> Teams notifier...")
    seen_tickets = set() 

    while True:
        tickets = fetch_tickets()

        for ticket in tickets:
            ticket_id = ticket.get("id")
            team_id = ticket.get("team_id")

            if ticket_id and ticket_id not in seen_tickets and team_id is None:
                notify_teams(ticket)
                seen_tickets.add(ticket_id)

        logging.info(f"Finalizado ciclo, aguardando {CHECK_INTERVAL} segundos...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
