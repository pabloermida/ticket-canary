import time
import requests
import os
from typing import List, Dict
from dotenv import load_dotenv

load_dotenv()

# ---- Config ----
AGIDESK_API_URL = os.getenv("AGIDESK_API_URL", "https://api.agidesk.com/v1/tickets")
AGIDESK_API_KEY = os.getenv("AGIDESK_API_KEY")
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # seconds


def fetch_tickets() -> List[Dict]:
    """
    Fetch recent tickets from Agidesk's API
    """

    headers = {
        "Authorization": f"Bearer {AGIDESK_API_KEY}",
        "Accept": "application/json"
    }
    response = requests.get(AGIDESK_API_URL, headers=headers)
    response.raise_for_status()
    return response.json()

def notify_teams(ticket: Dict):
    """
    Sends a notification to Microsoft Teams about a new ticket.
    """

    message = {
        "text": f"New ticket: {ticket['id']} - {ticket['title']}"
    }
    response = requests.post(TEAMS_WEBHOOK_URL, json=message)
    if response.status_code == 200:
        print(f"Ticket #{ticket['id']} send to Teams")
    else:
        print(f"Error sending to Teams: {response.status_code}, {response.text}")

def main():
    if not AGIDESK_API_KEY or not TEAMS_WEBHOOK_URL:
        print("ERROR: Missing variables!")
        print("Please, set AGIDESK_API_KEY and TEAMS_WEBHOOK_URL")
        return

    print("Starting Agidesk -> Teams notifier...")
    seen_tickets = set()

    while True:
        tickets = fetch_tickets()
        for ticket in tickets:
            if ticket["id"] not in seen_tickets and ticket.get("team_id") is None:
                notify_teams(ticket)
                seen_tickets.add(ticket["id"])

        time.sleep(CHECK_INTERVAL)
        print("Finished, trying again...")

if __name__ == "__main__":
    main()
