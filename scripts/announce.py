"""Send an update announcement to every JobQuest user. Run it on the VPS.

    cd /root/Job-Search-TG
    .venv/bin/python scripts/announce.py --file msg.txt          preview, sends nothing
    .venv/bin/python scripts/announce.py --file msg.txt --test   send only to the owner, to check the look
    .venv/bin/python scripts/announce.py --file msg.txt --send   send to every user

The text is Telegram HTML (<b>bold</b>); see features/broadcast.py.

The preview prints the recipient count, the exact text, and whether the spam
guards (features/broadcast.py) allow a send right now. Exit codes: 0 sent or
preview OK, 1 refused by a guard, 2 bad input or config.

How to write the text: see "Update announcements" in CLAUDE.md.
"""
import argparse
import os
import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from features import broadcast  # noqa: E402

OWNER_ID = 7744296624   # Alwin; recorded as the sender in `broadcasts`
OWNER_NAME = "Alwin"


def connect():
    """Same settings as bot.py: DATABASE_URL, else the POSTGRES_* variables."""
    url = os.getenv("DATABASE_URL")
    if url:
        u = urllib.parse.urlparse(url)
        cfg = dict(host=u.hostname, port=u.port or 5432, dbname=u.path[1:],
                   user=u.username, password=u.password)
    else:
        cfg = dict(host=os.getenv("POSTGRES_HOST", "localhost"),
                   port=int(os.getenv("POSTGRES_PORT", 5432)),
                   dbname=os.getenv("POSTGRES_DB", "job_alerts"),
                   user=os.getenv("POSTGRES_USER", "postgres"),
                   password=os.getenv("POSTGRES_PASSWORD", ""))
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **cfg)


def main():
    ap = argparse.ArgumentParser(description="Announce a JobQuest update to every user.")
    ap.add_argument("--file", required=True, help="UTF-8 text file with the announcement")
    ap.add_argument("--send", action="store_true", help="actually send (default: preview only)")
    ap.add_argument("--test", action="store_true",
                    help="send only to the owner; not recorded, no cooldown")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    text = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
    if not text:
        print("The file is empty.")
        return 2
    if len(text) > broadcast.MAX_CHARS:
        print(f"Too long: {len(text)} characters, Telegram allows {broadcast.MAX_CHARS}.")
        return 2
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env.")
        return 2

    conn = connect()
    try:
        cur = conn.cursor()
        chat_ids = broadcast.recipients(cur)
        reason = broadcast.refusal(cur, text)
        conn.rollback()
        print(f"Recipients: {len(chat_ids)}")
        print("-" * 60)
        print(text)
        print("-" * 60)
        if args.test:
            sent, _, _ = broadcast.send_all(token, [OWNER_ID], text)
            print("✅ Test copy sent to the owner." if sent else "⛔ Test copy failed (see the warning above).")
            return 0 if sent else 2
        if reason:
            print(f"⛔ Not allowed now. {reason}")
            return 1
        if not args.send:
            print("Preview only. Run again with --send to send it.")
            return 0

        bid, reason = broadcast.claim(cur, text, OWNER_ID, OWNER_NAME)
        conn.commit()
        if bid is None:
            print(f"⛔ Not sent. {reason}")
            return 1
        sent, blocked, failed = broadcast.send_all(token, chat_ids, text)
        broadcast.finish(cur, bid, sent, blocked, failed)
        conn.commit()
        print(f"📢 Update {bid} sent to {sent} users. Blocked the bot: {blocked}. Failed: {failed}.")
        print(f"Next update allowed in {broadcast.COOLDOWN_HOURS} h.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
