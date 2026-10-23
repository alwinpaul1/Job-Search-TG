"""Telegram callbacks for the feature modules.

bot.py calls register() once. Every callback prefix here (bc_, ams_,
sj_rmclosed) is unique, so these handlers sit in their own dispatcher group
and never compete with the main ConversationHandler. They also keep working
after a restart wipes conversation state.

Callback data:
    bc_add_<alert>_<job>    block the company of an alert job message
    bc_undo_<alert>_<job>   undo that block from the same message
    bc_list                 blocked-companies screen (from Preferences)
    bc_rm_<row id>          unblock one company on that screen
    bc_back                 back to Preferences
    ams_<alert>             German-language screen for one alert
    ams_l_<alert>_<code>    set it (any / nogerman)
    sj_rmclosed             delete closed saved jobs, then redraw Saved Jobs
"""
import html
import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from features import alert_settings, companies, keyboards, saved_status

logger = logging.getLogger(__name__)

# Telegram allows 100 inline buttons; keep the list screen well under that.
_MAX_LISTED = 40


def _answer(query, text=None):
    try:
        query.answer(text)
    except (telegram.error.TimedOut, telegram.error.BadRequest):
        pass


def _edit(query, text, markup):
    try:
        query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def _short(name, limit=28):
    return name if len(name) <= limit else name[: limit - 1] + "…"


