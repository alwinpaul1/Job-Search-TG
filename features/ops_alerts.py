"""Problem reports to the master admin in Telegram (owner's request, 2026-10-23).

Each problem has a kind. The first report of a kind goes out at once; while it
lasts it is repeated at most every REPEAT_S; when it clears, one "✅ fixed"
message follows. So a problem that lasts all day sends about four messages,
not one per cycle.

Reported:
- the bot restarting itself (scheduler stuck), or starting again after it
  stopped without a clean shutdown (crash, out-of-memory kill);
- an alert check failing, LinkedIn or Indeed failing for most alerts in a
  cycle, many LinkedIn job pages not loading (jobs wait for their description);
- Jev not answering (Clef judges instead), and Jev and Clef both down (jobs wait).

Not reported, because the bot handles them on its own: single LinkedIn 429s,
memory clean-ups, one alert finding nothing.

Messages go straight to the Bot API over HTTPS, so they work from any thread
and during a restart. Nothing here raises: a failed report is only logged.
"""
import html
import logging
import os
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

REPEAT_S = 6 * 3600
_API = "https://api.telegram.org/bot{token}/sendMessage"
_CLEAN_EXIT_FILE = ".clean_exit"

_owner = [None]
_lock = threading.Lock()
_open = {}          # kind -> {"since": epoch, "last_sent": epoch, "title": str}
_cycle = None       # notes collected during one alert-check cycle


def setup(owner_id):
    _owner[0] = owner_id


def _now():
    return datetime.now().strftime("%d %b %H:%M")


def _send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not _owner[0]:
        return
    try:
        resp = requests.post(_API.format(token=token), timeout=10, json={
            "chat_id": _owner[0], "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": True})
        if not resp.json().get("ok"):
            logger.warning(f"Admin problem report refused: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Admin problem report failed: {e}")


def _block(icon, title, lines):
    body = "\n".join(lines)
    return f"{icon} <b>{html.escape(title)}</b>\n{body}" if body else f"{icon} <b>{html.escape(title)}</b>"


def _duration(seconds):
    m = int(seconds // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def problem(kind, title, lines=()):
    """Report a problem now, unless the same kind was reported < REPEAT_S ago."""
    _send_many([(kind, title, list(lines))])


def resolved(*items):
    """Say reported problems are over, in one message: items are
    (kind, title, lines). Nothing is sent for a kind that was never reported."""
    _resolve_many([(kind, title, list(lines)) for kind, title, lines in items])


def _send_many(items):
    blocks = []
    now = time.time()
    with _lock:
        for kind, title, lines in items:
            state = _open.get(kind)
            if state and now - state["last_sent"] < REPEAT_S:
                continue
            if state:
                lines = lines + [f"<i>Ongoing since {datetime.fromtimestamp(state['since']).strftime('%d %b %H:%M')}.</i>"]
                state["last_sent"] = now
            else:
                _open[kind] = {"since": now, "last_sent": now, "title": title}
            blocks.append(_block("⚠️", title, lines))
    if blocks:
        _send(f"🚨 <b>JobQuest problem</b> · {_now()}\n\n" + "\n\n".join(blocks))


def _resolve_many(items):
    blocks = []
    now = time.time()
    with _lock:
        for kind, title, lines in items:
            state = _open.pop(kind, None)
            if state:
                blocks.append(_block("✅", title, lines + [f"<i>Lasted {_duration(now - state['since'])}.</i>"]))
    if blocks:
        _send(f"✅ <b>JobQuest fixed</b> · {_now()}\n\n" + "\n\n".join(blocks))


# --- Start and stop -------------------------------------------------------

def started(version=""):
    """Call once at startup. Reports an unclean previous stop."""
    clean = os.path.exists(_CLEAN_EXIT_FILE)
    try:
        if clean:
            os.remove(_CLEAN_EXIT_FILE)
    except OSError:
        pass
    if not clean:
        _send(f"🚨 <b>JobQuest problem</b> · {_now()}\n\n" + _block("⚠️", "The bot stopped unexpectedly", [
            "It crashed or was killed (for example out of memory) and has now started again.",
            "Alerts carry on from where they were; check the logs for the cause:",
            "<code>journalctl -u job-search-bot --since '-1h'</code>",
        ] + ([f"<i>Running {html.escape(version)}.</i>"] if version else [])))


def stopping_cleanly():
    """Call on every planned exit (systemd stop/restart, self-restart)."""
    try:
        with open(_CLEAN_EXIT_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except OSError as e:
        logger.warning(f"Could not write {_CLEAN_EXIT_FILE}: {e}")


def restarting_itself(reason):
    """The bot is about to restart itself: report it, then mark the exit as planned."""
    _send(f"🚨 <b>JobQuest problem</b> · {_now()}\n\n" + _block("⚠️", "The bot is restarting itself", [
        html.escape(reason), "It should be back within about a minute."]))
    stopping_cleanly()


# --- Alert-check cycle ------------------------------------------------------

def cycle_start(n_alerts):
    global _cycle
    _cycle = {"alerts": n_alerts, "failed": [], "linkedin_down": 0, "indeed_down": 0, "unreadable": 0}


def note_failed(alert_id, keywords, error):
    if _cycle is not None:
        _cycle["failed"].append((alert_id, keywords, error))


def note_board_down(board):
    if _cycle is not None:
        _cycle[f"{board}_down"] += 1


def note_unreadable(n):
    if _cycle is not None and n:
        _cycle["unreadable"] += n


def cycle_end(interrupted=False):
    """Report what went wrong in this cycle, and what is fixed since the last one."""
    global _cycle
    c, _cycle = _cycle, None
    if c is None or interrupted:
        return
    n = c["alerts"]
    found, cleared = [], []

    if c["failed"]:
        by_error = {}
        for aid, kw, err in c["failed"]:
            by_error.setdefault(err.splitlines()[0][:160] if err else "unknown error", []).append(f"#{aid} {kw}")
        lines = [f"{len(c['failed'])} of {n} alerts could not be checked this round:"]
        for err, which in list(by_error.items())[:4]:
            lines.append(f"• <code>{html.escape(err)}</code>\n  {html.escape(', '.join(which[:6]))}"
                         + (f" +{len(which) - 6} more" if len(which) > 6 else ""))
        lines.append("They are retried in the next round (every 30 min).")
        found.append(("alert_failed", "Alert checks failing", lines))
    else:
        cleared.append(("alert_failed", "Alert checks work again", []))

    for board, name in (("linkedin", "LinkedIn"), ("indeed", "Indeed")):
        down = c[f"{board}_down"]
        if n and down >= max(3, n // 2):
            found.append((board, f"{name} is blocking the bot", [
                f"Scraping {name} failed for {down} of {n} alerts this round, so those alerts got no {name} jobs.",
                "Usually a temporary block of the server's address; the bot keeps trying every round."]))
        else:
            cleared.append((board, f"{name} scraping works again", []))

    if c["unreadable"] >= 20:
        found.append(("postings", "LinkedIn job pages are not loading", [
            f"{c['unreadable']} jobs are waiting because their full description could not be read.",
            "They are not sent until Jev has read the whole job, and are retried every round."]))
    elif c["unreadable"] == 0:
        cleared.append(("postings", "LinkedIn job pages load again", []))

    if found:
        _send_many(found)
    if cleared:
        _resolve_many(cleared)
