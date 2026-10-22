"""Fetch one LinkedIn posting through the public guest endpoint.

    https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<numeric id>

One request answers two questions: is the job still open, and what does the
description say (used for language detection). Checked live 2026-10-22: a
closed posting returns 200 with a `closed-job` element reading "No longer
accepting applications"; an open one has no such element and carries the
description in `.show-more-less-html__markup`.

Only LinkedIn links are handled. Indeed sits behind Cloudflare and a plain
request is refused, so Indeed links come back UNKNOWN without a request.
"""
import logging
import re
import threading
import time
from collections import OrderedDict

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"

_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/96.0.4664.93 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
# /jobs/view/<id> and /jobs/view/<slug>-<id>, on any subdomain (de., www.).
_ID_PATTERNS = (
    re.compile(r"linkedin\.com/(?:[^?#]*/)?jobs/view/(?:[^/?#]*-)?(\d{7,})"),
    re.compile(r"linkedin\.com/[^#]*[?&]currentjobid=(\d{7,})"),
)

# Spacing between requests, shared by every thread (alert checker + sweep).
_MIN_INTERVAL_S = 1.5
_rate_lock = threading.Lock()
_last_request = [0.0]

# Results are reused for a few hours so a job that keeps reappearing in an
# alert isn't fetched every 30-minute cycle. Failures are not cached.
_CACHE_TTL_S = 6 * 3600
_CACHE_MAX = 3000
_cache = OrderedDict()
_cache_lock = threading.Lock()


class Posting:
    __slots__ = ("status", "description")

    def __init__(self, status, description=""):
        self.status = status
        self.description = description


def linkedin_job_id(link):
    link = (link or "").lower()
    if "linkedin.com" not in link:
        return None
    for pattern in _ID_PATTERNS:
        m = pattern.search(link)
        if m:
            return m.group(1)
    return None


def _cached(job_id):
    with _cache_lock:
        hit = _cache.get(job_id)
        if hit and time.time() - hit[0] < _CACHE_TTL_S:
            _cache.move_to_end(job_id)
            return hit[1]
        return None


def _store(job_id, posting):
    with _cache_lock:
        _cache[job_id] = (time.time(), posting)
        _cache.move_to_end(job_id)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _wait_turn():
    with _rate_lock:
        delay = _last_request[0] + _MIN_INTERVAL_S - time.time()
        if delay > 0:
            time.sleep(delay)
        _last_request[0] = time.time()


def parse(html_text):
    """Classify a guest-endpoint page. Split out so it can be tested offline."""
    soup = BeautifulSoup(html_text, "lxml")
    closed = soup.select_one(".closed-job") is not None or (
        "no longer accepting applications" in html_text.lower()
    )
    desc_el = soup.select_one(".show-more-less-html__markup")
    description = desc_el.get_text(" ", strip=True) if desc_el else ""
    # A page with neither marker nor title is a block or error page, not a job.
    if not closed and not soup.select_one(".top-card-layout__title"):
        return Posting(UNKNOWN)
    return Posting(CLOSED if closed else OPEN, description)


def peek(link):
    """Cached Posting for a link, or None. Never makes a request."""
    job_id = linkedin_job_id(link)
    return _cached(job_id) if job_id else None


def fetch(link, use_cache=True):
    """Return a Posting for a job link. Never raises."""
    job_id = linkedin_job_id(link)
    if not job_id:
        return Posting(UNKNOWN)
    if use_cache:
        hit = _cached(job_id)
        if hit is not None:
            return hit

    _wait_turn()
    try:
        resp = requests.get(_URL.format(job_id), headers=_HEADERS, timeout=15)
    except requests.RequestException as e:
        logger.warning(f"Posting fetch failed for {job_id}: {e}")
        return Posting(UNKNOWN)

    if resp.status_code in (404, 410):
        posting = Posting(CLOSED)
    elif resp.status_code != 200:
        # 429, and LinkedIn's anti-bot 999: says nothing about the job.
        logger.info(f"Posting fetch for {job_id} answered {resp.status_code}")
        return Posting(UNKNOWN)
    else:
        posting = parse(resp.text)
        if posting.status == UNKNOWN:
            return posting

    _store(job_id, posting)
    return posting
