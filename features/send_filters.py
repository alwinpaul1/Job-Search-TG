"""The filter step the alert checker runs on deduped jobs right before sending.

Order: company blocklist (no network), then Jev reads the FULL posting and
answers, in one request:
- for every alert: is the job about the alert's keywords? (job_fit)
- for "I don't speak German" alerts: does the posting require German?
  (german_requirement)

A job is judged only on its full description and sent only after a model
has judged it (owner's rule, 2026-10-23). Jev answers; when Jev can't be
reached the identical request goes to Cloudflare's Clef (features/clef.py).
When the description can't be read, or neither model answers, the job is
deferred: not sent and not recorded, so the next cycle sees it again. It is
never judged on its title alone and never sent unchecked.

Descriptions: Indeed rows carry the full text from the scrape (Indeed sits
behind Cloudflare and can't be fetched afterwards). LinkedIn search results
carry none, so the posting is fetched, which also reveals closed jobs.

Jev's answers are stored in `job_checks`, so a job that keeps showing up in
the scrape (dropped jobs are not recorded in sent_jobs) is fetched and asked
about once, across restarts too.
"""
import logging
import time

from features import clef, companies, german_requirement, jev, job_fit, job_postings, ops_alerts

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS job_checks (
        job_link TEXT NOT NULL,
        check_key TEXT NOT NULL,
        answer TEXT NOT NULL,
        checked_at TIMESTAMP DEFAULT NOW(),
        PRIMARY KEY (job_link, check_key)
    )
"""

# LinkedIn fetches per alert per cycle. At 1.5 s spacing that is about 40 s.
# Jobs past the cap are deferred and fetched in a later cycle.
MAX_FETCHES = 25
# Answers older than this are deleted; the checker stops seeing a job long before.
_KEEP_DAYS = 14
_PRUNE_EVERY_S = 6 * 3600
_MODELS_BACK = ("models", "Jobs are being judged again")
# Stored for a closed LinkedIn posting, so it isn't fetched again.
_CLOSED_KEY = "closed"
_last_prune = [0.0]


def _stored(cursor, link, keys):
    cursor.execute(
        "SELECT check_key, answer FROM job_checks WHERE job_link = %s AND check_key = ANY(%s)",
        (link, list(keys)),
    )
    return {row["check_key"]: row["answer"] for row in cursor.fetchall()}


def _store(cursor, link, answers):
    for key, value in answers.items():
        cursor.execute(
            """INSERT INTO job_checks (job_link, check_key, answer) VALUES (%s, %s, %s)
               ON CONFLICT (job_link, check_key) DO UPDATE
               SET answer = EXCLUDED.answer, checked_at = NOW()""",
            (link, key, value),
        )


def _prune(cursor):
    if time.monotonic() - _last_prune[0] < _PRUNE_EVERY_S:
        return
    _last_prune[0] = time.monotonic()
    cursor.execute(
        "DELETE FROM job_checks WHERE checked_at < NOW() - make_interval(days => %s)",
        (_KEEP_DAYS,),
    )


def _description(job, fetch_budget):
    """Return (full description or None if not readable now, closed, unreadable).

    unreadable: the page was asked for and gave no text (blocked, error page),
    as opposed to waiting for this cycle's fetch budget."""
    if not job_postings.linkedin_job_id(job["Link"]):
        # Indeed: the scrape's text, or nothing (it can't be fetched afterwards).
        text = job.get("Description") or None
        return text, False, text is None
    # LinkedIn: always the posting page, the full text plus open/closed, even
    # when the JobQuest fallback scrape brought some description along.
    posting = job_postings.peek(job["Link"])
    if posting is None:
        if fetch_budget[0] <= 0:
            return None, False, False
        fetch_budget[0] -= 1
        posting = job_postings.fetch(job["Link"])
    if posting.status == job_postings.CLOSED:
        return None, True, False
    if posting.status != job_postings.OPEN or not posting.description:
        return None, False, True   # blocked, rate-limited or empty page: try next cycle
    return posting.description, False, False


def apply(jobs, *, cursor, keywords, blocked, no_german):
    """Return (to_send, stats). `blocked` is a set of company keys."""
    stats = {"blocked": 0, "off_topic": 0, "needs_german": 0, "closed": 0,
             "no_description": 0, "models_down": 0, "by_clef": 0}
    kept, stats["blocked"] = companies.drop_blocked(jobs, blocked)
    _prune(cursor)

    fit_key = job_fit.check_key(keywords)
    unreadable_count = 0
    models_down = False   # once Jev and Clef both failed, the rest of this alert waits too
    to_send = []
    budget = [MAX_FETCHES]
    for job in kept:
        link = job["Link"]
        questions = {fit_key: job_fit.QUESTION}
        if no_german:
            questions[german_requirement.CHECK_KEY] = german_requirement.QUESTION
        answers = _stored(cursor, link, [*questions, _CLOSED_KEY])
        if _CLOSED_KEY in answers:
            stats["closed"] += 1
            continue
        missing = {k: q for k, q in questions.items() if k not in answers}

        if missing and models_down:
            stats["models_down"] += 1
            continue
        if missing:
            description, closed, unreadable = _description(job, budget)
            if closed:
                _store(cursor, link, {_CLOSED_KEY: "1"})
                stats["closed"] += 1
                continue
            if description is None:
                stats["no_description"] += 1
                unreadable_count += unreadable
                continue
            state = {"search_keywords": keywords, "job_title": job.get("Title", ""),
                     "job_description": description}
            # Question names are only for code; Jev never sees them.
            ask = {f"q{i}": q for i, q in enumerate(missing.values())}
            reply = jev.ask(state, ask)
            if reply is None:
                reply = clef.ask(state, ask)
                if reply is None:
                    stats["models_down"] += 1
                    models_down = True
                    ops_alerts.problem(("models", "Jev and Clef are both down: new jobs are on hold"))
                    continue
                stats["by_clef"] += 1
                ops_alerts.problem(("jev", "Jev is down: Clef is judging jobs instead"))
                ops_alerts.resolved(_MODELS_BACK)
            else:
                ops_alerts.resolved(("jev", "Jev works again"), _MODELS_BACK)
            fresh = {}
            for i, key in enumerate(missing):
                answer = reply[f"q{i}"]
                if key == german_requirement.CHECK_KEY:
                    fresh[key] = german_requirement.verdict(answer)
                else:
                    fresh[key] = f"{job_fit.unrelated_probability(answer):.4f}"
            _store(cursor, link, fresh)
            answers.update(fresh)

        if job_fit.off_topic(float(answers[fit_key])):
            stats["off_topic"] += 1
            logger.info(f"Off-topic for '{keywords}' (P={answers[fit_key]}): {job.get('Title', '')[:80]}")
            continue
        if no_german and german_requirement.needs_german(answers[german_requirement.CHECK_KEY]):
            stats["needs_german"] += 1
            continue
        to_send.append(job)
    ops_alerts.note_unreadable(unreadable_count)
    return to_send, stats
