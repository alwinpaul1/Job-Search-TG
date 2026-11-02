import json
import logging
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from features.workwise import COUNTRY_NAMES, _fold, _recent, _wanted, literal_titles, matches_keyword

logger = logging.getLogger(__name__)

FEED_URL = "https://{}.jobs.personio.de/xml"
JOB_URL = "https://{}.jobs.personio.de/job/{}"
STATE_FILE = Path(__file__).resolve().parent.parent / "personio_sites.json"
REFRESH_S = 30 * 60
DISCOVER_EVERY_S = 7 * 24 * 3600
FEED_PAUSE_S = 1.0
DISCOVER_WORKERS = 2
TIMEOUT_S = 20
MAX_LABEL = 63
LEGAL_FORMS = r"\b(gmbh|ag|se|kg|ug|mbh|co|ohg|gbr|e\.?v|inc|ltd|llc|group|gruppe|holding|deutschland|germany|haftungsbeschraenkt)\b"
JOB_TYPES = {
    "fulltime": lambda p: p["schedule"] in ("full-time", "full-or-part-time")
    and p["employmentType"] in ("permanent", "temporary", "trainee", ""),
    "parttime": lambda p: p["schedule"] in ("part-time", "full-or-part-time") or p["employmentType"] == "working_student",
    "contract": lambda p: p["employmentType"] in ("freelance", "temporary"),
    "internship": lambda p: p["employmentType"] in ("intern", "working_student"),
}

_lock = threading.Lock()
_feeds = {}
_worker = {"thread": None}


def slugs(company):
    full = _fold(company)
    core = re.sub(r"\s+", " ", re.sub(LEGAL_FORMS, " ", re.sub(r"[()\[\].,/+&]", " ", full))).strip()
    found = []
    for text in (core, full):
        for slug in (re.sub(r"[^a-z0-9]+", "-", text).strip("-"), re.sub(r"[^a-z0-9]+", "", text)):
            if slug and len(slug) <= MAX_LABEL and slug not in found:
                found.append(slug)
    return found[:3]


def _fetch(slug):
    response = requests.get(FEED_URL.format(slug), timeout=TIMEOUT_S, allow_redirects=False)
    if response.status_code != 200 or b"<workzag-jobs" not in response.content[:500]:
        return []
    return ET.fromstring(response.content).findall("position")


def _text(position, tag):
    return (position.findtext(tag) or "").strip()


def _parse(slug, position):
    created = _text(position, "createdAt")
    offices = [_text(position, "office")] + [(o.text or "").strip() for o in position.iter("additionalOffice")]
    description = "\n\n".join(
        f"{_text(d, 'name')}\n{BeautifulSoup(_text(d, 'value'), 'html.parser').get_text(chr(10)).strip()}"
        for d in position.iter("jobDescription")
    )
    return {
        "title": _text(position, "name"),
        "company": re.sub(r"\s*\([A-Z]{2}\)$", "", _text(position, "subcompany")) or slug,
        "offices": [o for o in offices if o],
        "created": datetime.fromisoformat(created).astimezone().replace(tzinfo=None) if created else None,
        "link": JOB_URL.format(slug, _text(position, "id")),
        "description": description.strip(),
        "schedule": _text(position, "schedule"),
        "employmentType": _text(position, "employmentType"),
    }


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {"discovered_at": 0, "checked": {}}


def _save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def _slug_of(company):
    for slug in slugs(company):
        try:
            if _fetch(slug):
                return company, slug
        except Exception as e:
            logger.warning(f"Personio lookup {slug} failed: {e}")
            return company, ""
    return company, None


def discover(companies):
    state = _load_state()
    todo = [c for c in dict.fromkeys(companies) if c and c not in state["checked"]]
    with ThreadPoolExecutor(DISCOVER_WORKERS) as pool:
        for done, (company, slug) in enumerate(pool.map(_slug_of, todo), 1):
            if slug != "":
                state["checked"][company] = slug
            if done % 500 == 0:
                _save_state(state)
    state["discovered_at"] = time.time()
    _save_state(state)
    logger.info(f"🔍 [PERSONIO] {len(todo)} companies newly checked, {len(sites(state))} feeds")
    return state


