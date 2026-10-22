"""English vs German detection for job descriptions.

Counts function words from each language. No model and no new dependency:
job ads are long enough that the/and/with vs der/und/mit separates them
reliably. Words that exist in both languages ("in", "die", "was", "also",
"will", "an", "so") are left out of both lists.
"""
import re

_EN = frozenset("""
the and of to with for you your our we are is be this that have has from on
or at by it its as who what which about their they us can all more would
should must been were not but if into such through other than any
""".split())

_DE = frozenset("""
der das und mit für fuer sie wir ist ein eine einen einem einer zu von den
dem des auf bei oder nicht sich ihre ihr ihren unser unsere unseren unserem
du dich dir dein deine uns werden sind wird als auch wie aus nach über ueber
durch sowie zur zum im vom bzw gerne deiner
""".split())

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Below this many function words the text is too short to call.
_MIN_HITS = 8
# The winner needs this many times the loser's count; bilingual ads return None.
_MARGIN = 2.0


def detect(text):
    """Return "en", "de", or None when the text is too short or mixed."""
    if not text:
        return None
    en = de = 0
    for word in _WORD.findall(text.lower()):
        if word in _EN:
            en += 1
        elif word in _DE:
            de += 1
    if en + de < _MIN_HITS:
        return None
    if en >= _MARGIN * de:
        return "en"
    if de >= _MARGIN * en:
        return "de"
    return None
