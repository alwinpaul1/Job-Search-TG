"""Idempotent schema changes for the feature modules, run from init_db()."""
from features import access, admins, alert_settings, broadcast, companies, saved_status, send_filters

STATEMENTS = (
    companies.SCHEMA_SQL,
    broadcast.SCHEMA_SQL,
    *alert_settings.SCHEMA_SQL,
    *saved_status.SCHEMA_SQL,
    *access.SCHEMA_SQL,
    admins.SCHEMA_SQL,
    send_filters.SCHEMA_SQL,
)


def apply(cursor):
    for stmt in STATEMENTS:
        cursor.execute(stmt)