def sites(state=None):
    state = state or _load_state()
    return sorted({slug for slug in state["checked"].values() if slug})


def sweep(only=None):
    for slug in sites():
        if only and slug not in only:
            continue
        try:
            _feeds[slug] = [_parse(slug, p) for p in _fetch(slug)]
        except Exception as e:
            logger.warning(f"Personio {slug} feed failed: {e}")
        time.sleep(FEED_PAUSE_S)
    logger.info(f"🔍 [PERSONIO] swept feeds: {sum(map(len, _feeds.values()))} jobs on {len(_feeds)} feeds")


def _run(companies_source):
    while True:
        started = time.time()
        try:
            if time.time() - _load_state()["discovered_at"] > DISCOVER_EVERY_S:
                discover(companies_source())
            sweep()
        except Exception as e:
            logger.warning(f"Personio round failed: {e}")
        time.sleep(max(60.0, REFRESH_S - (time.time() - started)))


def start(companies_source):
    with _lock:
        if _worker["thread"] is None:
            _worker["thread"] = threading.Thread(target=_run, args=(companies_source,), name="personio-feeds", daemon=True)
            _worker["thread"].start()


def _places(position):
    places = {_fold(o) for o in position["offices"]}
    return places | {_fold(o) for o in COUNTRY_NAMES["DE"]} | ({"remote"} if any("remote" in p for p in places) else set())


def jobs(keyword, location, hours_old=None, job_types=(), relevant=literal_titles):
    wanted = {_fold(w) for w in _wanted(location)}
    found = []
    positions = [p for feed in list(_feeds.values()) for p in feed]
    keep = relevant(keyword, [p["title"] for p in positions])
    for position in positions:
        if position["title"] not in keep:
            continue
        if wanted and not wanted & _places(position):
            continue
        if job_types and not any(JOB_TYPES[t](position) for t in job_types if t in JOB_TYPES):
            continue
        job = {
            "Title": position["title"], "Company": position["company"],
            "Location": ", ".join(position["offices"]), "Date Posted": position["created"],
            "Link": position["link"], "Description": position["description"],
        }
        if _recent(hours_old, job):
            found.append(job)
    logger.info(f"🔍 [PERSONIO] '{keyword}'@'{location}': {len(found)} jobs")
    return found


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    assert slugs("statworx GmbH") == ["statworx", "statworx-gmbh", "statworxgmbh"]
    assert slugs("Arteus Energy")[:2] == ["arteus-energy", "arteusenergy"]
    assert matches_keyword("Machine Learning Engineer", "(Junior) Machine Learning Engineer (m/w/d)")
    assert matches_keyword("Marketing manager, ML Engineer", "Senior ML Engineer")
    assert not matches_keyword("AI Engineer", "Sales Manager")
    assert matches_keyword("Werkstudent Marketing", "Werkstudent (m/w/d) Marketing & Kommunikation")
    part = {"schedule": "part-time", "employmentType": "working_student"}
    assert JOB_TYPES["parttime"](part) and JOB_TYPES["internship"](part) and not JOB_TYPES["fulltime"](part)
    assert "muenchen" in _places({"offices": ["München"]}) and "germany" in _places({"offices": ["München"]})
    state = discover(["statworx GmbH", "Arteus Energy", "Snke", "zz no such company xyz"])
    assert {"statworx", "arteus-energy", "snke"} <= set(sites(state))
    assert state["checked"]["zz no such company xyz"] is None
    sweep(only={"statworx", "arteus-energy", "snke"})
    ml = jobs("Machine Learning Engineer", "Germany")
    for job in ml[:5]:
        print(job["Date Posted"], "|", job["Title"], "|", job["Company"], "|", job["Location"], "|", job["Link"], "|", len(job["Description"]), "chars")
    assert ml and all(j["Description"] for j in ml)
    assert any(j["Company"].startswith("statworx") for j in ml)
    assert not jobs("Machine Learning Engineer", "Germany", hours_old=1)
    print(f"personio self-check OK: {len(sites(state))} feeds known")
