"""Inline keyboard for alert job messages.

The checker builds it when sending; Save/Unsave and Block/Unblock then swap a
single button in the message's existing keyboard, so one action never wipes
the state another one set.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

SAVE_PREFIXES = ("save_job_", "unsave_from_alert_")
BLOCK_PREFIXES = ("bc_add_", "bc_undo_")


def save_button(job_unique_id, saved):
    if saved:
        return InlineKeyboardButton("✅ Saved", callback_data=f"unsave_from_alert_{job_unique_id}")
    return InlineKeyboardButton("💾 Save", callback_data=f"save_job_{job_unique_id}")


def block_button(job_unique_id, blocked):
    if blocked:
        return InlineKeyboardButton("↩️ Unblock company", callback_data=f"bc_undo_{job_unique_id}")
    return InlineKeyboardButton("🚫 Block company", callback_data=f"bc_add_{job_unique_id}")


def alert_job_keyboard(job_link, job_unique_id, *, saved, has_company):
    rows = [
        [InlineKeyboardButton("View Job", url=job_link), save_button(job_unique_id, saved)],
        [
            InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts"),
            InlineKeyboardButton("🏠 Start", callback_data="start_command"),
        ],
    ]
    if has_company:
        rows.insert(1, [block_button(job_unique_id, False)])
    return InlineKeyboardMarkup(rows)


def swap_button(markup, prefixes, new_button):
    """Copy of `markup` with every button whose callback starts with one of
    `prefixes` replaced by `new_button`. None if no such button exists."""
    if markup is None:
        return None
    swapped = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            data = getattr(btn, "callback_data", None) or ""
            if data.startswith(prefixes):
                new_row.append(new_button)
                swapped = True
            else:
                new_row.append(btn)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if swapped else None
