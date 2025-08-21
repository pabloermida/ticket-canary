import time
import os
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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # seconds
FETCH_TIME_MINUTES = int(os.getenv("FETCH_TIME_MINUTES", "5")) # minutes


def fetch_tickets(api: AgideskAPI, initial_date: str) -> List[Ticket]:
    """
    Fetch recent, unassigned, and active tickets from Agidesk's API
    """
    tickets = api.search_tickets(
        # active=1,
        # responsible='null',
        sorting='-id',
        length=25,
        # periodfield='created_at',
        initialdate=initial_date
    )
    

    if len(tickets) == 0:
        print("no tickets found")
        return []
    
    print(f"{len(tickets)} were found")

    return tickets


def notify_teams(ticket: Ticket):
    """
    Sends a notification to Microsoft Teams about a new ticket.
    """
    msg = f"**Novo Ticket:** #{ticket.id}: {ticket.title}"

    print(ticket)

    # if ticket.priority == "Alta":
    #     msg += " 🚨 **VIP**"

    print(f"Notifying for ticket #{ticket.id}")
    try:
        # notificar
        print(f"Notification sent for ticket #{ticket.id}, message: {msg}")
    except requests.exceptions.RequestException as e:
        print(f"Error sending notification for ticket #{ticket.id}: {e}")


def main():
    required_vars = {
        "AGIDESK_ACCOUNT_ID": AGIDESK_ACCOUNT_ID,
        "AGIDESK_APP_KEY": AGIDESK_APP_KEY,
        "TEAMS_WEBHOOK_URL": TEAMS_WEBHOOK_URL,
    }

    missing_vars = [key for key, value in required_vars.items() if not value]

    if missing_vars:
        print("Error: Missing required environment variables.")
        print(f"Please set the following variables in your .env file: {', '.join(missing_vars)}")
        return

    print("Starting Agidesk → Teams notifier...")
    
    agidesk_api = AgideskAPI(account_id=AGIDESK_ACCOUNT_ID, app_key=AGIDESK_APP_KEY)
    seen_tickets = set()

    while True:
        time_ago = datetime.now() - timedelta(minutes=FETCH_TIME_MINUTES)
        initial_date_str = time_ago.strftime('%Y-%m-%d %H:%M:%S')

        print(f"Fetching tickets from: {initial_date_str}")

        tickets = fetch_tickets(agidesk_api, initial_date=initial_date_str)
        for ticket in tickets:
            notify_teams(ticket)
            seen_tickets.add(ticket.id)

        print("Finished, now I'm taking a break... 🏖️")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
