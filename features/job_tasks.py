import re

MAX_LINES = 40
MIN_CHARS = 15
MAX_CHARS = 300
CHECK_PREFIX = "duty4:"
DUTY_AT = 0.5
SUBJECT_AT = 0.4
BULLETS = " \t•·-–—*▪●◦→✓✔►▶"
NONE = "none"
POSITION_WORDS = re.compile(
    r"(?i)\b(working[\s-]?student|werkstudent(?:in|:in|\*in)?|werkstudierende[rn]?|"
    r"studentische[rn]? (?:aushilfe|hilfskraft|mitarbeiter(?:in)?)|internship|intern|praktikum|"
    r"praktikant(?:in|:in|\*in)?|pflichtpraktikum|schnupperpraktikum|thesis|abschlussarbeit|bachelorarbeit|"
    r"masterarbeit|phd|doktorand(?:in)?|junior|senior|lead|head of|trainee|graduate|absolvent(?:in)?|"
    r"ausbildung(?:splatz)?(?: zum| zur)?|duales studium|dualer student|dual student|student|studierende)\b"
)
TITLE_NOISE = re.compile(r"\((?:[mwdfxa]\s*[/|,]?\s*){2,}\)|\(all genders?\)|all genders?|"
                         r"\bm/w/d\b|\bw/m/d\b|\bf/m/d\b|\bm/f/d\b|[()\[\]|]")


def check_key(keywords):
    return CHECK_PREFIX + " ".join((keywords or "").lower().split())


def subject_of(text):
    subject = TITLE_NOISE.sub(" ", POSITION_WORDS.sub(" ", text or ""))
    return re.sub(r"\s+", " ", subject).strip(" -–,:/") or (text or "")


def keyword_subject(keywords):
    return ", ".join(subject_of(p) for p in (keywords or "").split(",") if p.strip()) or (keywords or "")


def lines_of(description):
    found = []
    for raw in (description or "").splitlines():
        line = raw.strip(BULLETS)
        if len(line) < MIN_CHARS:
            continue
        parts = [line] if len(line) <= MAX_CHARS else re.split(r"(?<=[.!?;])\s+", line)
        for part in parts:
            part = part.strip(BULLETS)
            if MIN_CHARS <= len(part) and part not in found:
                found.append(part[:MAX_CHARS])
    return found[:MAX_LINES]


def questions(n):
    asked = {
        "employer": {
            "type": "noul",
            "instructions": "Is the main business of `company` itself `subject` (it builds, makes or researches "
                            "that subject), judging from `lines` and `job_title`?",
        },
    }
    for i in range(n):
        asked[f"d{i}"] = {
            "type": "noul",
            "instructions": f"Is `lines[{i}]` a duty the person hired into `job_title` will perform?",
            "criteria": {
                "true": "Something this person will do in the job.",
                "false": "A requirement or skill the applicant must bring, a benefit, an invitation or slogan, "
                         "or a fact about the company, team or product.",
            },
        }
        asked[f"s{i}"] = {
            "type": "noul",
            "instructions": f"Is the work described in `lines[{i}]` itself work on `subject`, "
                            "as opposed to work around it such as securing, selling, administering, "
                            "supporting or merely using it, or work in another part of the same broad field?",
        }
    return asked


def judge(ask, keywords, title, company, description):
    subject = keyword_subject(keywords)
    lines = lines_of(description)
    if not lines:
        return None
    reply = ask({"search_keywords": keywords, "subject": subject, "job_title": title, "company": company,
                 "lines": lines},
                questions(len(lines)))
    if not reply:
        return None
    pairs = [(float(reply[f"d{i}"]["noul"]), float(reply[f"s{i}"]["noul"])) for i in range(len(lines))]
    return lines, pairs, float(reply["employer"]["noul"])


def on_subject(pairs):
    return sum(1 for duty, on in pairs if duty >= DUTY_AT and on >= SUBJECT_AT)


def encode(pairs, employer, model):
    return f"{on_subject(pairs)}/{len(pairs)} e{employer:.2f}" + (" clef" if model == "clef" else "")


def mention_only(stored):
    if stored == NONE:
        return False
    count, employer = stored.split()[:2]
    return count.split("/")[0] == "0" and float(employer[1:]) < 0.5


if __name__ == "__main__":
    text = ("Über uns\nWir bauen KI-Plattformen für die Industrie.\nDeine Aufgaben\n• Entwicklung von KI-Modellen\n"
            "• Abstimmung mit dem Produktteam\nDein Profil\n• Studium der Informatik\nWir bieten\n• 30 Tage Urlaub")
    assert lines_of(text) == ["Wir bauen KI-Plattformen für die Industrie.", "Entwicklung von KI-Modellen",
                              "Abstimmung mit dem Produktteam", "Studium der Informatik"], lines_of(text)
    long_line = "A" * 20 + ". " + "B" * 400
    assert all(len(l) <= MAX_CHARS for l in lines_of(long_line))
    assert encode([(0.9, 0.8), (0.9, 0.1), (0.2, 0.9)], 0.1, "jev") == "1/3 e0.10"
    assert encode([(0.1, 0.1)], 0.7, "clef") == "0/1 e0.70 clef"
    assert mention_only("0/7 e0.10") and mention_only("0/3 e0.20 clef") and not mention_only("1/9 e0.10")
    assert not mention_only("0/9 e0.80") and not mention_only(NONE)
    assert subject_of("Werkstudent (m/w/d) KI-Engineering") == "KI-Engineering"
    assert keyword_subject("Working Student AI") == "AI" and keyword_subject("Marketing Internship") == "Marketing"
    assert keyword_subject("Marketing manager, Junior Campaign Manager") == "Marketing manager, Campaign Manager"
    print("job_tasks self-check OK")
