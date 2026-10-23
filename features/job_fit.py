"""Is a job about what the alert's keywords ask for? One Jev question.

LinkedIn's search matches loosely: an alert for "Working Student Hydrogen
Systems" got 404 jobs in a week (2026-10-23), most of them Werkstudent jobs in
logistics, HR or auditing. Jev reads the keywords, the title and the full
description and says whether the job's own work is in that field.

The job is dropped when P(unrelated) >= DROP_AT. Tested on 339 real jobs from
every active alert, hand-checked against title and description: 0.7 dropped
40 of 75 off-topic jobs and 1 related one (a Siemens Energy gas-turbine job
for a hydrogen alert). Alerts with precise keywords ("AI Engineer",
"Marketing internship") lost nothing.
"""

DROP_AT = 0.7          # Jev (owner's choice, 2026-10-23)
# Clef's scores sit lower for the same jobs. On the same 339 labelled jobs:
# Clef >= 0.65 dropped 46 of 75 off-topic with 1 related job (Jev >= 0.7: 40
# and 1); at 0.7 Clef dropped only 36. So Clef, the fallback, gets its own cut-off.
DROP_AT_CLEF = 0.65

# Stored answers are keyed by this plus the keywords; bump it when the
# question's meaning changes so old answers are not reused.
CHECK_PREFIX = "fit1:"

QUESTION = {
    "type": "choice",
    "instructions": (
        "A job seeker set up a job alert with the search keywords in `search_keywords`. "
        "Is this job posting about what those keywords ask for? Judge the subject or field "
        "of the work the job does day to day, from `job_title` and `job_description`. "
        "Words like working student, intern, Werkstudent, Praktikum, thesis, junior or "
        "graduate only describe the type of position; matching only on those is not a fit. "
        "The employer's industry alone does not make the job's own work fit."
    ),
    "criteria": {
        "match": {"what": "The job's main work is in the field or subject the keywords name."},
        "related": {"what": "The job is in the same broad area and touches the keywords' subject, "
                            "but the subject is not its main focus."},
        "unrelated": {"what": "The job's work is in a different field. It shares at most the "
                              "position type (such as working student or intern) or the employer's industry."},
    },
}


def check_key(keywords):
    return CHECK_PREFIX + " ".join((keywords or "").lower().split())


def unrelated_probability(answer):
    return float(answer["probabilities"]["unrelated"])


def encode(p_unrelated, model):
    """Stored form: '0.7100' for Jev (also what older rows hold), '0.6800 clef' for Clef."""
    return f"{p_unrelated:.4f}" + (" clef" if model == "clef" else "")


def off_topic(stored):
    """Apply the cut-off of the model that gave the stored answer."""
    value, _, model = stored.partition(" ")
    return float(value) >= (DROP_AT_CLEF if model == "clef" else DROP_AT)
