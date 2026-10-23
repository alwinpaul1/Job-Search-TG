"""Invite-only access: the bot works only for admins and invited users.

    /invite             admins only: a one-time link t.me/<bot>?start=inv_<code>,
                        valid INVITE_TTL_DAYS; the inviting admin is told when it's used
    /start inv_<code>   redeems it (opening the link sends exactly this)

A TypeHandler in dispatcher group -1 runs before every other handler and stops
updates from anyone else (DispatcherHandlerStop) after a short invite-only
reply that names the admins' @usernames, so people know whom to ask. Allowed IDs are cached and reloaded every RELOAD_S, so the gate makes no
DB call per update and keeps the last good list if the database blips.

The users already here when this shipped were copied into allowed_users once,
when the table was created. Deleting a user in /admin removes their access
(revoke).
"""
import logging
import re
import secrets
import threading
import time

import telegram
from telegram import Update
from telegram.ext import CommandHandler, DispatcherHandlerStop, TypeHandler

logger = logging.getLogger(__name__)

SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS invites (
        code TEXT PRIMARY KEY,
        created_by BIGINT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW(),
        expires_at TIMESTAMP NOT NULL,
        used_by BIGINT,
        used_at TIMESTAMP
    )
    """,
    # Runs its body only the first time, so existing users keep access and a
    # later empty table never re-admits anyone.
    """
    DO $$ BEGIN
        IF to_regclass('public.allowed_users') IS NULL THEN
            CREATE TABLE allowed_users (
                chat_id BIGINT PRIMARY KEY,
                invited_by BIGINT,
                added_at TIMESTAMP DEFAULT NOW()
            );
            INSERT INTO allowed_users (chat_id)
                SELECT chat_id FROM user_settings UNION SELECT chat_id FROM alerts;
        END IF;
    END $$
    """,
)

INVITE_TTL_DAYS = 7
RELOAD_S = 300
_RETRY_S = 30
# Telegram deep-link payloads are [A-Za-z0-9_-], at most 64 characters.
_INVITE_RE = re.compile(r"^/start(?:@\w+)?\s+inv_([A-Za-z0-9_-]{8,60})\s*$")

# {ask} becomes "Ask @admin1 or @admin2" (admins' usernames from user_settings).
LOCKED_TEXT = "🔒 JobQuest is invite-only. {ask} for an invite link."
BAD_INVITE_TEXT = "This invite link has expired or was already used. {ask} for a new one."
WELCOME_TEXT = "✅ You're in. Welcome to JobQuest!"

_gate = None


class _Access:
    def __init__(self, get_conn, return_conn, admin_ids):
        self.get_conn = get_conn
        self.return_conn = return_conn
        self.admin_ids = set(admin_ids)
        self._allowed = set()
        self._admin_usernames = []
        self._next_reload = 0.0
        self._lock = threading.Lock()

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

    def _reload(self, force=False):
        if not force and time.monotonic() < self._next_reload:
            return
        with self._lock:
            if not force and time.monotonic() < self._next_reload:
                return
            try:
                def work(cur):
                    cur.execute("SELECT chat_id FROM allowed_users")
                    return {row["chat_id"] for row in cur.fetchall()}
                self._allowed = self._db(work)
                self._next_reload = time.monotonic() + RELOAD_S
            except Exception as e:
                self._next_reload = time.monotonic() + _RETRY_S
                logger.warning(f"Could not reload allowed users, keeping {len(self._allowed)}: {e}")
            # Only for the invite-only text, so a failure here never blocks access.
            try:
                def work(cur):
                    cur.execute(
                        """SELECT username FROM user_settings
                           WHERE chat_id = ANY(%s) AND coalesce(username, '') <> ''
                           ORDER BY lower(username)""",
                        (list(self.admin_ids),),
                    )
                    return [row["username"] for row in cur.fetchall()]
                self._admin_usernames = self._db(work)
            except Exception as e:
                logger.warning(f"Could not load admin usernames: {e}")

    def is_allowed(self, user_id):
        if user_id in self.admin_ids:
            return True
        self._reload()
        return user_id in self._allowed

    def _text(self, template):
        names = " or ".join(f"@{u}" for u in self._admin_usernames)
        return template.format(ask=f"Ask {names}" if names else "Ask an admin")

    def forget(self, user_id):
        self._allowed.discard(user_id)

    def _redeem(self, code, user):
        """Use the invite for `user`. Returns the inviting admin's ID, or None."""
        def work(cur):
            cur.execute(
                """UPDATE invites SET used_by = %s, used_at = NOW()
                   WHERE code = %s AND used_by IS NULL AND expires_at > NOW()
                   RETURNING created_by""",
                (user.id, code),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """INSERT INTO allowed_users (chat_id, invited_by) VALUES (%s, %s)
                   ON CONFLICT (chat_id) DO NOTHING""",
                (user.id, row["created_by"]),
            )
            return row["created_by"]

        try:
            inviter = self._db(work)
        except Exception:
            logger.exception(f"Invite redeem failed for user {user.id}")
            return None
        if inviter is not None:
            self._allowed.add(user.id)
            logger.info(f"User {user.id} joined with an invite from {inviter}")
        return inviter

    def gate(self, update, context):
        user = update.effective_user
        if user is None or self.is_allowed(user.id):
            return
        message = update.message
        match = _INVITE_RE.match((message.text or "") if message else "")
        try:
            if match:
                inviter = self._redeem(match.group(1), user)
                if inviter is not None:
                    message.reply_text(WELCOME_TEXT)
                    name = user.first_name or "Someone"
                    if user.username:
                        name += f" (@{user.username})"
                    try:
                        context.bot.send_message(chat_id=inviter, text=f"✅ {name} joined with your invite.")
                    except telegram.error.TelegramError:
                        pass
                    return  # let /start show the main menu
                message.reply_text(self._text(BAD_INVITE_TEXT))
            elif update.callback_query:
                update.callback_query.answer(self._text(LOCKED_TEXT), show_alert=True)
            elif message and update.effective_chat and update.effective_chat.type == "private":
                message.reply_text(self._text(LOCKED_TEXT))
        except telegram.error.TelegramError as e:
            logger.warning(f"Invite-only reply to {user.id} failed: {e}")
        raise DispatcherHandlerStop

    def invite(self, update, context):
        user = update.effective_user
        if user.id not in self.admin_ids:
            return
        code = secrets.token_urlsafe(12)

        def work(cur):
            cur.execute(
                """INSERT INTO invites (code, created_by, expires_at)
                   VALUES (%s, %s, NOW() + make_interval(days => %s))""",
                (code, user.id, INVITE_TTL_DAYS),
            )
        self._db(work)
        update.message.reply_text(
            f"🔗 Invite link for one person, valid {INVITE_TTL_DAYS} days:\n"
            f"https://t.me/{context.bot.username}?start=inv_{code}\n\n"
            "Send it to the person you want to invite. You'll get a message when they join.",
            disable_web_page_preview=True,
        )


def revoke(cursor, chat_id):
    """Remove a user's access; call inside the delete-user transaction."""
    cursor.execute("DELETE FROM allowed_users WHERE chat_id = %s", (chat_id,))
    if _gate is not None:
        _gate.forget(chat_id)


def register(dispatcher, *, get_conn, return_conn, admin_ids, command_group):
    global _gate
    _gate = _Access(get_conn, return_conn, admin_ids)
    _gate._reload(force=True)
    dispatcher.add_handler(TypeHandler(Update, _gate.gate), group=-1)
    dispatcher.add_handler(CommandHandler("invite", _gate.invite), group=command_group)
    return _gate
