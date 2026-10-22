"""The filter step the alert checker runs on deduped jobs right before sending.

Order: company blocklist (no network), then, for alerts set to "I don't speak
German", the German check. It needs the description: Indeed rows carry it
from the scrape; LinkedIn rows don't, so their posting is fetched, which also
reveals jobs that already closed.

The German check asks Jev whether the job needs German (german_requirement).
If Jev can't be reached it falls back to the language the ad is written in,
which misses English ads that require German but keeps the alert working.
"""
from features import companies, german_requirement, job_postings, language

# LinkedIn fetches per alert per cycle. At 1.5 s spacing that is about 40 s.
# Jobs past the cap are deferred: not sent and not recorded, so the next
# cycle sees them again (and earlier fetches are served from the cache).
MAX_FETCHES = 25


def _description(job, fetch_budget):
    """Return (description, closed). fetch_budget is a one-item list."""
    description = job.get("Description") or ""
    if description:
        return description, False
    posting = job_postings.peek(job["Link"])
    if posting is None and job_postings.linkedin_job_id(job["Link"]):
        if fetch_budget[0] <= 0:
            return None, False
        fetch_budget[0] -= 1
        posting = job_postings.fetch(job["Link"])
    if posting is None:
        return "", False
    return posting.description, posting.status == job_postings.CLOSED


def apply(jobs, *, blocked, no_german):
    """Return (to_send, stats). `blocked` is a set of company keys."""
    stats = {"blocked": 0, "needs_german": 0, "closed": 0, "deferred": 0, "fallback": 0}
    kept, stats["blocked"] = companies.drop_blocked(jobs, blocked)
    if not no_german:
        return kept, stats

    to_send = []
    budget = [MAX_FETCHES]
    for job in kept:
        description, closed = _description(job, budget)
        if description is None:
            stats["deferred"] += 1
            continue
        if closed:
            stats["closed"] += 1
            continue
        ad_language = language.detect(description)
        verdict = german_requirement.classify(
            job.get("Title", ""), description, cache_key=job.get("Link")
        )
        if verdict is None:
            stats["fallback"] += 1
            can_apply = ad_language != "de"
        else:
            can_apply = german_requirement.english_speaker_can_apply(verdict, ad_language)
        if not can_apply:
            stats["needs_german"] += 1
            continue
        to_send.append(job)
    return to_send, stats
