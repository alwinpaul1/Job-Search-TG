import json
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MARKETPLACE = "www.workwise.io"
SITEMAP_URL = "https://www.workwise.io/sitemaps/companyPagesSitemap.xml"
DISTRIBUTION_URL = "https://candidates.workwise.io/v2/distributions/byDomain/{}"
SEARCH_URL = "https://search.workwise.io/v2/searches"
ENQUIRY_URL = "https://candidates.workwise.io/v2/enquiries/{}"
STATE_FILE = Path(__file__).resolve().parent.parent / "workwise_sites.json"
REFRESH_S = 30 * 60
DISCOVER_EVERY_S = 7 * 24 * 3600
SITE_PAUSE_S = 1.0
DISCOVER_WORKERS = 2
SEARCH_SIZE = 50
TIMEOUT_S = 20
FULL_TIME_HOURS = 35
JOB_TYPES = {
    "fulltime": lambda e: e["type"]["shortName"] in ("employment", "traineeship")
    and (e.get("hoursEnd") or FULL_TIME_HOURS) >= FULL_TIME_HOURS,
    "parttime": lambda e: e["type"]["shortName"] in ("student", "temporary")
    or (e["type"]["shortName"] == "employment" and (e.get("hoursStart") or FULL_TIME_HOURS) < FULL_TIME_HOURS),
    "contract": lambda e: e["type"]["shortName"] == "temporary",
    "internship": lambda e: e["type"]["shortName"] in ("internship", "compulsory-internship", "thesis"),
}
CITY_ALIASES = {"munich": "münchen", "cologne": "köln", "nuremberg": "nürnberg"}
COUNTRY_NAMES = {"DE": {"germany", "deutschland"}}

_lock = threading.Lock()
_searches = {}
_details = {}
_site_enquiries = {}
_worker = {"thread": None}


