"""Fetch one LinkedIn posting through the public guest endpoint.

    https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<numeric id>

One request answers two questions: is the job still open, and what does the
description say (used for language detection). Checked live 2026-10-22: a
closed posting returns 200 with a `closed-job` element reading "No longer
accepting applications"; an open one has no such element and carries the
description in `.show-more-less-html__markup`.

Only LinkedIn links are handled. Indeed sits behind Cloudflare and a plain
request is refused, so Indeed links come back UNKNOWN without a request.

Staying under LinkedIn's limits (2026-10-23; every new job of every alert is
now fetched, so Jev can read its full description):
- requests look like Chrome: curl_cffi with Chrome's TLS fingerprint, the same
  as JobQuest's LinkedIn scraper, in one session that keeps LinkedIn's cookies;
- at most one request every 1.5-3 s (random), shared by every thread;
- a refusal (429, LinkedIn's 999, or a block page) is usually a one-off:
  measured from the VPS, the next request a few seconds later succeeds. So the
  same page is retried after 5 s, then 10 s (or Retry-After);
- only when a page is refused three times in a row are job-page requests
  paused: 1 min, doubling up to 15 min, reset by the next success. Meanwhile
  fetch() returns UNKNOWN at once, so jobs wait a round instead of hammering
  LinkedIn into a longer block.
"""
import logging
import random
import re
import threading
import time
from collections import OrderedDict

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as _http
    _SESSION_KWARGS = {"impersonate": "chrome"}
except ImportError:  # local runs without curl_cffi
    import requests as _http
    _SESSION_KWARGS = {}

logger = logging.getLogger(__name__)

OPEN = "open"
CLOSED = "closed"
UNKNOWN = "unknown"

_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
# With curl_cffi the User-Agent comes from the impersonated Chrome; setting
# our own would contradict the TLS fingerprint.
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
if not _SESSION_KWARGS:
    _HEADERS["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
# /jobs/view/<id> and /jobs/view/<slug>-<id>, on any subdomain (de., www.).
_ID_PATTERNS = (
    re.compile(r"linkedin\.com/(?:[^?#]*/)?jobs/view/(?:[^/?#]*-)?(\d{7,})"),
    re.compile(r"linkedin\.com/[^#]*[?&]currentjobid=(\d{7,})"),
)

# Spacing between requests, shared by every thread (alert checker + sweep).
_MIN_INTERVAL_S = 1.5
_MAX_INTERVAL_S = 3.0
_request_lock = threading.Lock()   # also guards the session (not thread-safe)
_last_request = [0.0]
_session = [None]

# Retries of a refused page (seconds to wait before each), then a pause.
_RETRY_WAITS_S = (5, 10)
_MAX_RETRY_WAIT_S = 30      # a longer Retry-After becomes a pause, not a sleep in the checker
_PAUSE_START_S = 60
_PAUSE_MAX_S = 900
_pause = {"until": 0.0, "next": _PAUSE_START_S}

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


def paused():
    """True while job-page requests are paused after a refusal."""
    return time.time() < _pause["until"]


def _refused(reason, retry_after=None):
    wait = max(_pause["next"], retry_after or 0)
    _pause["until"] = time.time() + wait
    _pause["next"] = min(_pause["next"] * 2, _PAUSE_MAX_S)
    logger.warning(f"LinkedIn refused a job page ({reason}); pausing job-page requests for {wait:.0f} s")


def _accepted():
    _pause["next"] = _PAUSE_START_S


def _retry_after(resp):
    try:
        return min(float(resp.headers.get("Retry-After", "")), _PAUSE_MAX_S)
    except (TypeError, ValueError):
        return None


def _refusal(resp):
    """Why LinkedIn refused this response, or None if it didn't."""
    if resp.status_code in (429, 999):
        return f"HTTP {resp.status_code}"
    if resp.status_code == 200 and parse(resp.text).status == UNKNOWN:
        return "block page"
    return None


def _get(url):
    """One spaced request through the shared session. Holds _request_lock."""
    delay = _last_request[0] + random.uniform(_MIN_INTERVAL_S, _MAX_INTERVAL_S) - time.time()
    if delay > 0:
        time.sleep(delay)
    if _session[0] is None:
        _session[0] = _http.Session(**_SESSION_KWARGS)
    try:
        return _session[0].get(url, headers=_HEADERS, timeout=15)
    finally:
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

    with _request_lock:
        if paused():
            return Posting(UNKNOWN)
        for attempt in range(len(_RETRY_WAITS_S) + 1):
            try:
                resp = _get(_URL.format(job_id))
            except Exception as e:   # network errors (requests and curl_cffi raise different types)
                logger.warning(f"Posting fetch failed for {job_id}: {e}")
                return Posting(UNKNOWN)
            reason = _refusal(resp)
            if reason is None:
                break
            wait = _retry_after(resp) or (_RETRY_WAITS_S[attempt] if attempt < len(_RETRY_WAITS_S) else None)
            if wait is None or wait > _MAX_RETRY_WAIT_S:
                _refused(f"{reason}, {attempt + 1} times in a row", _retry_after(resp))
                return Posting(UNKNOWN)
            time.sleep(wait)

        if resp.status_code in (404, 410):
            posting = Posting(CLOSED)
        elif resp.status_code != 200:
            logger.info(f"Posting fetch for {job_id} answered {resp.status_code}")
            return Posting(UNKNOWN)
        else:
            posting = parse(resp.text)
        _accepted()

    _store(job_id, posting)
    return posting
