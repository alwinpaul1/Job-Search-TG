"""Seniority of career-site jobs: which LinkedIn experience level does the posting ask for?

LinkedIn jobs carry a level that the alert's experience filter (Internship,
Entry level, Associate, Mid-Senior level, Director, Executive) applies to.
Workwise and Personio postings have no such field, so the filter did nothing
for them: alert 28 "Junior AI Engineer" (Entry / Associate / Internship) got
a "Senior Data Scientist/ML Ops Engineer" from a Personio feed (2026-11-02).

For those jobs only, and only when the alert has an experience filter, Jev (or
Clef) reads the full posting and gives a probability for each LinkedIn level.
The job is dropped when the most likely level is not one the alert selected.
The answer is stored per job (it doesn't depend on the alert).
"""
import json

# Stored per job; bump on any change of meaning.
CHECK_KEY = "level1"

INTERNSHIP = "internship"
ENTRY = "entry"
ASSOCIATE = "associate"
MID_SENIOR = "mid_senior"
DIRECTOR = "director"
EXECUTIVE = "executive"
_ALL = (INTERNSHIP, ENTRY, ASSOCIATE, MID_SENIOR, DIRECTOR, EXECUTIVE)

# LinkedIn's f_E codes (bot.py EXPERIENCE_LEVELS), in the same order as _ALL.
_CODES = dict(zip("123456", _ALL))

QUESTION = {
    "type": "choice",
    "instructions": ("Which career level is this job posting written for? Judge the experience and "
                     "responsibility the posting asks of the applicant, from the whole text: required years "
                     "of experience, how independent the work is, whether the person leads people or owns "
                     "decisions. A word such as Junior, Senior, Lead or Head in the title is strong evidence, "
                     "but the stated requirements decide. Ignore the company's size and the pay."),
    "criteria": {
        INTERNSHIP: {"what": "For people still studying: internship, working student (Werkstudent), "
                             "thesis, practical semester. No professional experience is expected.",
                     "examples": ["Werkstudent (m/w/d) in the finance team, 15 hours a week, enrolled student"]},
        ENTRY: {"what": "A first job or junior role: a degree or up to about two years of experience, works "
                        "under guidance. Includes trainee and graduate programs.",
                "examples": ["Junior Accountant: completed studies, first professional experience welcome"]},
        ASSOCIATE: {"what": "Some experience, roughly two to four years: does defined work independently, "
                            "leads nobody, and the posting does not call for senior-level experience.",
                    "examples": ["Accountant with 3 years of experience in bookkeeping"]},
        MID_SENIOR: {"what": "An experienced professional: senior, lead, principal or expert, expected to "
                             "bring about five or more years of experience or to own projects, architecture "
                             "or a small team.",
                     "examples": ["Senior Accountant: 6+ years of experience, mentors the junior staff"]},
        DIRECTOR: {"what": "Runs a department or function and manages managers or leads: director, head of, "
                           "vice president.",
                   "examples": ["Head of Finance: leads the finance department and its team leads"]},
        EXECUTIVE: {"what": "Runs the company or a whole business unit: C-level, managing director "
                            "(Geschäftsführer), partner.",
                    "examples": ["Chief Financial Officer reporting to the board"]},
    },
}


def is_career_site(link):
    """Workwise and Personio postings: the sources that have no experience level."""
    return "workwise.io/" in (link or "") or ".jobs.personio.de/" in (link or "")


def allowed_for(alert):
    """The levels the alert's experience filter selects, or None when it has no
    experience filter (the check is not asked)."""
    try:
        chosen = json.loads(alert.get("filters") or "{}").get("experience") or {}
    except (ValueError, AttributeError):
        chosen = {}
    return frozenset(_CODES[code] for code in chosen.values() if code in _CODES) or None


def encode(answer):
    """Stored form of Jev's/Clef's answer: 'internship=0.01,entry=0.90,associate=0.05,...'."""
    probs = answer["probabilities"]
    values = {k: float(probs[k]) for k in _ALL}
    if any(not 0.0 <= v <= 1.0 for v in values.values()):
        raise ValueError(f"probabilities {values}")
    return ",".join(f"{k}={v:.4f}" for k, v in values.items())


def most_likely(stored):
    probs = {k: float(v) for k, v in (part.split("=") for part in stored.split(","))}
    return max(probs, key=probs.get)


def wrong_level(stored, allowed):
    """True when the posting most likely asks for a level the alert didn't select."""
    return most_likely(stored) not in allowed


if __name__ == "__main__":
    from dotenv import load_dotenv
    from features import clef, jev

    load_dotenv()
    senior = ("Senior Backend Engineer (m/w/d)\nDeine Aufgaben: Du verantwortest die Architektur unserer Plattform und "
              "führst ein Team von vier Entwicklern.\nDein Profil: Mindestens 7 Jahre Berufserfahrung in der "
              "Softwareentwicklung, Erfahrung als Tech Lead.")
    student = ("Werkstudent Backend (m/w/d)\nDeine Aufgaben: Du unterstützt unser Team bei kleinen Aufgaben.\n"
               "Dein Profil: Du bist eingeschrieben in Informatik, 15 Stunden pro Woche, keine Erfahrung nötig.")
    assert allowed_for({"filters": '{"experience": {"Entry level": "2", "Associate": "3", "Internship": "1"}}'}) \
        == {ENTRY, ASSOCIATE, INTERNSHIP}
    assert allowed_for({"filters": '{"experience": {}}'}) is None and allowed_for({"filters": "{}"}) is None
    assert allowed_for({"filters": None}) is None and allowed_for({"filters": "not json"}) is None
    assert is_career_site("https://x.jobs.personio.de/job/1") and is_career_site("https://www.workwise.io/en/job/1-a")
    assert not is_career_site("https://www.linkedin.com/jobs/view/123") and not is_career_site(None)
    stored = encode({"probabilities": {"internship": 0, "entry": 0.1, "associate": 0.2, "mid_senior": 0.7,
                                       "director": 0, "executive": 0}})
    assert stored.startswith("internship=0.0000,entry=0.1000") and most_likely(stored) == MID_SENIOR
    assert wrong_level(stored, {ENTRY, ASSOCIATE}) and not wrong_level(stored, {ASSOCIATE, MID_SENIOR})
    for model in (jev, clef):
        reply = model.ask({"search_keywords": "Backend Engineer", "job_title": "x", "job_description": senior},
                          {"q0": QUESTION})
        assert reply is not None, f"{model.__name__} unavailable: {model.last_error}"
        assert most_likely(encode(reply["q0"])) == MID_SENIOR, (model.__name__, reply["q0"])
        reply = model.ask({"search_keywords": "Backend Engineer", "job_title": "x", "job_description": student},
                          {"q0": QUESTION})
        assert most_likely(encode(reply["q0"])) == INTERNSHIP, (model.__name__, reply["q0"])
    print("job_level self-check OK")
