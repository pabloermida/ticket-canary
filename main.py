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


def _is_truthy_env(name: str) -> bool:
    v = os.getenv(name, "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def run_local_test_by_ids():
    """Fetch specific tickets by ID and print AI responses.

    Optionally, add the AI summary as a comment in Agidesk when
    `LOCAL_TEST_WRITE_COMMENTS=1` (or MODE=production).
    """
    from agidesk import AgideskAPI
    from ticket_canary_function.__init__ import (
        call_openai_simplified,
        build_ai_comment_html,
        notify_teams,
        notify_teams_adaptive,
        build_ticket_adaptive_card,
        build_teams_text_message,
    )

    account_id = os.getenv("AGIDESK_ACCOUNT_ID")
    app_key = os.getenv("AGIDESK_APP_KEY")
    if not account_id or not app_key:
        logging.error("AGIDESK_ACCOUNT_ID and AGIDESK_APP_KEY are required for local test.")
        return

    ids = parse_ids_from_env()
    logging.info(f"Running local test for ticket IDs: {ids}")

    agi = AgideskAPI(account_id=account_id, app_key=app_key)

    allow_write = (os.getenv("MODE", "development").strip().lower() == "production") or _is_truthy_env("LOCAL_TEST_WRITE_COMMENTS")
    send_teams = _is_truthy_env("LOCAL_TEST_SEND_TEAMS")
    style = os.getenv("TEAMS_MESSAGE_STYLE", "card").strip().lower()
    if allow_write:
        logging.info("Local test is configured to WRITE comments to Agidesk.")
    else:
        logging.info("Local test is in READ-ONLY mode (no Agidesk comments). Set LOCAL_TEST_WRITE_COMMENTS=1 to enable writing.")
    if send_teams:
        logging.info(
            f"Local test will SEND Teams notifications (style='{style}'). Set TEAMS_MESSAGE_STYLE=text to use plain text."
        )
    else:
        logging.info("Local test will NOT send Teams notifications. Set LOCAL_TEST_SEND_TEAMS=1 to enable.")

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
            if send_teams:
                try:
                    fallback_text = build_teams_text_message(issue)
                    if style == "text":
                        ok = notify_teams(fallback_text)
                        if ok:
                            logging.info(f"Teams text message sent for ticket {issue.id}.")
                        else:
                            logging.error(f"Failed to send Teams text message for ticket {issue.id}.")
                    else:
                        card = build_ticket_adaptive_card(issue, ai_summary)
                        ok = notify_teams_adaptive(card, fallback_message=fallback_text)
                        if ok:
                            logging.info(f"Teams Adaptive Card sent for ticket {issue.id}.")
                        else:
                            logging.error(f"Failed to send Teams Adaptive Card for ticket {issue.id}.")
                except Exception as e:
                    logging.error(f"Error sending Teams notification for ticket {issue.id}: {e}")
            if allow_write:
                try:
                    html = build_ai_comment_html(ai_summary)
                    resp = agi.add_comment(issue.id, html)
                    logging.info(f"Comment posted to Agidesk for ticket {issue.id}.")
                    if resp:
                        logging.debug(json.dumps(resp, ensure_ascii=False))
                except Exception as e:
                    logging.error(f"Failed to post comment for ticket {issue.id}: {e}")
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
