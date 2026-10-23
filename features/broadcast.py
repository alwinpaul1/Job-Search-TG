"""Owner-only /update: announce a bot update to every user, without spamming them.

    /update <text>   owner only (OWNER_USER_ID in bot.py; other admins can't);
                     saves a pending update and shows a preview
    ✅ Send / ✖️ Cancel buttons (callbacks bcast_send_<id> / bcast_cancel_<id>)

Spam guards, enforced in the database so they hold across double taps,
restarts and any future second sender:
- each preview is a `broadcasts` row; Send claims it once (pending -> sending)
  under a table lock, so repeated or simultaneous taps send it once;
- after any update goes out, no other one is allowed for COOLDOWN_HOURS;
- the exact same text is never sent twice.
The owner gets a sent / blocked / failed summary when it finishes.

The text goes out exactly as typed, as plain text. Recipients: every chat in
user_settings or alerts, at ~20 messages/second in a background thread.
How to write the text: see "Update announcements" in CLAUDE.md.
"""
import logging
import threading
import time

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

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
_SEND_GAP_S = 0.05
# Telegram's limit for one text message.
_MAX_CHARS = 4096


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
        wait = COOLDOWN_HOURS - ago
        who = row["admin_name"] or "Someone"
        return (f"{who} sent an update {ago:.0f} h ago. To avoid spamming users, "
                f"the next one is allowed in {wait:.0f} h.")
    cursor.execute(
        """SELECT 1 FROM broadcasts
           WHERE status IN ('sending', 'sent') AND text = %s AND id IS DISTINCT FROM %s
           LIMIT 1""",
        (text, exclude_id),
    )
    if cursor.fetchone():
        return "This exact message was already sent before."
    return None


def send_all(bot, chat_ids, text, gap_s=_SEND_GAP_S):
    """Send `text` to each chat. Returns (sent, blocked, failed)."""
    sent = blocked = failed = 0
    for chat_id in chat_ids:
        for attempt in (1, 2):
            try:
                bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
                sent += 1
            except telegram.error.RetryAfter as e:
                if attempt == 1:
                    time.sleep(float(getattr(e, "retry_after", 5)) + 1)
                    continue
                failed += 1
            except telegram.error.Unauthorized:
                blocked += 1   # user blocked the bot or deleted the chat
            except telegram.error.TelegramError as e:
                failed += 1
                logger.warning(f"Update to {chat_id} failed: {e}")
            break
        time.sleep(gap_s)
    return sent, blocked, failed


class _Handlers:
    def __init__(self, get_conn, return_conn, owner_ids):
        self.get_conn = get_conn
        self.return_conn = return_conn
        self.owner_ids = owner_ids

    def _db(self, fn):
        conn = self.get_conn()
        try:
            result = fn(conn.cursor())
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            self.return_conn(conn)

    def command(self, update, context):
        user = update.effective_user
        if user.id not in self.owner_ids:
            return
        # Everything after "/update" (space or newline), line breaks kept.
        parts = (update.message.text or "").split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            update.message.reply_text(
                "Usage: /update <message>\n\n"
                "Write it short and plain. You'll see a preview before anything is sent."
            )
            return
        if len(text) > _MAX_CHARS:
            update.message.reply_text(f"Too long: {len(text)} characters, Telegram allows {_MAX_CHARS}.")
            return

        def work(cur):
            reason = refusal(cur, text)
            if reason:
                return reason, None, 0
            cur.execute(
                "INSERT INTO broadcasts (admin_id, admin_name, text) VALUES (%s, %s, %s) RETURNING id",
                (user.id, user.first_name, text),
            )
            return None, cur.fetchone()["id"], len(recipients(cur))

        reason, bid, n = self._db(work)
        if reason:
            update.message.reply_text(f"⛔ Not sent. {reason}")
            return
        update.message.reply_text(
            f"Preview. This goes to {n} users exactly as shown:\n\n{text}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Send to {n}", callback_data=f"bcast_send_{bid}"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"bcast_cancel_{bid}"),
            ]]),
            disable_web_page_preview=True,
        )

    def confirm(self, update, context):
        query = update.callback_query
        user = update.effective_user
        if user.id not in self.owner_ids:
            query.answer()
            return
        action, bid = context.match.group(1), int(context.match.group(2))

        def work(cur):
            # One claim at a time across dispatcher threads and repeated taps.
            cur.execute("LOCK TABLE broadcasts IN SHARE ROW EXCLUSIVE MODE")
            cur.execute("SELECT status, text FROM broadcasts WHERE id = %s", (bid,))
            row = cur.fetchone()
            if not row or row["status"] != "pending":
                return "done", None, None
            if action == "cancel":
                cur.execute("UPDATE broadcasts SET status = 'cancelled' WHERE id = %s", (bid,))
                return "cancelled", None, None
            reason = refusal(cur, row["text"], exclude_id=bid)
            if reason:
                cur.execute("UPDATE broadcasts SET status = 'refused' WHERE id = %s", (bid,))
                return "refused", reason, None
            cur.execute(
                "UPDATE broadcasts SET status = 'sending', sent_at = NOW() WHERE id = %s", (bid,)
            )
            return "claimed", row["text"], recipients(cur)

        outcome, info, chat_ids = self._db(work)
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except telegram.error.BadRequest:
            pass
        if outcome == "done":
            query.answer("This update was already sent or cancelled.")
            return
        if outcome == "cancelled":
            query.answer("Cancelled")
            return
        if outcome == "refused":
            query.answer("Not sent")
            query.message.reply_text(f"⛔ Not sent. {info}")
            return

        text = info
        query.answer(f"Sending to {len(chat_ids)} users…")
        bot = context.bot
        sender = user.first_name or "The owner"

        def run():
            sent, blocked, failed = send_all(bot, chat_ids, text)
            logger.info(f"Update {bid} by {user.id}: {sent} sent, {blocked} blocked, {failed} failed")
            try:
                self._db(lambda cur: cur.execute(
                    "UPDATE broadcasts SET status = 'sent', sent = %s, blocked = %s, failed = %s WHERE id = %s",
                    (sent, blocked, failed, bid),
                ))
            except Exception as e:
                logger.error(f"Could not record update {bid} as sent: {e}")
            summary = (f"📢 {sender} sent an update to {sent} users. "
                       f"Blocked the bot: {blocked}. Failed: {failed}.\n"
                       f"Next update allowed in {COOLDOWN_HOURS} h.")
            for owner_id in self.owner_ids:
                try:
                    bot.send_message(chat_id=owner_id, text=summary)
                except telegram.error.TelegramError:
                    pass

        threading.Thread(target=run, name="update-broadcast", daemon=True).start()


def register(dispatcher, *, get_conn, return_conn, owner_ids, group):
    h = _Handlers(get_conn, return_conn, owner_ids)
    dispatcher.add_handler(CommandHandler("update", h.command), group=group)
    dispatcher.add_handler(
        CallbackQueryHandler(h.confirm, pattern=r"^bcast_(send|cancel)_(\d+)$"), group=group
    )
