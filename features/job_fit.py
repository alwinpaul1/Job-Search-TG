"""Is a job about what the alert's keywords ask for? One Jev question.

LinkedIn's search matches loosely: an alert for "Working Student Hydrogen
Systems" got 404 jobs in a week (2026-10-23), most of them Werkstudent jobs in
logistics, HR or auditing. Jev reads the keywords, the title and the full
description and says whether the job's own work is in that field.

The job is dropped when Jev's (or Clef's) most likely answer is "unrelated";
"match" and "related" are sent (Alwin, 2026-11-01). The old rule, drop at
P(unrelated) >= 0.7, sent jobs the model itself rated more unrelated than
anything else (a Toyo Tires plant co-op for a hydrogen alert, 0.60).
"related" excludes jobs that only share a broad field (IT, software, engineering,
energy, marketing). On 100 real jobs this moved 13 from related to unrelated (an IT
System Engineer and a C# developer for ML / data alerts among them) and no match
to unrelated (2026-11-01).
"""

# Stored answers are keyed by this plus the keywords; bump it when the
# question's meaning changes so old answers are not reused.
CHECK_PREFIX = "fit3:"

QUESTION = {'type': 'choice',
 'instructions': 'A job seeker set up a job alert with the search keywords in `search_keywords`. Decide how '
                 'the work this job does day to day relates to the specific subject or role those keywords '
                 'name, judged from `job_title` and `job_description`. Words like working student, intern, '
                 'Werkstudent, Praktikum, thesis, junior, senior or graduate only describe the type of '
                 'position; matching only on those is not a fit. Sharing only a broad field with the '
                 'keywords, such as IT, software, engineering, energy, science, marketing or sales, is not a '
                 "fit, and neither is the employer's industry alone.",
 'criteria': {'match': {'what': "The job's main day-to-day work is the specific subject or role the keywords "
                                'name.',
                        'examples': ["keywords 'Frontend Developer': the job builds web user interfaces",
                                     "keywords 'Wind Energy Engineer': the job designs or commissions wind "
                                     'turbines']},
              'related': {'what': "The job's own daily work genuinely uses the keywords' specific subject as "
                                  'a real, named part of the role, though it is not the main focus.',
                          'examples': ["keywords 'Frontend Developer': a full-stack job where building the "
                                       'web UI is one of its stated tasks',
                                       "keywords 'Wind Energy Engineer': a grid-connection engineer whose "
                                       'projects are wind farms'],
                          'not_for': "Jobs in the same broad field that do not themselves do the keywords' "
                                     'subject: those are unrelated.'},
              'unrelated': {'what': "The job's own work does not involve the keywords' specific subject. It "
                                    'may share a broad field, the position type, a buzzword in passing, or '
                                    "the employer's industry.",
                            'examples': ["keywords 'Frontend Developer': an IT support or network "
                                         'administration job',
                                         "keywords 'Wind Energy Engineer': an electrician wiring office "
                                         'buildings at an energy company']}}}


def check_key(keywords):
    return CHECK_PREFIX + " ".join((keywords or "").lower().split())


def unrelated_probability(answer):
    return float(answer["probabilities"]["unrelated"])


def top_answer(answer):
    probabilities = {k: float(v) for k, v in answer["probabilities"].items()}
    return max(probabilities, key=probabilities.get)


def encode(answer, model):
    """Stored form: 'related 0.4300' for Jev, 'unrelated 0.6100 clef' for Clef."""
    return f"{top_answer(answer)} {unrelated_probability(answer):.4f}" + (" clef" if model == "clef" else "")


def off_topic(stored):
    return stored.split()[0] == "unrelated"
