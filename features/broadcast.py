"""Admin /update: announce a bot update to every user.

    /update <text>   admin only; shows a preview with the recipient count
    ✅ Send / ✖️ Cancel buttons (callbacks bcast_send / bcast_cancel)

Nothing is sent until the admin taps Send. The text goes out exactly as
typed, as plain text, so Telegram formatting characters can't break it.
Recipients: every chat in user_settings or alerts. Sending runs in its own
thread at ~20 messages/second, under Telegram's 30/s bot limit, and the
admin gets a sent / blocked / failed summary at the end.

How to write the text: see "Update announcements" in CLAUDE.md.
"""
import logging
import threading
import time

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

logger = logging.getLogger(__name__)

_SEND_GAP_S = 0.05
_PENDING_KEY = "broadcast_text"
# Telegram's limit for one text message.
_MAX_CHARS = 4096


def recipients(cursor):
    cursor.execute(
        """SELECT chat_id FROM user_settings
           UNION
           SELECT chat_id FROM alerts"""
    )
    return sorted(row["chat_id"] for row in cursor.fetchall())


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
    def __init__(self, get_conn, return_conn, admin_ids):
        self.get_conn = get_conn
        self.return_conn = return_conn
        self.admin_ids = admin_ids

    def _count(self):
        conn = self.get_conn()
        try:
            ids = recipients(conn.cursor())
            conn.commit()
            return ids
        finally:
            self.return_conn(conn)

    def command(self, update, context):
        if update.effective_user.id not in self.admin_ids:
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
        context.user_data[_PENDING_KEY] = text
        n = len(self._count())
        update.message.reply_text(
            f"Preview. This goes to {n} users exactly as shown:\n\n{text}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Send to {n}", callback_data="bcast_send"),
                InlineKeyboardButton("✖️ Cancel", callback_data="bcast_cancel"),
            ]]),
            disable_web_page_preview=True,
        )

    def confirm(self, update, context):
        query = update.callback_query
        if update.effective_user.id not in self.admin_ids:
            query.answer()
            return
        text = context.user_data.pop(_PENDING_KEY, None)
        if query.data == "bcast_cancel" or not text:
            query.answer("Cancelled" if text else "Nothing to send. Run /update again.")
            query.edit_message_reply_markup(reply_markup=None)
            return
        chat_ids = self._count()
        query.answer(f"Sending to {len(chat_ids)} users…")
        query.edit_message_reply_markup(reply_markup=None)
        admin_chat = query.message.chat_id
        bot = context.bot

        def run():
            sent, blocked, failed = send_all(bot, chat_ids, text)
            logger.info(f"Update sent: {sent} sent, {blocked} blocked, {failed} failed")
            try:
                bot.send_message(
                    chat_id=admin_chat,
                    text=f"📢 Update sent to {sent} users. Blocked the bot: {blocked}. Failed: {failed}.",
                )
            except telegram.error.TelegramError:
                pass

        threading.Thread(target=run, name="update-broadcast", daemon=True).start()


def register(dispatcher, *, get_conn, return_conn, admin_ids, group):
    h = _Handlers(get_conn, return_conn, admin_ids)
    dispatcher.add_handler(CommandHandler("update", h.command), group=group)
    dispatcher.add_handler(CallbackQueryHandler(h.confirm, pattern=r"^bcast_(send|cancel)$"), group=group)
