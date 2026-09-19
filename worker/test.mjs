// assert-based self-check for the Worker: `node worker/test.mjs`
import assert from "node:assert/strict";
import worker from "./worker.js";

const env = { TELEGRAM_BOT_TOKEN: "tok", GITHUB_TOKEN: "ghp", WEBHOOK_SECRET: "s3", OWNER_CHAT_ID: "42", REPO: "me/repo" };
let calls = [], githubStatus = 204;
globalThis.fetch = async (url, init) => {
  calls.push({ url, body: init?.body ? JSON.parse(init.body) : null, headers: init?.headers });
  return new Response(null, { status: url.includes("github.com") ? githubStatus : 200 });
};

const send = (body, secret = "s3", method = "POST") =>
  worker.fetch(new Request("https://w.example/", { method, headers: { "X-Telegram-Bot-Api-Secret-Token": secret }, body: method === "POST" ? body : undefined }), env);
const msg = (chat, text) => JSON.stringify({ message: { chat: { id: chat }, text } });
const reset = () => { calls = []; githubStatus = 204; };

// wrong or missing secret, or a GET: refused, nothing called
for (const res of [await send(msg(42, "/status"), "wrong"), await send(msg(42, "/status"), "s3", "GET")]) assert.equal(res.status, 403);
assert.equal(calls.length, 0);

// a stranger's /status: answered 200 (so Telegram doesn't retry) but nothing is sent or started
assert.equal((await send(msg(999, "/status"))).status, 200);
assert.equal(calls.length, 0, "strangers get nothing");

// the owner's /status: instant acknowledgement, then the GitHub workflow is started
reset();
assert.equal((await send(msg(42, "/status"))).status, 200);
assert.equal(calls.length, 2);
assert.match(calls[0].url, /api\.telegram\.org\/bottok\/sendMessage/);
assert.equal(calls[0].body.chat_id, "42");
assert.match(calls[0].body.text, /about a minute/);
assert.equal(calls[1].url, "https://api.github.com/repos/me/repo/actions/workflows/telegram-status.yml/dispatches");
assert.equal(calls[1].headers.Authorization, "Bearer ghp");
assert.deepEqual(calls[1].body, { ref: "main" });

// group-style command and capitals
reset();
await send(msg(42, "/Status@HelmBot"));
assert.equal(calls.length, 2);

// anything else: a hint, no workflow
reset();
await send(msg(42, "hello"));
assert.equal(calls.length, 1);
assert.match(calls[0].body.text, /Send \/status/);

// GitHub refuses: the user is told, still 200
reset(); githubStatus = 403;
assert.equal((await send(msg(42, "/status"))).status, 200);
assert.equal(calls.length, 3);
assert.match(calls[2].body.text, /GitHub answered 403/);

// garbage body: still 200
reset();
assert.equal((await send("not json")).status, 200);

console.log("ok");
