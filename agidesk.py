"""
Agidesk API Client
"""
import requests
import json
import time
from datetime import datetime
from typing import List, Optional, Dict, Union, Any
from pydantic import BaseModel


class Board(BaseModel):
    id: str
    title: str

class TicketList(BaseModel):
    id: str
    title: str
    boards: Optional[Dict[str, Board]] = None

class Ticket(BaseModel):
    id: str
    title: str
    content: Optional[str] = None
    created_at: Optional[str] = None
    lists: Optional[Dict[str, TicketList]] = None

class AgideskAPI:
    """A wrapper for the Agidesk API"""

    def __init__(self, account_id: str, app_key: str):
        if not account_id or not app_key:
            raise ValueError("Tenant ID and API Key are required.")
        
        self.base_url = f"https://{account_id}.agidesk.com/api/v1"
        self.app_key = app_key
        self.headers = {
            "X-Tenant-ID": account_id,
        }

    def search_tickets(self, **kwargs) -> List[Ticket]:
        url = f"{self.base_url}/search/issues"
        
        params = kwargs.copy()
        params['app_key'] = self.app_key
        
        try:
            response = requests.get(url, headers=self.headers, params=params)
            
            with open("api_responses.log", "a", encoding="utf-8") as f:
                f.write(f"--- API Response at {datetime.now().isoformat()} ---\n")
                try:
                    json.dump(response.json(), f, indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    f.write(response.text)
                f.write("\n---\n\n")

            response.raise_for_status()
            
            tickets_data = response.json()
            
            if isinstance(tickets_data, dict):
                if all(isinstance(v, dict) and 'id' in v for v in tickets_data.values()):
                    tickets_data = list(tickets_data.values())
                else:
                    tickets_data = [tickets_data]
            
            if not isinstance(tickets_data, list):
                print("Warning: API response was not a list of tickets.")
                return []
                
            return [Ticket.model_validate(ticket) for ticket in tickets_data]
        except requests.exceptions.RequestException as e:
            print(f"Error fetching from search/issues endpoint: {e}")
            if 'response' in locals() and response.text:
                print(f"Response body: {response.text}")
            return []
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Error decoding JSON from search/issues endpoint: {e}")
            return []

    def get_issue(self, issue_id: str) -> Optional[Ticket]:
        """Fetches a single ticket by its ID."""
        url = f"{self.base_url}/issues/{issue_id}"
        params = {'app_key': self.app_key}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return Ticket.model_validate(response.json())
        except requests.exceptions.RequestException as e:
            print(f"Error fetching ticket {issue_id}: {e}")
            return None
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Error decoding JSON for ticket {issue_id}: {e}")
            return None

    def update_issue(self, issue_id: str, payload: Dict) -> Dict:
        """Updates a ticket."""
        url = f"{self.base_url}/issues/{issue_id}"
        params = {'app_key': self.app_key}
        try:
            response = requests.put(url, headers=self.headers, params=params, json=payload, timeout=60)
            response.raise_for_status()
            if response.text.strip():
                return response.json()
            return {}
        except requests.exceptions.RequestException as e:
            print(f"Error updating ticket {issue_id}: {e}")
            if 'response' in locals() and response.text:
                print(f"Response body: {response.text}")
            raise
