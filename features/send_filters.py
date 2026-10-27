"""The filter step the alert checker runs on deduped jobs right before sending.

Order: company blocklist (no network), then Jev reads the FULL posting and
answers, in one request:
- for every alert: is the job about the alert's keywords? (job_fit)
- for "I don't speak German" alerts: does the posting require German?
  (german_requirement)

For alerts whose location is "Remote", the same request also asks where the
work is done (workplace): on-site jobs are dropped unless the alert allows them.

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

from features import clef, companies, german_requirement, jev, job_fit, job_postings, ops_alerts, workplace

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


def _read(reply, missing, model):
    """Stored values from a model's reply, or None if the reply isn't usable."""
    try:
        fresh = {}
        for i, key in enumerate(missing):
            answer = reply[f"q{i}"]
            if key == german_requirement.CHECK_KEY:
                verdict = german_requirement.verdict(answer)
                if verdict not in (german_requirement.REQUIRED, german_requirement.OPTIONAL,
                                   german_requirement.NOT_STATED):
                    raise ValueError(f"unknown German answer {verdict!r}")
                fresh[key] = verdict
            elif key == workplace.CHECK_KEY:
                fresh[key] = workplace.encode(answer)
            else:
                p = job_fit.unrelated_probability(answer)
                if not 0.0 <= p <= 1.0:
                    raise ValueError(f"probability {p}")
                fresh[key] = job_fit.encode(p, model)
        return fresh
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Unusable model answer: {e}")
        return None


def _judge(state, missing):
    """Ask Jev, else Clef. Returns (stored values, "jev" | "clef"), or (None, None)."""
    # Question names are only for code; the models never see them.
    ask = {f"q{i}": q for i, q in enumerate(missing.values())}
    for name, model in (("jev", jev), ("clef", clef)):
        reply = model.ask(state, ask)
        fresh = _read(reply, missing, name) if reply is not None else None
        if fresh is not None:
            return fresh, name
        if reply is not None:
            model.last_error = "unusable answer"
    return None, None


# Verdicts of Check.job(). Every one except SEND is also a key in apply()'s stats.
SEND = "send"
UNCHECKED = ("no_description", "models_down")   # couldn't be judged right now


class Check:
    """Judges jobs one at a time against one set of questions. Alert checks
    (apply) and live searches (features/search_pages.py) share it, so both
    read the full description, ask the same questions and reuse the same
    stored answers."""

    def __init__(self, keywords, *, no_german=False, workplaces=None, max_fetches=MAX_FETCHES):
        self.keywords = keywords
        self.fit_key = job_fit.check_key(keywords)
        self.questions = {self.fit_key: job_fit.QUESTION}
        if no_german:
            self.questions[german_requirement.CHECK_KEY] = german_requirement.QUESTION
        if workplaces is not None:
            self.questions[workplace.CHECK_KEY] = workplace.QUESTION
        self.no_german = no_german
        self.workplaces = workplaces
        self.budget = [max_fetches]
        self.models_down = False   # once Jev and Clef both failed, the rest of this run waits too
        self.unreadable = 0
        self.by_clef = 0

    def job(self, cursor, job):
        """SEND, or why not: off_topic, needs_german, wrong_place, closed,
        no_description or models_down (the last two: not judged yet)."""
        link = job["Link"]
        answers = _stored(cursor, link, [*self.questions, _CLOSED_KEY])
        if _CLOSED_KEY in answers:
            return "closed"
        missing = {k: q for k, q in self.questions.items() if k not in answers}

        if missing and self.models_down:
            return "models_down"
        if missing:
            description, closed, unreadable = _description(job, self.budget)
            if closed:
                _store(cursor, link, {_CLOSED_KEY: "1"})
                return "closed"
            if description is None:
                self.unreadable += unreadable
                return "no_description"
            state = {"search_keywords": self.keywords, "job_title": job.get("Title", ""),
                     "job_description": description}
            fresh, model = _judge(state, missing)
            if fresh is None:
                self.models_down = True
                ops_alerts.problem(("models", f"Jev and Clef are both down (Jev: {jev.last_error}, "
                                              f"Clef: {clef.last_error}): new jobs are on hold"))
                return "models_down"
            if model == "clef":
                self.by_clef += 1
                ops_alerts.problem(("jev", f"Jev is down ({jev.last_error}): Clef is judging jobs instead"))
                ops_alerts.resolved(_MODELS_BACK)
            else:
                ops_alerts.resolved(("jev", "Jev works again"), _MODELS_BACK)
            _store(cursor, link, fresh)
            answers.update(fresh)

        if job_fit.off_topic(answers[self.fit_key]):
            logger.info(f"Off-topic for '{self.keywords}' (P={answers[self.fit_key]}): {job.get('Title', '')[:80]}")
            return "off_topic"
        if self.no_german and german_requirement.needs_german(answers[german_requirement.CHECK_KEY]):
            return "needs_german"
        if self.workplaces is not None and workplace.wrong_place(answers[workplace.CHECK_KEY], self.workplaces):
            logger.info(f"Not remote for '{self.keywords}' ({answers[workplace.CHECK_KEY]}): {job.get('Title', '')[:80]}")
            return "wrong_place"
        return SEND


def apply(jobs, *, cursor, keywords, blocked, no_german, workplaces=None):
    """Return (to_send, stats). `blocked` is a set of company keys; `workplaces`
    the allowed workplaces for a remote alert (workplace.allowed_for), else None."""
    stats = {"blocked": 0, "off_topic": 0, "needs_german": 0, "wrong_place": 0, "closed": 0,
             "no_description": 0, "models_down": 0, "by_clef": 0}
    kept, stats["blocked"] = companies.drop_blocked(jobs, blocked)
    _prune(cursor)

    check = Check(keywords, no_german=no_german, workplaces=workplaces)
    to_send = []
    for job in kept:
        verdict = check.job(cursor, job)
        if verdict == SEND:
            to_send.append(job)
        else:
            stats[verdict] += 1
    stats["by_clef"] = check.by_clef
    ops_alerts.note_unreadable(check.unreadable)
    return to_send, stats
