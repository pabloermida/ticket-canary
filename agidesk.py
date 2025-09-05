"""
Agidesk API Client
"""
import requests
import json
import time
from datetime import datetime
from typing import List, Optional, Dict, Union, Any
from pydantic import BaseModel, field_validator, model_validator


class Board(BaseModel):
    id: str
    title: str

class TicketList(BaseModel):
    id: str
    title: str
    boards: Optional[Dict[str, Board]] = None

    @field_validator('boards', mode='before')
    @classmethod
    def normalize_boards(cls, v):
        """Accept dict or list for boards; coerce list -> dict keyed by id."""
        if v is None or v == {}:
            return None
        if isinstance(v, list):
            if not v:
                return None
            out: Dict[str, Any] = {}
            for item in v:
                if isinstance(item, dict):
                    bid = item.get('id') or item.get('board_id') or item.get('slug')
                    if bid is not None:
                        out[str(bid)] = item
            return out or None
        if isinstance(v, dict):
            return v
        return None

class Ticket(BaseModel):
    id: str
    title: str
    content: Optional[str] = None
    htmlcontent: Optional[str] = None
    created_at: Optional[str] = None
    lists: Optional[Dict[str, TicketList]] = None
    customer: Optional[str] = None
    contact: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def derive_customer_contact(cls, data):
        """Coerce various Agidesk shapes into simple 'customer' and 'contact' strings.

        - Some payloads expose 'customers'/'contacts' as dicts keyed by ID. Pick the
          default item when flagged, otherwise the first one, and extract a readable title.
        - Also accept 'fullcustomer'/'fullcontact' fallbacks when present.
        """
        if not isinstance(data, dict):
            return data

        def pick_name(container):
            if container is None:
                return None
            items = None
            if isinstance(container, dict):
                items = list(container.values())
            elif isinstance(container, list):
                items = container
            else:
                return None
            if not items:
                return None
            chosen = None
            for it in items:
                if isinstance(it, dict) and (it.get('default') == '1' or it.get('default') is True):
                    chosen = it
                    break
            if chosen is None:
                chosen = items[0] if isinstance(items[0], dict) else None
            if not isinstance(chosen, dict):
                return None
            for key in ('fulltitle', 'title', 'fullname', 'contacttitle', 'fullcustomer', 'fullcontact'):
                val = chosen.get(key)
                if isinstance(val, str) and val.strip():
                    return val
            fn = chosen.get('firstname')
            ln = chosen.get('lastname')
            if fn or ln:
                return ' '.join([p for p in (fn, ln) if p])
            return None

        if 'customer' not in data or not data.get('customer'):
            name = pick_name(data.get('customers'))
            if name:
                data['customer'] = name
            elif isinstance(data.get('fullcustomer'), str):
                data['customer'] = data['fullcustomer']

        if 'contact' not in data or not data.get('contact'):
            name = pick_name(data.get('contacts'))
            if name:
                data['contact'] = name
            elif isinstance(data.get('fullcontact'), str):
                data['contact'] = data['fullcontact']

        return data

    @field_validator('lists', mode='before')
    @classmethod
    def normalize_lists(cls, v):
        """Accept dict or list for lists; coerce list -> dict keyed by id.

        Some Agidesk responses return `lists: []` or `lists: [{...}]` rather than
        an object. This normalizes to the dict shape our code expects.
        """
        if v is None or v == {}:
            return None
        if isinstance(v, list):
            if not v:
                return None
            out: Dict[str, Any] = {}
            for item in v:
                if isinstance(item, dict):
                    lid = item.get('id') or item.get('list_id') or item.get('slug')
                    if lid is not None:
                        out[str(lid)] = item
            return out or None
        if isinstance(v, dict):
            return v
        return None

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

    def add_comment(self, issue_id: str, html_content: str) -> Dict:
        """Adds a new internal comment to a ticket."""
        url = f"{self.base_url}/comments"
        params = {'app_key': self.app_key}
        payload = {
            "module": "tasks",
            "privacy_id": 2,
            "htmlcontent": html_content,
            "tasks": int(issue_id)
        }
        try:
            response = requests.post(url, headers=self.headers, params=params, json=payload, timeout=60)
            response.raise_for_status()
            if response.text.strip():
                return response.json()
            return {}
        except requests.exceptions.RequestException as e:
            print(f"Error adding comment to ticket {issue_id}: {e}")
            if 'response' in locals() and response.text:
                print(f"Response body: {response.text}")
            raise
