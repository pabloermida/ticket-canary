import os
import json
import logging
from typing import List


def load_local_env_from_settings(path: str = "local.settings.json") -> None:
    """Load environment variables from local.settings.json (Functions-style).

    Only sets variables that are not already present in the environment.
    """
    if not os.path.exists(path):
        logging.info(f"No {path} found; relying on current environment.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("Values", {}) or {}
        for k, v in values.items():
            if os.getenv(k) is None and v is not None:
                os.environ[k] = str(v)
        logging.info("Loaded environment from local.settings.json")
    except Exception as e:
        logging.warning(f"Failed to load {path}: {e}")


def run_function_timer():
    """Run the Azure Functions entrypoint (timer trigger) locally."""
    # Import here so logging/env is configured first
    from ticket_canary_function.__init__ import main as azure_function_main
    # Our function doesn't use the TimerRequest argument, so pass None
    azure_function_main(None)


def parse_ids_from_env() -> List[str]:
    raw = os.getenv("LOCAL_TEST_TICKET_IDS")
    if not raw:
        return ["3012", "2321", "2207", "3342"]
    raw = raw.strip()
    # Try JSON first
    if raw.startswith("["):
        try:
            items = json.loads(raw)
            return [str(x) for x in items]
        except Exception:
            pass
    # Fallback to comma-separated
    return [s.strip() for s in raw.split(",") if s.strip()]


def run_local_test_by_ids():
    """Fetch specific tickets by ID and print AI responses (no date filtering)."""
    from agidesk import AgideskAPI
    from ticket_canary_function.__init__ import call_openai_simplified

    account_id = os.getenv("AGIDESK_ACCOUNT_ID")
    app_key = os.getenv("AGIDESK_APP_KEY")
    if not account_id or not app_key:
        logging.error("AGIDESK_ACCOUNT_ID and AGIDESK_APP_KEY are required for local test.")
        return

    ids = parse_ids_from_env()
    logging.info(f"Running local test for ticket IDs: {ids}")

    agi = AgideskAPI(account_id=account_id, app_key=app_key)

    for tid in ids:
        try:
            issue = agi.get_issue(tid)
            if not issue:
                logging.warning(f"Ticket {tid}: not found or API error.")
                continue
            logging.info(f"Ticket {issue.id} — {issue.title}")
            ai_summary = call_openai_simplified(issue)
            print(json.dumps({
                "ticket_id": issue.id,
                "title": issue.title,
                "ai_summary": ai_summary,
            }, ensure_ascii=False, indent=2))
        except Exception as e:
            logging.error(f"Error processing ticket {tid}: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_local_env_from_settings()
    # By default, run local test mode that fetches specific IDs and prints AI.
    # Set RUN_TIMER=1 to execute the full timer-trigger pipeline instead.
    if os.getenv("RUN_TIMER"):
        run_function_timer()
    else:
        run_local_test_by_ids()
