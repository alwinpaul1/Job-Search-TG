"""Problem reports to the master admin in Telegram (owner's request, 2026-10-23).

One line per problem (owner, 2026-10-23: "simple, one line what's the issue"):

    🚨 JobQuest · 16:03
    ⚠️ LinkedIn is blocking the bot: 16 of 30 alerts got no LinkedIn jobs

    ✅ JobQuest · 16:33
    ✅ LinkedIn works again (was down 30 min)

Each problem has a kind. The first report of a kind goes out at once; while it
lasts it is repeated at most every REPEAT_S; when it clears, one ✅ line
follows. A problem that lasts all day sends about four messages.

Reported: alert checks failing, LinkedIn or Indeed failing for most alerts in a
round, many unreadable LinkedIn job pages, Jev down (Clef judging), Jev and
Clef both down, the bot restarting itself, and a start after an unclean stop
(crash, out-of-memory kill). Not reported, because the bot handles them:
single LinkedIn 429s, memory clean-ups, planned restarts.

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
_open = {}          # kind -> {"since": epoch, "last_sent": epoch}
_cycle = None       # notes collected during one alert-check round


def setup(owner_id):
    _owner[0] = owner_id


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


def _message(head, lines):
    return f"{head} <b>JobQuest</b> · {datetime.now().strftime('%H:%M')}\n" + "\n".join(lines)


def _duration(seconds):
    m = max(1, int(seconds // 60))
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def problem(*items):
    """Report problems, one line each: items are (kind, line). A kind already
    reported less than REPEAT_S ago is skipped."""
    lines = []
    now = time.time()
    with _lock:
        for kind, line in items:
            state = _open.get(kind)
            if state and now - state["last_sent"] < REPEAT_S:
                continue
            if state:
                state["last_sent"] = now
                line += f" (since {datetime.fromtimestamp(state['since']).strftime('%H:%M')})"
            else:
                _open[kind] = {"since": now, "last_sent": now}
            lines.append(f"⚠️ {line}")
    if lines:
        _send(_message("🚨", lines))


def resolved(*items):
    """Say reported problems are over, one line each: items are (kind, line).
    Nothing is sent for a kind that was never reported."""
    lines = []
    now = time.time()
    with _lock:
        for kind, line in items:
            state = _open.pop(kind, None)
            if state:
                lines.append(f"✅ {line} (was down {_duration(now - state['since'])})")
    if lines:
        _send(_message("✅", lines))


# --- Start and stop -------------------------------------------------------

def started():
    """Call once at startup. Reports an unclean previous stop."""
    clean = os.path.exists(_CLEAN_EXIT_FILE)
    if clean:
        try:
            os.remove(_CLEAN_EXIT_FILE)
        except OSError:
            pass
    else:
        _send(_message("🚨", ["⚠️ The bot crashed or was killed and has restarted"]))


def stopping_cleanly():
    """Call on every planned exit (systemd stop/restart, self-restart)."""
    try:
        with open(_CLEAN_EXIT_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except OSError as e:
        logger.warning(f"Could not write {_CLEAN_EXIT_FILE}: {e}")


def restarting_itself(reason):
    """The bot is about to restart itself: report it, then mark the exit as planned."""
    _send(_message("🚨", [f"⚠️ The bot is restarting itself: {html.escape(reason)}"]))
    stopping_cleanly()


# --- Alert-check round ------------------------------------------------------

def cycle_start(n_alerts):
    global _cycle
    _cycle = {"alerts": n_alerts, "failed": [], "linkedin_down": 0, "indeed_down": 0, "unreadable": 0}


def note_failed(alert_id, error):
    if _cycle is not None:
        _cycle["failed"].append((alert_id, error))


def note_board_down(board):
    if _cycle is not None:
        _cycle[f"{board}_down"] += 1


def note_unreadable(n):
    if _cycle is not None and n:
        _cycle["unreadable"] += n


def _short(error):
    """A few words for an exception, e.g. 'scraping failed' or 'KeyError'."""
    if "Scrape failed on all boards" in error:
        return "scraping failed"
    name, _, text = error.partition(":")
    text = text.strip()
    return f"{name}: {text[:50]}" if text else name


def cycle_end(interrupted=False):
    """Report what went wrong in this round, and what is fixed since the last one."""
    global _cycle
    c, _cycle = _cycle, None
    if c is None or interrupted:
        return
    n = c["alerts"]
    found, cleared = [], []

    if c["failed"]:
        by_error = {}
        for aid, err in c["failed"]:
            by_error.setdefault(_short(err), []).append(f"#{aid}")
        why = "; ".join(f"{html.escape(e, quote=False)} ({', '.join(ids)})" for e, ids in by_error.items())
        found.append(("alert_failed", f"{len(c['failed'])} of {n} alerts failed: {why}"))
    else:
        cleared.append(("alert_failed", "Alert checks work again"))

    for board, name in (("linkedin", "LinkedIn"), ("indeed", "Indeed")):
        down = c[f"{board}_down"]
        if n and down >= max(3, n // 2):
            found.append((board, f"{name} is blocking the bot: {down} of {n} alerts got no {name} jobs"))
        else:
            cleared.append((board, f"{name} works again"))

    if c["unreadable"] >= 20:
        found.append(("postings", f"LinkedIn job pages won't load: {c['unreadable']} jobs waiting"))
    elif c["unreadable"] == 0:
        cleared.append(("postings", "LinkedIn job pages load again"))

    if found:
        problem(*found)
    if cleared:
        resolved(*cleared)
