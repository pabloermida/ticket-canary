"""
Agidesk API Client
"""
import requests
from typing import List, Optional, Dict
from pydantic import BaseModel


class TicketTeam(BaseModel):
    id: str
    title: str

class Ticket(BaseModel):
    id: str
    title: str
    created_at: str
    priority: str
    responsible_id: Optional[str]
    team: Optional[TicketTeam]

class AgideskAPI:
    """A wrapper for the Agidesk API"""

    def __init__(self, account_id: str, app_key: str):
        if not account_id or not app_key:
            raise ValueError("Tenant ID and API Key are required.")
        
        self.base_url = f"https://{account_id}.agidesk.com/api/v1"
        self.app_key = app_key
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Tenant-ID": account_id,
        }

    def search_tickets(self, **kwargs) -> List[Ticket]:
        """
        Searches for tickets using the /search/issues endpoint.
        """
        url = f"{self.base_url}/search/issues"
        params = {'app_key': self.app_key}
        
        try:
            response = requests.post(url, headers=self.headers, params=params, data=kwargs)
            response.raise_for_status()
            
            tickets_data = response.json()
            
            # The API might return a single object or a list of objects.
            if isinstance(tickets_data, dict):
                tickets_data = [tickets_data]
            
            return [Ticket.model_validate(ticket) for ticket in tickets_data]
        except requests.exceptions.RequestException as e:
            print(f"Error fetching tickets from Agidesk: {e}")
            if 'response' in locals() and response.text:
                print(f"Response body: {response.text}")
            return []
        except ValueError as e: # Catches JSON decoding errors
            print(f"Error decoding JSON from Agidesk API: {e}")
            return []
