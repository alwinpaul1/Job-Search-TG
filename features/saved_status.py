"""Mark saved LinkedIn jobs that stopped accepting applications.

A scheduled sweep re-checks each open saved job at most once a day and sets
`closed_at` when LinkedIn reports it closed. Saved Jobs then shows a closed
badge and offers to remove them. Rows are never deleted here; only the user's
"Remove closed" button deletes.
"""
import logging

from features import job_postings

logger = logging.getLogger(__name__)

SCHEMA_SQL = (
    "ALTER TABLE saved_jobs ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP",
    "ALTER TABLE saved_jobs ADD COLUMN IF NOT EXISTS status_checked_at TIMESTAMP",
)

# Consecutive UNKNOWN answers that mean we are being throttled, not that
# every job is odd. The sweep stops there and the rest wait for the next run.
_STOP_AFTER_UNKNOWN = 3


def sweep(get_conn, return_conn, batch=60):
    """Check up to `batch` saved jobs. Returns (checked, newly_closed)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, job_link FROM saved_jobs
               WHERE closed_at IS NULL
                 AND job_link ILIKE %s
                 AND (status_checked_at IS NULL
                      OR status_checked_at < NOW() - INTERVAL '1 day')
               ORDER BY status_checked_at NULLS FIRST, saved_at
               LIMIT %s""",
            ("%linkedin.com%", batch),
        )
        rows = cur.fetchall()
        conn.commit()
    finally:
        return_conn(conn)

    # No pool connection is held during the requests (up to ~90 s).
    results = []
    unknown_run = 0
    for row in rows:
        posting = job_postings.fetch(row["job_link"])
        results.append((row["id"], posting.status))
        unknown_run = unknown_run + 1 if posting.status == job_postings.UNKNOWN else 0
        if unknown_run >= _STOP_AFTER_UNKNOWN:
            logger.warning("Saved-job sweep stopped early: LinkedIn is not answering")
            # The throttled ones get retried next run, not pushed back a day.
            results = results[:-unknown_run]
            break

    if not results:
        return 0, 0

    closed = 0
    conn = get_conn()
    try:
        cur = conn.cursor()
        for saved_id, status in results:
            is_closed = status == job_postings.CLOSED
            closed += is_closed
            cur.execute(
                """UPDATE saved_jobs
                   SET status_checked_at = NOW(),
                       closed_at = CASE WHEN %s THEN NOW() ELSE closed_at END
                   WHERE id = %s""",
                (is_closed, saved_id),
            )
        conn.commit()
    finally:
        return_conn(conn)

    logger.info(f"Saved-job sweep: checked {len(results)}, newly closed {closed}")
    return len(results), closed


def remove_closed(cursor, chat_id):
    cursor.execute(
        "DELETE FROM saved_jobs WHERE chat_id = %s AND closed_at IS NOT NULL",
        (chat_id,),
    )
    return cursor.rowcount
