#!/bin/bash
# One-time setup of the Cloudflare Worker that answers /status. Run `npx wrangler login` first (opens your browser).
# Secrets come from ../.env and a hidden prompt; they are never printed or written to a file.
set -euo pipefail
cd "$(dirname "$0")"
env_value() { grep "^$1=" ../.env | head -1 | cut -d= -f2- || true; }
TELEGRAM_BOT_TOKEN=$(env_value TELEGRAM_BOT_TOKEN)
OWNER_CHAT_ID=$(env_value TELEGRAM_CHAT_ID)
[ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$OWNER_CHAT_ID" ] || { echo "Put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in ../.env first"; exit 1; }
read -rsp "GitHub token (fine-grained, this repo only, Actions: read and write): " GITHUB_TOKEN; echo
WEBHOOK_SECRET=$(openssl rand -hex 24)

URL=$(npx --yes wrangler deploy 2>&1 | tee /dev/stderr | grep -o 'https://[^ ]*\.workers\.dev' | head -1 || true)
[ -n "$URL" ] || { echo "Could not find the Worker's URL in wrangler's output"; exit 1; }
for name in TELEGRAM_BOT_TOKEN GITHUB_TOKEN WEBHOOK_SECRET OWNER_CHAT_ID; do
  printf %s "${!name}" | npx wrangler secret put "$name"
done

cd .. && WEBHOOK_URL=$URL WEBHOOK_SECRET=$WEBHOOK_SECRET .venv/bin/python - <<'PY'
import os, bot
print("webhook:", bot._telegram("setWebhook", url=os.environ["WEBHOOK_URL"], secret_token=os.environ["WEBHOOK_SECRET"], allowed_updates=["message"]))
print("menu:", bot._telegram("setMyCommands", commands=[{"command": "status", "description": "Account summary (about a minute)"}]))
print("description:", bot._telegram("setMyDescription", description="Your paper-trading account. Send /status for a summary; it takes about a minute."))
PY
echo "Done. Send /status to your bot."
