"""Live search (🚀 Start Search): Jev reads each job's full posting, a page at a time.

The scrape returns up to a few hundred jobs, ranked by JobBERT on the title.
Judging them all before showing anything would take minutes (each LinkedIn
posting is fetched at one every 1.5-3 s), so jobs are judged as the user pages:
before a page is shown, enough jobs are judged to fill it, and the next page is
judged in the background while the user reads this one. A rejected job (not
about the search keywords, or on-site for a "remote" search) is removed from
the list, so pages fill with jobs that passed.

The check is the alerts' check (send_filters.Check): full description, Jev
then Clef, answers stored in job_checks and shared with alerts that use the
same keywords. One difference: a job that can't be judged right now (no
description, Jev and Clef both down) is shown anyway. The user is waiting and
there is no next round to retry it in.

Indeed descriptions can't be fetched again and are too big for user_data
(PicklePersistence writes it to disk), so they are kept here, in memory, per
user. After a restart a search has no session, and its remaining jobs are
shown unjudged.
"""
import logging
import threading

from features import job_postings, send_filters

logger = logging.getLogger(__name__)

# LinkedIn page fetches per check() call; the shared limiter in job_postings paces them.
MAX_FETCHES = 40

_sessions = {}   # user_id -> _Session, the latest search only
_sessions_lock = threading.Lock()


class _Session:
    def __init__(self, keywords, workplaces, descriptions):
        self.keywords = keywords
        self.workplaces = workplaces
        self.descriptions = descriptions   # Indeed link -> full description
        self.checked = 0                   # jobs[:checked] passed (or couldn't be judged)
        self.dropped = 0
        self.lock = threading.Lock()


def start(user_id, jobs, *, keywords, workplaces=None):
    """Register a new search. Call while the jobs still carry their descriptions."""
    descriptions = {j["Link"]: j["Description"] for j in jobs
                    if j.get("Description") and not job_postings.linkedin_job_id(j["Link"])}
    with _sessions_lock:
        _sessions[user_id] = _Session(keywords, workplaces, descriptions)


def ready(user_id, jobs, count):
    """True when the first `count` jobs (or all of them) need no more checking."""
    s = _sessions.get(user_id)
    return s is None or s.checked >= min(count, len(jobs))


def check(user_id, jobs, count, *, get_conn, return_conn):
    """Judge jobs in order, deleting rejected ones from `jobs` in place, until
    the first `count` remaining have been checked or the list runs out.
    Deletions happen only past the checked part, so a page already shown never
    changes under the user. Does nothing for a search without a session."""
    s = _sessions.get(user_id)
    if s is None:
        return
    with s.lock:
        if s.checked >= min(count, len(jobs)):
            return
        run = send_filters.Check(s.keywords, workplaces=s.workplaces, max_fetches=MAX_FETCHES)
        unchecked = 0
        conn = get_conn()
        try:
            cur = conn.cursor()
            while s.checked < min(count, len(jobs)):
                job = jobs[s.checked]
                description = s.descriptions.get(job["Link"])
                verdict = run.job(cur, dict(job, Description=description) if description else job)
                conn.commit()
                if verdict == send_filters.SEND or verdict in send_filters.UNCHECKED:
                    unchecked += verdict != send_filters.SEND
                    s.checked += 1
                else:
                    del jobs[s.checked]
                    s.dropped += 1
        except Exception:
            conn.rollback()
            raise
        finally:
            return_conn(conn)
        logger.info(f"Live search {user_id} '{s.keywords}': {s.checked} jobs checked, "
                    f"{s.dropped} rejected so far, {unchecked} shown unjudged this time, "
                    f"{run.by_clef} by Clef")
