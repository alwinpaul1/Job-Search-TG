"""Remote alerts: does the job's own posting say where the work is done?

An alert with location "Remote" (Harry's #52, #53) gets on-site jobs from
around the world: LinkedIn treats "Remote" as a loose place name and ignores
its own remote-only filter there (checked 2026-10-23: with and without
f_WT=2, the same internships in Nanterre, Calgary, Alabama...). Of 33 jobs
those two alerts received, none was remote: 21 postings named on-site or
hybrid work, 12 said nothing.

So for alerts whose location mentions "remote", Jev reads the full posting
and answers remote / hybrid / on-site / not stated. The job is dropped when
P(a workplace the alert doesn't allow) >= DROP_AT. Allowed: remote, plus
hybrid or on-site when the alert's workplace filter includes them. A posting
that says nothing is kept, like the German rule: only a stated fact drops a job.

Tested on those 33 jobs plus 3 fully remote ones: at 0.6 Jev dropped all 17
it called on-site and kept every "not stated" (max 0.47) and every remote
job (0.00); Clef dropped 14 of 15, kept all "not stated" (max 0.51) and the
remote ones (0.01).
"""
import json

DROP_AT = 0.6

# Stored per job (the answer doesn't depend on the alert); bump on any change of meaning.
CHECK_KEY = "workplace1"

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"
NOT_STATED = "not_stated"
_ALL = (REMOTE, HYBRID, ONSITE, NOT_STATED)

QUESTION = {
    "type": "choice",
    "instructions": ("Where does this job posting say the work is done? Judge only what the posting states "
                     "about the employee's place of work."),
    "criteria": {
        REMOTE: {"what": "Fully remote: the employee can work from home or anywhere, with no regular office or site attendance."},
        HYBRID: {"what": "Hybrid: part of the time remote and part at an office or site."},
        ONSITE: {"what": "On site: the work is done at a named office, plant, lab, site or city, with no remote option stated."},
        NOT_STATED: {"what": "The posting does not say where the work is done."},
    },
}

# The alert's workplace filter names (bot.py WORKPLACE_TYPES) -> answers they allow.
_FILTER_ALLOWS = {"Hybrid": HYBRID, "On-site": ONSITE}


def allowed_for(alert):
    """The workplaces a job may have for this alert, or None when not checked
    (the alert's location isn't remote)."""
    if "remote" not in (alert.get("location") or "").lower():
        return None
    allowed = {REMOTE, NOT_STATED}
    try:
        chosen = json.loads(alert.get("filters") or "{}").get("workplace") or {}
    except (ValueError, AttributeError):
        chosen = {}
    allowed.update(_FILTER_ALLOWS[name] for name in chosen if name in _FILTER_ALLOWS)
    return frozenset(allowed)


def encode(answer):
    """Stored form of Jev's/Clef's answer: 'remote=0.01,hybrid=0.02,onsite=0.95,not_stated=0.02'."""
    probs = answer["probabilities"]
    values = {k: float(probs[k]) for k in _ALL}
    if any(not 0.0 <= v <= 1.0 for v in values.values()):
        raise ValueError(f"probabilities {values}")
    return ",".join(f"{k}={v:.4f}" for k, v in values.items())


def wrong_place(stored, allowed):
    """True when the posting most likely puts the work somewhere the alert doesn't want."""
    probs = {k: float(v) for k, v in (part.split("=") for part in stored.split(","))}
    return sum(p for k, p in probs.items() if k not in allowed) >= DROP_AT
