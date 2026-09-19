// Cloudflare Worker: receives Telegram's webhook, answers /status at once, and starts the GitHub
// "Telegram status" workflow, which sends the real status. Your Alpaca keys never leave GitHub.
// Secrets (set by setup.sh): TELEGRAM_BOT_TOKEN, GITHUB_TOKEN, WEBHOOK_SECRET, OWNER_CHAT_ID.

const say = (env, text) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.OWNER_CHAT_ID, text }),
  });

export default {
  async fetch(request, env) {
    // anyone can POST here: only Telegram knows the secret the webhook was registered with
    if (request.method !== "POST" || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    try {
      const message = (await request.json()).message;
      // only the owner's chat: anyone can message a bot, but only you may read your account
      if (message?.text && String(message.chat.id) === env.OWNER_CHAT_ID) {
        const command = message.text.trim().split(/\s/)[0].split("@")[0].toLowerCase();
        if (command !== "/status") {
          await say(env, "Send /status for a summary of the account.");
        } else {
          await say(env, "Fetching your status. This takes about a minute.");
          const res = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/telegram-status.yml/dispatches`, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${env.GITHUB_TOKEN}`,
              Accept: "application/vnd.github+json",
              "User-Agent": "helm-telegram-worker",
              "X-GitHub-Api-Version": "2022-11-28",
            },
            body: JSON.stringify({ ref: "main" }),
          });
          if (!res.ok) await say(env, `Could not start the status job: GitHub answered ${res.status}.`);
        }
      }
    } catch (e) {
      console.log("handler error:", e.message); // always answer 200, or Telegram retries the same message
    }
    return new Response("ok");
  },
};