class _Handlers:
    def __init__(self, get_conn, return_conn, preferences_menu, saved_jobs_menu):
        self.get_conn = get_conn
        self.return_conn = return_conn
        self.preferences_menu = preferences_menu
        self.saved_jobs_menu = saved_jobs_menu

    def _db(self, fn):
        """Run fn(cursor) in one transaction and return its result."""
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

    # --- block / unblock from an alert job message -------------------------

    def _company_for(self, cursor, chat_id, alert_id, job_id, message):
        cursor.execute(
            """SELECT c.company FROM job_details_cache c
               JOIN alerts a ON a.id = c.alert_id
               WHERE c.alert_id = %s AND c.job_id = %s AND a.chat_id = %s""",
            (alert_id, job_id, chat_id),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """SELECT company FROM sent_jobs
                   WHERE alert_id = %s AND job_id = %s AND chat_id = %s""",
                (alert_id, job_id, chat_id),
            )
            row = cursor.fetchone()
        if row and row["company"] and row["company"] != "N/A":
            return row["company"]
        # Cache rows go when an alert is deleted. The message itself still
        # reads "title / company - location" on lines 2 and 3.
        lines = [l.strip() for l in (message.text or "").split("\n") if l.strip()]
        if len(lines) > 2 and not lines[2].startswith(("Posted:", "From your alert")):
            return lines[2].split(" - ")[0].strip()
        return ""

    def block_toggle(self, update, context):
        query = update.callback_query
        undo = query.data.startswith("bc_undo_")
        rest = query.data[len("bc_undo_" if undo else "bc_add_"):]
        alert_id, _, job_id = rest.partition("_")
        if not alert_id.isdigit() or not job_id:
            _answer(query, "❌ Unknown job")
            return
        chat_id = query.message.chat_id

        def work(cur):
            company = self._company_for(cur, chat_id, int(alert_id), job_id, query.message)
            if not company:
                return None
            if undo:
                companies.unblock_name(cur, chat_id, company)
            elif not companies.block(cur, chat_id, company):
                return None
            return company

        company = self._db(work)
        if not company:
            _answer(query, "❌ Couldn't tell which company this is")
            return

        if undo:
            _answer(query, f"↩️ {_short(company)} unblocked")
        else:
            _answer(query, f"🚫 {_short(company)} blocked. Undo it here or in Preferences.")
        markup = keyboards.swap_button(
            query.message.reply_markup, keyboards.BLOCK_PREFIXES,
            keyboards.block_button(rest, blocked=not undo),
        )
        if markup is not None:
            try:
                query.edit_message_reply_markup(reply_markup=markup)
            except telegram.error.BadRequest:
                pass

    # --- blocked companies screen (Preferences) ----------------------------

    def _render_blocked(self, query):
        chat_id = query.message.chat_id
        rows = self._db(lambda cur: companies.list_blocked(cur, chat_id))
        if not rows:
            text = (
                "🚫 <b>Blocked Companies</b>\n\n"
                "None yet. Tap <b>🚫 Block company</b> under any alert job "
                "and that company's jobs stop coming."
            )
        else:
            text = (
                f"🚫 <b>Blocked Companies</b> ({len(rows)})\n\n"
                "Jobs from these companies are never sent to you and are hidden "
                "from searches. Tap one to unblock it."
            )
            if len(rows) > _MAX_LISTED:
                text += f"\n\nShowing the first {_MAX_LISTED}."
        buttons = [
            [InlineKeyboardButton(f"✖️ {_short(r['company_name'], 40)}",
                                  callback_data=f"bc_rm_{r['id']}")]
            for r in rows[:_MAX_LISTED]
        ]
        buttons.append([InlineKeyboardButton("🔙 Back to Preferences", callback_data="bc_back")])
        _edit(query, text, InlineKeyboardMarkup(buttons))

    def blocked_list(self, update, context):
        query = update.callback_query
        _answer(query)
        self._render_blocked(query)

    def blocked_remove(self, update, context):
        query = update.callback_query
        row_id = query.data[len("bc_rm_"):]
        if not row_id.isdigit():
            _answer(query)
            return
        chat_id = query.message.chat_id
        name = self._db(lambda cur: companies.unblock_id(cur, chat_id, int(row_id)))
        _answer(query, f"↩️ {_short(name)} unblocked" if name else None)
        self._render_blocked(query)

    def blocked_back(self, update, context):
        # bot.py's preferences_menu answers the query and redraws the screen.
        # The conversation is still in its Preferences state, so its buttons work.
        self.preferences_menu(update, context)

    # --- per-alert job language ---------------------------------------------

    def _load_alert(self, alert_id, chat_id):
        def work(cur):
            cur.execute(
                "SELECT * FROM alerts WHERE id = %s AND chat_id = %s",
                (alert_id, chat_id),
            )
            return cur.fetchone()
        return self._db(work)

    def _render_settings(self, query, alert):
        lang = alert_settings.language_of(alert)
        aid = alert["id"]
        text = (
            "🌐 <b>German Language</b>\n\n"
            f"📝 <b>Keywords:</b> {html.escape(alert['keywords'])}\n"
            f"📍 <b>Location:</b> {html.escape(alert['location'])}\n\n"
            "With <b>I don't speak German</b>, each job description is read and "
            "only jobs that say German is required are skipped. Jobs where "
            "German is a plus, or that don't mention language, are still sent."
        )
        buttons = [
            [InlineKeyboardButton(f"✅ {label}" if code == lang else label,
                                  callback_data=f"ams_l_{aid}_{code}")
             for code, label in alert_settings.LANGUAGES.items()],
            [InlineKeyboardButton("⬅️ Back to Alert", callback_data=f"view_alert_{aid}")],
        ]
        _edit(query, text, InlineKeyboardMarkup(buttons))

    def settings_show(self, update, context):
        query = update.callback_query
        _answer(query)
        alert = self._load_alert(int(context.match.group(1)), query.message.chat_id)
        if not alert:
            _edit(query, "❌ Alert not found.", None)
            return
        self._render_settings(query, alert)

    def settings_set(self, update, context):
        query = update.callback_query
        alert_id, value = int(context.match.group(1)), context.match.group(2)
        chat_id = query.message.chat_id
        if not self._db(lambda cur: alert_settings.set_language(cur, alert_id, chat_id, value)):
            _answer(query, "❌ Alert not found")
            return
        _answer(query, "✅ Saved. Applies from the next check.")
        alert = self._load_alert(alert_id, chat_id)
        if alert:
            self._render_settings(query, alert)

    # --- saved jobs ---------------------------------------------------------

    def remove_closed_saved(self, update, context):
        query = update.callback_query
        chat_id = query.message.chat_id
        removed = self._db(lambda cur: saved_status.remove_closed(cur, chat_id))
        _answer(query, f"🧹 Removed {removed} closed job(s)")
        context.user_data["saved_jobs_page"] = 0
        self.saved_jobs_menu(update, context)


def register(dispatcher, *, get_conn, return_conn, preferences_menu, saved_jobs_menu, group):
    h = _Handlers(get_conn, return_conn, preferences_menu, saved_jobs_menu)
    for callback, pattern in (
        (h.block_toggle, r"^bc_(add|undo)_"),
        (h.blocked_list, r"^bc_list$"),
        (h.blocked_remove, r"^bc_rm_\d+$"),
        (h.blocked_back, r"^bc_back$"),
        (h.settings_show, r"^ams_(\d+)$"),
        (h.settings_set, r"^ams_l_(\d+)_([a-z]+)$"),
        (h.remove_closed_saved, r"^sj_rmclosed$"),
    ):
        dispatcher.add_handler(CallbackQueryHandler(callback, pattern=pattern), group=group)
