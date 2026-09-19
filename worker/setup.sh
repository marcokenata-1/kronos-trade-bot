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

# prove the token works before deploying anything: this really starts the status workflow, so a message
# should reach your Telegram in about a minute. The token goes to curl on stdin, not on the command line.
code=$(printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" | curl -s -o /dev/null -w "%{http_code}" -K - -X POST \
  -H "Accept: application/vnd.github+json" -H "User-Agent: helm-setup" -d '{"ref":"main"}' \
  "https://api.github.com/repos/marcokenata-1/kronos-trade-bot/actions/workflows/telegram-status.yml/dispatches")
case $code in
  204) echo "GitHub accepted the token: a status message should reach Telegram in about a minute." ;;
  401) echo "GitHub does not recognise that token (401). Copy the WHOLE token (it starts with github_pat_) and run this again."; exit 1 ;;
  403|404) echo "The token is valid but can't start workflows on this repo ($code). It needs access to kronos-trade-bot with Actions: read and write."; exit 1 ;;
  *) echo "Unexpected answer from GitHub ($code)."; exit 1 ;;
esac
WEBHOOK_SECRET=$(../.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')

# first deploy is not piped, so wrangler can ask you to pick a workers.dev subdomain (needed once per account);
# the second one is piped to capture the URL, and is quick because nothing has changed
npx --yes wrangler deploy
URL=$(npx wrangler deploy 2>&1 | tee /dev/stderr | grep -o 'https://[^ ]*\.workers\.dev' | head -1 || true)
[ -n "$URL" ] || {
  echo "Could not find the Worker's URL in wrangler's output."
  echo "If it asked you to register a workers.dev subdomain: do that once in the Cloudflare dashboard"
  echo "(Workers & Pages, then the onboarding page), then run this script again."
  exit 1
}
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
