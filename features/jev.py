"""TypeSafe's Jev System One model over HTTP: one request, any number of questions.

Needs TYPESAFE_API_KEY in the environment (.env). Billing is input tokens
only, so asking the German and the topic question in one request about the
same posting costs about the same as asking one.

Measured 2026-10-23: a 60,000-character description goes through intact
(about 12,000 input tokens). Real postings are far shorter (largest of 233
recent ones: 10,134 characters), so descriptions are sent whole, never cut.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# Why the last ask() returned None, in a few words, for the admin report.
last_error = ""

_URL = "https://api.typesafe.ai/v1/systemone"
# Pinned, not the jev-latest alias: an alias moves on release and can change answers.
MODEL = "jev-1.14.0"


def _key():
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def _fail(reason):
    global last_error
    last_error = reason


def ask(state, questions):
    """Return {question name: answer}, or None if Jev can't be asked right now."""
    key = _key()
    if not key:
        _fail("no API key set")
        return None
    body = {"model": MODEL, "state": state, "questions": questions}
    for attempt in range(3):
        try:
            resp = requests.post(
                _URL, json=body, timeout=30,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            logger.warning(f"Jev request failed: {e}")
            _fail("not reachable")
            return None
        if resp.status_code in (429, 500, 502, 503, 529):
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            logger.warning(f"Jev answered {resp.status_code}: {resp.text[:200]}")
            _fail("API key rejected" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}")
            return None
        try:
            return resp.json()["answers"]
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"Jev reply unreadable: {e}")
            _fail("unreadable reply")
            return None
    logger.warning("Jev busy three times in a row")
    _fail("busy")
    return None
