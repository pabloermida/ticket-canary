# ticket-canary

Add environment variables:

- Cria venv local.
- export AGIDESK_ACCOUNT_ID="infiniit"
- export AGIDESK_APP_KEY="SEU_TOKEN"
- export TEAMS_WEBHOOK_URL="https://...incomingwebhook..."
- export OPENAI_API_KEY="sk-..."
- export OPENAI_MODEL="gpt-4.1-mini"
- export ROUTER_PROCESSED_TAG="router:processed"
- export POLL_INTERVAL_SEC="300"  # seconds
- export FETCH_TIME_SECONDS="300"  # seconds
- export MODE="development" # Use 'production' to enable write operations
- python3 main.py
