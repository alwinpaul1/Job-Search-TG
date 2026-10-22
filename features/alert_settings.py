"""Per-alert German setting: send every job, or only jobs an English speaker
can apply to (features/german_requirement.py decides).

Stored in its own `alerts` column, not in the `filters` JSON: the edit-alert
and admin filter flows rewrite `filters` wholesale and would wipe extra keys.
"""
import html

SCHEMA_SQL = (
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS match_language TEXT DEFAULT 'any'",
)

ANY = "any"
NO_GERMAN = "nogerman"
LANGUAGES = {
    ANY: "Any job",
    NO_GERMAN: "I don't speak German",
}


def language_of(alert):
    value = (alert.get("match_language") or ANY) if alert else ANY
    return value if value in LANGUAGES else ANY


def skips_german_jobs(alert):
    return language_of(alert) == NO_GERMAN


def summary_html(alert):
    """Line for the alert-details screen."""
    return f"∙ <b>German:</b> <code>{html.escape(LANGUAGES[language_of(alert)])}</code>\n"


def set_language(cursor, alert_id, chat_id, value):
    if value not in LANGUAGES:
        return False
    cursor.execute(
        "UPDATE alerts SET match_language = %s WHERE id = %s AND chat_id = %s",
        (value, alert_id, chat_id),
    )
    return cursor.rowcount == 1
