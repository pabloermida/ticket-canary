import time
import os
import logging
from typing import List, Dict
from typing import List
from dotenv import load_dotenv
from agidesk import AgideskAPI, Ticket
import requests
from datetime import datetime, timedelta

load_dotenv()

# ---- Config ----
AGIDESK_ACCOUNT_ID = os.getenv("AGIDESK_ACCOUNT_ID")
AGIDESK_APP_KEY = os.getenv("AGIDESK_APP_KEY")
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
FETCH_TIME_MINUTES = int(os.getenv("FETCH_TIME_MINUTES", "50"))
ID_TIME_SERVICOS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def fetch_tickets(api: AgideskAPI, initial_date: str) -> List[Ticket]:
    """
    Fetches recent tickets from the teams the user is part of.
    """
    print(f"Fetching tickets since {initial_date}...")
    tickets = api.search_tickets(
        forecast='teams',
        periodfield='created_at',
        initialdate=initial_date,
        per_page=100,
        team=[ID_TIME_SERVICOS],
        fields='id,title,content,contact,contacts,responsible_id,priority,type,team_id'
    )

    if not tickets:
        print("No new tickets found in the specified teams and timeframe.")
        return []
    
    print(f"Found {len(tickets)} new tickets.")
    return tickets


def notify_teams(ticket: Ticket):
    """
    Envia notificação para o Microsoft Teams sobre um novo ticket.
    """
    message = {
        "text": f"Novo ticket:\n- ID: {ticket.id}\n- Título: {ticket.title}"
    }

    try:
        response = requests.post(TEAMS_WEBHOOK_URL, json=message, timeout=10)
        if response.status_code == 200:
            logging.info(f"Ticket #{ticket.id} enviado ao Teams")
        else:
            logging.error(f"Erro ao enviar para Teams: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        logging.error(f"Erro de conexão com Teams: {e}")


def main():
    """
    Main application loop.
    """
    required_vars = ["AGIDESK_ACCOUNT_ID", "AGIDESK_APP_KEY", "TEAMS_WEBHOOK_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        print("Error: Missing required environment variables.")
        print(f"Please set the following: {', '.join(missing_vars)}")
        return

    print("--- Starting Ticket Canary ---")
    
    api = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    seen_tickets = set()

    try:
        while True:
            start_time = datetime.now()
            
            time_ago = start_time - timedelta(minutes=FETCH_TIME_MINUTES)
            initial_date_str = time_ago.strftime('%Y-%m-%d %H:%M:%S')

            new_tickets = fetch_tickets(api, initial_date_str)
            
            notification_count = 0
            for ticket in new_tickets:
                if ticket.id not in seen_tickets:
                    # First time seeing this ticket. Print its content.
                    print("-" * 20)
                    print(f"New Ticket #{ticket.id} Title:")
                    print(ticket.title)
                    print("-" * 20)

                    # We are interested in tickets that are not assigned to a specific person yet.
                    if ticket.responsible_id is None:
                        notify_teams(ticket)
                        notification_count += 1
                    
                    # Add to seen_tickets so we don't log or notify again.
                    seen_tickets.add(ticket.id)
            
            if notification_count == 0 and new_tickets:
                print("New tickets were found, but all are already assigned or have been seen. No new notifications sent.")

            print(f"Finished check. Waiting for {CHECK_INTERVAL} seconds...")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n--- Shutting down gracefully ---")


if __name__ == "__main__":
    main()
