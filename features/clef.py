"""Cloudflare's Clef decision model: the fallback when Jev can't answer.

Clef takes the same request as Jev (state + questions) and answers in the same
shape, so send_filters passes it the identical request. Run through Workers
AI's REST endpoint with a Cloudflare API token that has Workers AI access.

Checked 2026-10-23 on the same hand-labelled jobs as Jev:
- topic (job_fit), 339 jobs at P(unrelated) >= 0.7: dropped 37 of 75
  off-topic, no on-topic or related job (Jev: 40, one related);
- German (german_requirement), 106 ads: right send/skip on 105, never sent a
  German-required job (Jev: 106).
clef-flash did worse on both, so the full model is used.

Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment (.env).
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

MODEL = "clef"
_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/" + MODEL


def _config():
    return (os.environ.get("CLOUDFLARE_API_TOKEN", "").strip(),
            os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip())


def ask(state, questions):
    """Return {question name: answer}, or None if Clef can't be asked right now."""
    token, account = _config()
    if not token or not account:
        return None
    body = {"model": MODEL, "state": state, "questions": questions}
    for attempt in range(3):
        try:
            resp = requests.post(
                _URL.format(account=account), json=body, timeout=60,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            logger.warning(f"Clef request failed: {e}")
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt + 1)
            continue
        if resp.status_code != 200:
            logger.warning(f"Clef answered {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            return resp.json()["result"]["answers"]
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"Clef reply unreadable: {e}")
            return None
    logger.warning("Clef busy three times in a row")
    return None
