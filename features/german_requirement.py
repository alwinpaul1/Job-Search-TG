"""Does a job need German? One Jev question, asked about the full posting.

Phrase matching can't answer this: "Verhandlungssicheres Deutsch. Ein Plus:
SAP-Erfahrung" has "ein Plus" right after "Deutsch" yet German is required.
On 106 hand-labelled real ads (LinkedIn + Arbeitnow, 78 German-written,
2026-10-22) Jev matched every label; ad-language detection got 93 right and
phrase rules 99.

A job counts as needing German only when the posting says so. An ad written
in German that never mentions language is NOT assumed to need it (owner's
call, 2026-10-23).

send_filters asks this together with the topic question (job_fit) in one Jev
request, and only once it has the full description.
"""

REQUIRED = "required"
OPTIONAL = "optional"
NOT_STATED = "not_stated"

# Stored answers are keyed by this; change it when the question's meaning changes.
CHECK_KEY = "german1"

QUESTION = {
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


def verdict(answer):
    """REQUIRED, OPTIONAL or NOT_STATED from Jev's answer to QUESTION."""
    return answer["choice"]


def needs_german(verdict):
    """Skip a job only when its posting states German is required. "German is a
    plus" and postings silent about language, in any language, are sent."""
    return verdict == REQUIRED