def _fold(text):
    text = (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def matches_keyword(keyword, title):
    words = set(re.findall(r"[a-z0-9]+", _fold(title)))
    return any(
        phrase and all(word in words for word in phrase)
        for phrase in (re.findall(r"[a-z0-9]+", _fold(p)) for p in (keyword or "").split(","))
    )


def _search_body(description="", company_id=None):
    return {
        "description": description, "end": None, "hoursEnd": None, "hoursStart": None,
        "languageLevels": [], "leadershipExperience": "does_not_matter", "locationLevels": [],
        "mobileWork": None, "occupations": [], "ongoing": True, "remoteWork": "does_not_matter",
        "salary": None, "salaryFlexible": None, "salaryType": None,
        "searchCompanyLevels": [{"companyId": company_id, "wanted": True}] if company_id else [],
        "searchCompanyTagLevels": [], "searchesEnquiryTypes": [], "start": None,
        "workExperience": "does_not_matter", "worldwide": True,
    }


def _enquiries(domain, body, size):
    response = requests.post(
        SEARCH_URL,
        params={"size": size, "withMatchings": "true"},
        json=body,
        headers={"Accept": "application/json", "Origin": f"https://{domain}",
                 "Referer": f"https://{domain}/en/jobsuche"},
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()
    return [m["enquiries"] for m in response.json()["data"]["matchingResult"]["data"]["matchings"]]


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {"discovered_at": 0, "checked": {}}


def _save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def _site_of(company):
    company_id, slug = company
    domain = f"{slug}.workwise.io"
    response = requests.get(DISTRIBUTION_URL.format(domain),
                            headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
    if response.status_code == 404:
        return company_id, None
    response.raise_for_status()
    return company_id, domain if response.json()["data"].get("customerId") == company_id else None


def discover():
    state = _load_state()
    sitemap = requests.get(SITEMAP_URL, timeout=60)
    sitemap.raise_for_status()
    companies = [(int(i), s) for i, s in re.findall(r"/unternehmen/(\d+)-([a-z0-9-]+)</loc>", sitemap.text)]
    todo = [c for c in companies if str(c[0]) not in state["checked"]]
    with ThreadPoolExecutor(DISCOVER_WORKERS) as pool:
        for company_id, domain in pool.map(_safe_site_of, todo):
            if company_id is not None:
                state["checked"][str(company_id)] = domain
    state["discovered_at"] = time.time()
    _save_state(state)
    logger.info(f"🔍 [WORKWISE] {len(companies)} companies, {len(todo)} newly checked, {len(sites(state))} career sites")
    return state


def _safe_site_of(company):
    try:
        return _site_of(company)
    except Exception as e:
        logger.warning(f"Workwise site lookup {company[1]} failed: {e}")
        return None, None


def sites(state=None):
    state = state or _load_state()
    return {int(cid): domain for cid, domain in state["checked"].items() if domain}


def sweep(only=None):
    for company_id, domain in sites().items():
        if only and domain not in only:
            continue
        try:
            _site_enquiries[domain] = _enquiries(domain, _search_body(company_id=company_id), 100)
        except Exception as e:
            logger.warning(f"Workwise {domain} list failed: {e}")
        time.sleep(SITE_PAUSE_S)
    logger.info(f"🔍 [WORKWISE] swept career sites: {sum(map(len, _site_enquiries.values()))} jobs "
                f"on {sum(1 for v in _site_enquiries.values() if v)} sites")


def _run():
    while True:
        started = time.time()
        try:
            if time.time() - _load_state()["discovered_at"] > DISCOVER_EVERY_S:
                discover()
            sweep()
        except Exception as e:
            logger.warning(f"Workwise career-site round failed: {e}")
        time.sleep(max(60.0, REFRESH_S - (time.time() - started)))


def _ensure_worker():
    with _lock:
        if _worker["thread"] is None:
            _worker["thread"] = threading.Thread(target=_run, name="workwise-sites", daemon=True)
            _worker["thread"].start()


def _detail(enquiry_id):
    if enquiry_id not in _details:
        response = requests.get(ENQUIRY_URL.format(enquiry_id),
                                headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
        response.raise_for_status()
        _details[enquiry_id] = response.json()["data"]
    return _details[enquiry_id]


def _places(enquiry):
    places = set()
    for level in enquiry.get("locationLevels") or []:
        places |= {(level.get(k) or "").strip().lower() for k in ("city", "state")}
        places |= COUNTRY_NAMES.get(level.get("countryCode"), set())
    if "remote" in (enquiry.get("jobLocationTypes") or []):
        places |= {"remote"} | COUNTRY_NAMES["DE"]
    return places - {""}


def _to_job(enquiry):
    detail = _detail(enquiry["id"])
    published = detail.get("firstPublished")
    return {
        "Title": enquiry["name"],
        "Company": (enquiry.get("company") or {}).get("name") or "",
        "Location": ", ".join(l["city"] for l in enquiry.get("locationLevels") or [] if l.get("city")) or "Remote",
        "Date Posted": datetime.fromisoformat(published).astimezone().replace(tzinfo=None) if published else None,
        "Link": f"https://{MARKETPLACE}/en/job/{enquiry['id']}-{enquiry['slug']}",
        "Description": BeautifulSoup(detail.get("description") or "", "html.parser").get_text("\n").strip(),
    }


def _wanted(location):
    return {CITY_ALIASES.get(t, t) for t in (p.strip().lower() for p in (location or "").split(",")) if t}


def _search_enquiries(keyword):
    key = (keyword or "").strip().lower()
    with _lock:
        cached = _searches.get(key)
        if cached and time.time() - cached[0] <= REFRESH_S:
            return cached[1]
    found = _enquiries(MARKETPLACE, _search_body(description=keyword), SEARCH_SIZE)
    with _lock:
        _searches[key] = (time.time(), found)
    return found


def _type_ok(job_types, enquiry):
    return not job_types or any(JOB_TYPES[t](enquiry) for t in job_types if t in JOB_TYPES)


def _recent(hours_old, job):
    return not hours_old or (job["Date Posted"] is not None
                             and (datetime.now() - job["Date Posted"]).total_seconds() <= hours_old * 3600)


def literal_titles(keyword, titles):
    return {t for t in titles if matches_keyword(keyword, t)}


def jobs(keyword, location, hours_old=None, job_types=(), relevant=literal_titles):
    _ensure_worker()
    try:
        searched = _search_enquiries(keyword)
    except Exception as e:
        logger.warning(f"Workwise search '{keyword}' failed: {e}")
        searched = []
    wanted = _wanted(location)
    site = [e for v in list(_site_enquiries.values()) for e in v]
    keep = relevant(keyword, [e["name"] for e in site])
    on_sites = (e for e in site if e["name"] in keep)
    unique = {e["id"]: e for e in [*searched, *on_sites]}
    found = []
    for enquiry in unique.values():
        if (wanted and not wanted & _places(enquiry)) or not _type_ok(job_types, enquiry):
            continue
        try:
            job = _to_job(enquiry)
            if _recent(hours_old, job):
                found.append(job)
        except Exception as e:
            logger.warning(f"Workwise job {enquiry.get('id')} failed: {e}")
    logger.info(f"🔍 [WORKWISE] '{keyword}'@'{location}': {len(found)} jobs")
    return found


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _worker["thread"] = "self-check"
    assert _wanted("Berlin, Munich ,") == {"berlin", "münchen"}
    assert _places({"locationLevels": [{"city": "München", "state": "Bayern", "countryCode": "DE"}]}) == {"münchen", "bayern", "germany", "deutschland"}
    assert _places({"locationLevels": [], "jobLocationTypes": ["remote"]}) == {"remote", "germany", "deutschland"}
    student = {"type": {"shortName": "student"}, "hoursStart": 20, "hoursEnd": 20}
    full = {"type": {"shortName": "employment"}, "hoursStart": 40, "hoursEnd": 40}
    assert _type_ok(["parttime"], student) and not _type_ok(["fulltime"], student)
    assert _type_ok(["fulltime"], full) and not _type_ok(["internship"], full) and _type_ok([], full)
    assert _recent(24, {"Date Posted": datetime.now()}) and not _recent(24, {"Date Posted": datetime(2020, 1, 1)})
    assert not _recent(24, {"Date Posted": None}) and _recent(None, {"Date Posted": None})
    state = discover()
    found_sites = sites(state)
    for domain in ("clypp.workwise.io", "lunch-more.workwise.io", "inkuplay-ug-haftungsbeschraenkt.workwise.io"):
        assert domain in found_sites.values(), domain
    sweep(only={"clypp.workwise.io", "lunch-more.workwise.io", "inkuplay-ug-haftungsbeschraenkt.workwise.io"})
    shared = {e["id"] for e in _search_enquiries("AI Engineer")}
    clypp = [j for j in jobs("AI Engineer", "Munich") if j["Company"] == "Clypp"]
    lunch = [j for j in jobs("Mensakraft", "Stuttgart") if "lunch" in j["Company"].lower()]
    inkuplay = [j for j in jobs("Chief of Staff", "Germany") if "inkuplay" in j["Company"].lower()]
    for job in clypp + lunch + inkuplay:
        print(job["Date Posted"], "|", job["Title"], "|", job["Company"], "|", job["Location"], "|", job["Link"], "|", len(job["Description"]), "chars")
    assert any("Computer Vision" in j["Title"] and j["Description"] for j in clypp)
    assert 129637 not in shared
    assert lunch and inkuplay
    assert matches_keyword("AI Engineer", "AI Engineer – Computer Vision & Backend (m/f/d)")
    assert not [j for j in jobs("Working Student Renewable", "Germany") if "Kaufmann" in j["Title"]]
    assert not [j for j in jobs("AI Engineer", "Hamburg") if j["Company"] == "Clypp"]
    assert not [j for j in jobs("AI Engineer", "Munich", hours_old=24) if "Computer Vision" in j["Title"]]
    assert not [j for j in jobs("AI Engineer", "Munich", job_types=["internship"]) if "Computer Vision" in j["Title"]]
    assert all(j["Location"] == "Remote" for j in inkuplay)
    print(f"workwise self-check OK: {len(found_sites)} career sites")
