"""
Agidesk API Client
"""
import requests
import json
import time
from datetime import datetime
from typing import List, Optional, Dict, Union
from pydantic import BaseModel


class TicketTeam(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None

class TicketPriority(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None

class TicketCustomer(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None
    email: Optional[str] = None

class TicketContact(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None
    email: Optional[str] = None
    
class TicketService(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None

class Ticket(BaseModel):
    id: str
    title: str
    fulltitle: Optional[str] = None
    content: Optional[str] = None
    htmlcontent: Optional[str] = None
    active: Optional[str] = None
    status_id: Optional[str] = None
    type_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    duedate: Optional[str] = None
    priority: Optional[Union[str, TicketPriority]] = None
    priority_id: Optional[str] = None
    responsible_id: Optional[str] = None
    team_id: Optional[str] = None
    contact: Optional[str] = None
    customer: Optional[str] = None
    service: Optional[str] = None
    type: Optional[str] = None
    team: Optional[Union[str, TicketTeam]] = None
    customers: Optional[Dict[str, TicketCustomer]] = None
    contacts: Optional[Dict[str, TicketContact]] = None
    services: Optional[Dict[str, TicketService]] = None

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
        """
        [CORRECTED] Fetches tickets using the /search/issues endpoint.
        """
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
