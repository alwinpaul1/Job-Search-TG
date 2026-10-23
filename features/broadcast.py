"""Update announcements to every user, sent from the server, never from the chat.

There is no /update command. Announcements go out with scripts/announce.py on
the VPS (run from a Claude Code session), so nobody can send one from Telegram:

    .venv/bin/python scripts/announce.py --file msg.txt          preview, sends nothing
    .venv/bin/python scripts/announce.py --file msg.txt --send   send to every user

Spam guards, enforced in the `broadcasts` table:
- a send claims its row under a table lock, so two runs at once send once;
- after an update goes out, no other one is allowed for COOLDOWN_HOURS;
- the exact same text is never sent twice.

The text goes out exactly as written, as plain text. Recipients: every chat in
user_settings or alerts, at ~20 messages/second. How to write the text: see
"Update announcements" in CLAUDE.md.

This module imports no telegram code: the script runs outside the bot, without
bot.py's vendored-urllib3 shim, and talks to the Bot API over HTTPS.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS broadcasts (
        id SERIAL PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        admin_name TEXT,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT NOW(),
        sent_at TIMESTAMP,
        sent INTEGER,
        blocked INTEGER,
        failed INTEGER
    )
"""

COOLDOWN_HOURS = 24
MAX_CHARS = 4096          # Telegram's limit for one text message
_SEND_GAP_S = 0.05
_API = "https://api.telegram.org/bot{token}/sendMessage"


def recipients(cursor):
    cursor.execute(
        """SELECT chat_id FROM user_settings
           UNION
           SELECT chat_id FROM alerts"""
    )
    return sorted(row["chat_id"] for row in cursor.fetchall())


def refusal(cursor, text, exclude_id=None):
    """Why an update with this text may not go out now, or None if it may."""
    cursor.execute(
        """SELECT admin_name, EXTRACT(EPOCH FROM NOW() - sent_at) / 3600 AS hours_ago
           FROM broadcasts
           WHERE status IN ('sending', 'sent')
             AND sent_at > NOW() - make_interval(hours => %s)
             AND id IS DISTINCT FROM %s
           ORDER BY sent_at DESC LIMIT 1""",
        (COOLDOWN_HOURS, exclude_id),
    )
    row = cursor.fetchone()
    if row:
        ago = float(row["hours_ago"])
        who = row["admin_name"] or "Someone"
        return (f"{who} sent an update {ago:.0f} h ago. To avoid spamming users, "
                f"the next one is allowed in {COOLDOWN_HOURS - ago:.0f} h.")
    cursor.execute(
        """SELECT 1 FROM broadcasts
           WHERE status IN ('sending', 'sent') AND text = %s AND id IS DISTINCT FROM %s
           LIMIT 1""",
        (text, exclude_id),
    )
    if cursor.fetchone():
        return "This exact message was already sent before."
    return None


def claim(cursor, text, sender_id, sender_name):
    """Reserve a send: returns (broadcast id, None), or (None, reason) if refused.

    Commit right after, before sending, so the cooldown holds even if the
    send is interrupted."""
    cursor.execute("LOCK TABLE broadcasts IN SHARE ROW EXCLUSIVE MODE")
    reason = refusal(cursor, text)
    if reason:
        return None, reason
    cursor.execute(
        """INSERT INTO broadcasts (admin_id, admin_name, text, status, sent_at)
           VALUES (%s, %s, %s, 'sending', NOW()) RETURNING id""",
        (sender_id, sender_name, text),
    )
    return cursor.fetchone()["id"], None


def finish(cursor, broadcast_id, sent, blocked, failed):
    cursor.execute(
        "UPDATE broadcasts SET status = 'sent', sent = %s, blocked = %s, failed = %s WHERE id = %s",
        (sent, blocked, failed, broadcast_id),
    )


def send_all(token, chat_ids, text, gap_s=_SEND_GAP_S, post=requests.post):
    """Send `text` to each chat. Returns (sent, blocked, failed)."""
    url = _API.format(token=token)
    sent = blocked = failed = 0
    for chat_id in chat_ids:
        for attempt in (1, 2):
            try:
                resp = post(url, json={"chat_id": chat_id, "text": text,
                                       "disable_web_page_preview": True}, timeout=20)
                body = resp.json()
            except (requests.RequestException, ValueError) as e:
                failed += 1
                logger.warning(f"Update to {chat_id} failed: {type(e).__name__}")
                break
            if body.get("ok"):
                sent += 1
            elif body.get("error_code") == 429 and attempt == 1:
                time.sleep(float(body.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            elif body.get("error_code") == 403:
                blocked += 1   # user blocked the bot or deleted the chat
            else:
                failed += 1
                logger.warning(f"Update to {chat_id} failed: {body.get('description')}")
            break
        time.sleep(gap_s)
    return sent, blocked, failed
