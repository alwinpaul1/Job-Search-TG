"""Does a job need German? Asked of TypeSafe's Jev model, one call per job.

Phrase matching can't answer this: "Verhandlungssicheres Deutsch. Ein Plus:
SAP-Erfahrung" has "ein Plus" right after "Deutsch" yet German is required.
On 106 hand-labelled real ads (LinkedIn + Arbeitnow, 78 German-written,
2026-10-22) Jev matched every label; ad-language detection got 93 right and
phrase rules 99.

Needs TYPESAFE_API_KEY in the environment (.env). Without it, or when the API
fails, classify() returns None and the caller falls back to ad language.
Cost is input tokens only: about 1,500 per job.
"""
import logging
import os
import threading
import time
from collections import OrderedDict

import requests

logger = logging.getLogger(__name__)

_URL = "https://api.typesafe.ai/v1/systemone"
# Pinned, not the jev-latest alias: an alias moves on release and can change answers.
MODEL = "jev-1.14.0"
_MAX_DESC_CHARS = 12000

REQUIRED = "required"
OPTIONAL = "optional"
NOT_STATED = "not_stated"

_QUESTION = {
    "type": "choice",
    "instructions": (
        "What does this job posting say about the German language skills the "
        "applicant needs? Judge only the applicant's own German skills. Ignore "
        "mentions of Germany, German customers, the German market, German "
        "offices, or language-course benefits."
    ),
    "criteria": {
        REQUIRED: {"what": "The applicant is expected to speak German, at any level, and it is not marked optional."},
        OPTIONAL: {"what": "German is explicitly optional: a plus, an advantage, nice to have, "
                           "helpful but not required, or the posting says German is not needed."},
        NOT_STATED: {"what": "The posting does not say anything about the applicant's German skills."},
    },
}

# Same job seen again (deferred, or in several alerts) isn't paid for twice.
_CACHE_MAX = 5000
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _key():
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def available():
    return bool(_key())


def classify(title, description, cache_key=None):
    """Return REQUIRED, OPTIONAL or NOT_STATED, or None if Jev can't be asked."""
    if cache_key:
        with _cache_lock:
            if cache_key in _cache:
                _cache.move_to_end(cache_key)
                return _cache[cache_key]
    key = _key()
    if not key:
        return None
    body = {
        "model": MODEL,
        "state": {"job_title": title or "", "job_description": (description or "")[:_MAX_DESC_CHARS]},
        "questions": {"german": _QUESTION},
    }
    for attempt in range(3):
        try:
            resp = requests.post(
                _URL, json=body, timeout=20,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            logger.warning(f"Jev request failed: {e}")
            return None
        if resp.status_code in (429, 529):
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            logger.warning(f"Jev answered {resp.status_code}: {resp.text[:200]}")
            return None
        verdict = resp.json()["answers"]["german"]["choice"]
        if cache_key:
            with _cache_lock:
                _cache[cache_key] = verdict
                while len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
        return verdict
    logger.warning("Jev rate-limited three times in a row; skipping the check")
    return None


def english_speaker_can_apply(verdict, ad_language):
    """The policy, kept in code. A posting silent about German is judged by the
    language it is written in: in Germany a German ad that says nothing about
    language expects German (Indeed Hiring Lab found 2.7% of ads say German
    isn't needed)."""
    if verdict == REQUIRED:
        return False
    if verdict == OPTIONAL:
        return True
    return ad_language != "de"
