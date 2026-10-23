"""Master-admin (owner) controls: the admin list and every invite link.

    /admins, or 👑 Admins in /admin    owner only: list admins, ➕ add one from
                                       the invited users, ✖️ remove one; each
                                       change has a confirm step
    📨 Invites in /admin               owner only: recent invite links and their
                                       status; ✖️ cancels an unused one, whoever made it
    📢 Send update in /admin           owner only: how to send /update and whether
                                       one is allowed now (features/broadcast.py)

Callback data:
    own_admins / own_addlist / own_add_<id> / own_addok_<id> / own_rm_<id> / own_rmok_<id>
    own_invites / own_ic_<code>

Admins live in the `admins` table. bot.py's ADMIN_USER_IDS is the live set
every `in ADMIN_USER_IDS` check reads; it is updated in place after each change
and at startup, and never emptied, not even briefly. The owner is always an
admin and can't be removed. The table is filled from bot.py's starting set the
first time, when it's empty; the owner's row keeps it from ever being empty again.
"""
import html
import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

from features import access

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS admins (
        chat_id BIGINT PRIMARY KEY,
        added_by BIGINT,
        added_at TIMESTAMP DEFAULT NOW()
    )
"""

_INVITES_SHOWN = 15
_owner = None


def panel_rows(user_id):
    """Extra button rows for the /admin user list."""
    rows = [[InlineKeyboardButton("🔗 Invite someone", callback_data="acc_invite")]]
    if user_id == _owner:
        rows.append([InlineKeyboardButton("👑 Admins", callback_data="own_admins"),
                     InlineKeyboardButton("📨 Invites", callback_data="own_invites")])
        rows.append([InlineKeyboardButton("📢 Send update", callback_data="bcast_help")])
    return rows


def _apply(live, ids):
    """Make the live set equal `ids` without ever emptying it."""
    live.update(ids)
    live.difference_update(live - ids)


def _answer(query, text=None):
    try:
        query.answer(text)
    except (telegram.error.TimedOut, telegram.error.BadRequest):
        pass


class _Owner:
    def __init__(self, get_conn, return_conn, live_admins, owner_id):
        self.get_conn = get_conn
        self.return_conn = return_conn
        self.live = live_admins
        self.owner_id = owner_id

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

    def load(self):
        def work(cur):
            cur.execute(
                """INSERT INTO admins (chat_id)
                   SELECT unnest(%s::bigint[])
                   WHERE NOT EXISTS (SELECT 1 FROM admins)
                   ON CONFLICT DO NOTHING""",
                (sorted(self.live),),
            )
            cur.execute("INSERT INTO admins (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (self.owner_id,))
            cur.execute("SELECT chat_id FROM admins")
            return {row["chat_id"] for row in cur.fetchall()}
        try:
            _apply(self.live, self._db(work) | {self.owner_id})
            logger.info(f"Admins: {sorted(self.live)}")
        except Exception as e:
            logger.error(f"Could not load admins, using the built-in list {sorted(self.live)}: {e}")

    def _is_owner(self, update):
        if update.effective_user.id == self.owner_id:
            return True
        if update.callback_query:
            _answer(update.callback_query, "Only the master admin can do that.")
        return False

    def _show(self, update, text, rows):
        markup = InlineKeyboardMarkup(rows)
        if update.callback_query:
            try:
                update.callback_query.edit_message_text(
                    text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
            except telegram.error.BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        else:
            update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")

    def _people(self, cur, ids):
        cur.execute("SELECT chat_id, first_name, username FROM user_settings WHERE chat_id = ANY(%s)",
                    (list(ids),))
        found = {r["chat_id"]: access.person(r["first_name"], r["username"], str(r["chat_id"]))
                 for r in cur.fetchall()}
        return {i: found.get(i, str(i)) for i in ids}

    # --- admins ---------------------------------------------------------------

    def admins_screen(self, update, context, answered=False):
        if not self._is_owner(update):
            return
        if update.callback_query and not answered:
            _answer(update.callback_query)

        def work(cur):
            cur.execute("SELECT chat_id FROM admins ORDER BY chat_id <> %s, added_at", (self.owner_id,))
            ids = [r["chat_id"] for r in cur.fetchall()]
            return ids, self._people(cur, ids)
        ids, names = self._db(work)
        lines = [f"• {html.escape(names[i])}" + (" (master)" if i == self.owner_id else "") for i in ids]
        text = ("👑 <b>Admins</b>\n\n" + "\n".join(lines) +
                "\n\nAdmins can open /admin, edit or delete any user, and make invite links. "
                "Only you can change who is an admin.")
        rows = [[InlineKeyboardButton(f"✖️ Remove {names[i][:30]}", callback_data=f"own_rm_{i}")]
                for i in ids if i != self.owner_id]
        rows.append([InlineKeyboardButton("➕ Add admin", callback_data="own_addlist")])
        rows.append([InlineKeyboardButton("📨 Invites", callback_data="own_invites"),
                     InlineKeyboardButton("🔙 Admin panel", callback_data="adm_users")])
        self._show(update, text, rows)

    def add_list(self, update, context):
        if not self._is_owner(update):
            return
        _answer(update.callback_query)

        def work(cur):
            cur.execute(
                """SELECT u.chat_id FROM allowed_users u
                   WHERE u.chat_id NOT IN (SELECT chat_id FROM admins)
                   ORDER BY u.added_at, u.chat_id LIMIT 40"""
            )
            ids = [r["chat_id"] for r in cur.fetchall()]
            return ids, self._people(cur, ids)
        ids, names = self._db(work)
        text = ("➕ <b>Add admin</b>\n\nPick one of the bot's users." if ids else
                "➕ <b>Add admin</b>\n\nEveryone who uses the bot is already an admin. "
                "Invite someone first, then come back here.")
        rows = [[InlineKeyboardButton(names[i][:40], callback_data=f"own_add_{i}")] for i in ids]
        rows.append([InlineKeyboardButton("🔙 Admins", callback_data="own_admins")])
        self._show(update, text, rows)

    def confirm(self, update, context):
        if not self._is_owner(update):
            return
        query = update.callback_query
        action, target = context.match.group(1), int(context.match.group(2))
        if target == self.owner_id:
            _answer(query, "You're the master admin; that can't change.")
            return
        _answer(query)
        name = self._db(lambda cur: self._people(cur, [target]))[target]
        if action == "add":
            text = (f"Make <b>{html.escape(name)}</b> an admin?\n\n"
                    "They'll be able to open /admin, edit or delete any user, and make invite links.")
            yes = InlineKeyboardButton("✅ Make admin", callback_data=f"own_addok_{target}")
        else:
            text = (f"Remove <b>{html.escape(name)}</b> as admin?\n\n"
                    "They keep using the bot as a normal user.")
            yes = InlineKeyboardButton("✖️ Remove admin", callback_data=f"own_rmok_{target}")
        self._show(update, text, [[yes, InlineKeyboardButton("🔙 Cancel", callback_data="own_admins")]])

    def change(self, update, context):
        if not self._is_owner(update):
            return
        query = update.callback_query
        action, target = context.match.group(1), int(context.match.group(2))
        if target == self.owner_id:
            _answer(query, "You're the master admin; that can't change.")
            return

        def work(cur):
            if action == "addok":
                cur.execute("INSERT INTO admins (chat_id, added_by) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (target, self.owner_id))
            else:
                cur.execute("DELETE FROM admins WHERE chat_id = %s", (target,))
                # Keep them as a normal user.
                cur.execute("INSERT INTO allowed_users (chat_id, invited_by) VALUES (%s, %s) "
                            "ON CONFLICT DO NOTHING", (target, self.owner_id))
            cur.execute("SELECT chat_id FROM admins")
            return {r["chat_id"] for r in cur.fetchall()}
        _apply(self.live, self._db(work) | {self.owner_id})
        access.refresh()
        logger.info(f"Owner {action} admin {target}; admins now {sorted(self.live)}")
        if action == "addok":
            _answer(query, "✅ Admin added")
            try:
                context.bot.send_message(
                    chat_id=target,
                    text="👑 You're now a JobQuest admin. Use /admin to manage users and /invite to invite people.")
            except telegram.error.TelegramError:
                pass
        else:
            _answer(query, "Admin removed")
        self.admins_screen(update, context, answered=True)

    # --- invites --------------------------------------------------------------

    def invites_screen(self, update, context, answered=False):
        if not self._is_owner(update):
            return
        if not answered:
            _answer(update.callback_query)

        def work(cur):
            cur.execute(
                """SELECT code, created_by, used_by, created_at, used_at, cancelled_at,
                          expires_at, expires_at > NOW() AS live
                   FROM invites ORDER BY created_at DESC LIMIT %s""",
                (_INVITES_SHOWN,),
            )
            rows = cur.fetchall()
            ids = {r["created_by"] for r in rows} | {r["used_by"] for r in rows if r["used_by"]}
            return rows, self._people(cur, ids)
        invites, names = self._db(work)
        lines, buttons = [], []
        for r in invites:
            by = html.escape(names[r["created_by"]])
            made = r["created_at"].strftime("%d %b")
            if r["used_by"]:
                lines.append(f"✅ used by {html.escape(names[r['used_by']])} · from {by} · {made}")
            elif r["cancelled_at"]:
                lines.append(f"✖️ cancelled · from {by} · {made}")
            elif not r["live"]:
                lines.append(f"⌛ expired · from {by} · {made}")
            else:
                lines.append(f"🟢 open until {r['expires_at']:%d %b %H:%M} · from {by} · {made}")
                buttons.append([InlineKeyboardButton(
                    f"✖️ Cancel {names[r['created_by']][:20]}'s {r['created_at']:%d %b %H:%M} link",
                    callback_data=f"own_ic_{r['code']}")])
        text = (f"📨 <b>Invites</b> (latest {_INVITES_SHOWN})\n\n" + "\n".join(lines)
                if lines else "📨 <b>Invites</b>\n\nNo invite links yet.")
        buttons.append([InlineKeyboardButton("🔗 New invite", callback_data="acc_invite")])
        buttons.append([InlineKeyboardButton("👑 Admins", callback_data="own_admins"),
                        InlineKeyboardButton("🔙 Admin panel", callback_data="adm_users")])
        self._show(update, text, buttons)

    def cancel_invite(self, update, context):
        if not self._is_owner(update):
            return
        code = context.match.group(1)

        def work(cur):
            cur.execute(
                """UPDATE invites SET cancelled_at = NOW()
                   WHERE code = %s AND used_by IS NULL AND cancelled_at IS NULL""",
                (code,),
            )
            return cur.rowcount
        cancelled = self._db(work)
        _answer(update.callback_query, "Invite cancelled" if cancelled else "Already used or cancelled")
        self.invites_screen(update, context, answered=True)


def register(dispatcher, *, get_conn, return_conn, live_admins, owner_id, group):
    global _owner
    _owner = owner_id
    h = _Owner(get_conn, return_conn, live_admins, owner_id)
    h.load()
    dispatcher.add_handler(CommandHandler("admins", h.admins_screen), group=group)
    for callback, pattern in (
        (h.admins_screen, r"^own_admins$"),
        (h.add_list, r"^own_addlist$"),
        (h.confirm, r"^own_(add|rm)_(\d+)$"),
        (h.change, r"^own_(addok|rmok)_(\d+)$"),
        (h.invites_screen, r"^own_invites$"),
        (h.cancel_invite, r"^own_ic_([A-Za-z0-9_-]+)$"),
    ):
        dispatcher.add_handler(CallbackQueryHandler(callback, pattern=pattern), group=group)
    return h
