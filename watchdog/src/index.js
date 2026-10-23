// JobQuest watchdog: tells the owner when the bot goes silent.
//
// The bot reports its own problems (features/ops_alerts.py), but not when the
// server, the bot process or its network is down, or when rounds stop
// finishing. It POSTs /beat every 5 min with the time its last round finished;
// a cron here checks those beats and messages the owner in Telegram, in the
// same one-line style as the bot's own reports:
//
//   🚨 JobQuest · 17:40
//   ⚠️ No sign of life from the bot for 35 min: the server, bot or network is down
//
//   ✅ JobQuest · 18:05
//   ✅ The bot is back (silent 60 min)
//
// A problem is repeated at most every 6 h while it lasts.
// Secrets (wrangler secret put): WATCHDOG_SECRET, TELEGRAM_BOT_TOKEN.

const SILENT_MIN = 30;          // no beat for this long → server/bot/network down
const ROUND_STALE_MIN = 120;    // beats arrive but no round finished for this long
const REPEAT_MIN = 6 * 60;

const nowMs = () => Date.now();
const minutes = (ms) => Math.round(ms / 60000);
const duration = (ms) => {
  const m = Math.max(1, minutes(ms));
  return m < 120 ? `${m} min` : `${Math.floor(m / 60)} h` + (m % 60 ? ` ${m % 60} min` : "");
};
const clock = (ms) =>
  new Date(ms).toLocaleTimeString("de-DE", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit" });

async function load(env) {
  return (await env.STATE.get("state", "json")) || {};
}

async function save(env, state) {
  await env.STATE.put("state", JSON.stringify(state));
}

async function send(env, head, lines) {
  if (!lines.length) return;
  const text = `${head} <b>JobQuest</b> · ${clock(nowMs())}\n` + lines.join("\n");
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.OWNER_CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  if (!resp.ok) console.log(`Telegram refused: ${resp.status} ${await resp.text()}`);
}

// Open a problem that began at `since` (or repeat it after REPEAT_MIN).
// Returns the line to send, or null.
function raise(state, kind, line, since, now) {
  const p = (state.open ||= {})[kind];
  if (!p) {
    state.open[kind] = { since, sent: now };
    return `⚠️ ${line}`;
  }
  if (now - p.sent >= REPEAT_MIN * 60000) {
    p.sent = now;
    return `⚠️ ${line} (since ${clock(p.since)})`;
  }
  return null;
}

function clear(state, kind, line, now) {
  const p = (state.open || {})[kind];
  if (!p) return null;
  delete state.open[kind];
  return `✅ ${line} (${kind === "silent" ? "silent" : "stuck"} ${duration(now - p.since)})`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/beat") return new Response("not found", { status: 404 });
    if (request.headers.get("Authorization") !== `Bearer ${env.WATCHDOG_SECRET}`)
      return new Response("unauthorized", { status: 401 });
    let body = {};
    try { body = await request.json(); } catch { /* an empty beat still counts */ }

    const now = nowMs();
    const state = await load(env);
    const back = [clear(state, "silent", "The bot is back", now)];
    state.lastBeat = now;
    if (Number.isFinite(body.round_end) && body.round_end > 0) state.roundEnd = body.round_end * 1000;
    if (state.roundEnd && now - state.roundEnd < ROUND_STALE_MIN * 60000)
      back.push(clear(state, "rounds", "Alert rounds are finishing again", now));
    await save(env, state);
    await send(env, "✅", back.filter(Boolean));
    return new Response("ok");
  },

  async scheduled(event, env) {
    const now = nowMs();
    const state = await load(env);
    if (!state.lastBeat) return;   // never heard from the bot yet: nothing to compare with
    const lines = [];
    const silentFor = now - state.lastBeat;
    if (silentFor >= SILENT_MIN * 60000) {
      lines.push(raise(state, "silent",
        `No sign of life from the bot for ${duration(silentFor)}: the server, bot or network is down`, state.lastBeat, now));
    } else if (state.roundEnd && now - state.roundEnd >= ROUND_STALE_MIN * 60000) {
      lines.push(raise(state, "rounds",
        `The bot is running but no alert round has finished for ${duration(now - state.roundEnd)}: users get no jobs`,
        state.roundEnd, now));
    }
    await save(env, state);
    await send(env, "🚨", lines.filter(Boolean));
  },
};
