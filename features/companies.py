"""Per-chat company blocklist.

A blocked company is hidden from every alert in that chat and from live
search results. Matching uses company_key(), which drops accents, punctuation
and trailing legal-form words, so "Acme GmbH", "ACME" and "Acme, Inc." are
the same company. It does NOT do the first-word match the dedup uses: blocking
"Scalable Capital" must not hide "Scalable Press".
"""
import re
import unicodedata

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS blocked_companies (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        company_key TEXT NOT NULL,
        company_name TEXT NOT NULL,
        blocked_at TIMESTAMP DEFAULT NOW(),
        UNIQUE (chat_id, company_key)
    )
"""

# Trailing tokens only. "co" and "kg" cover "GmbH & Co. KG".
_LEGAL_SUFFIXES = {
    "gmbh", "mbh", "ag", "se", "kg", "co", "kgaa", "ug", "ev", "gbr", "ohg",
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "corp",
    "corporation", "company", "bv", "nv", "sa", "sas", "sarl", "srl", "spa",
    "ab", "as", "oy", "aps", "pty", "lp", "llp", "group", "holding",
}


def company_key(name):
    """Normalized company identity, or "" when nothing usable is left."""
    if not name:
        return ""
    txt = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    tokens = re.sub(r"[^a-z0-9]+", " ", txt.lower()).split()
    # Keep at least one token: a company literally named "Group" stays "group".
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def blocked_keys(cursor, chat_id):
    cursor.execute(
        "SELECT company_key FROM blocked_companies WHERE chat_id = %s",
        (chat_id,),
    )
    return {row["company_key"] for row in cursor.fetchall()}


def block(cursor, chat_id, company_name):
    """Block a company. Returns its key, or "" if the name has no usable key."""
    key = company_key(company_name)
    if not key:
        return ""
    cursor.execute(
        """INSERT INTO blocked_companies (chat_id, company_key, company_name)
           VALUES (%s, %s, %s)
           ON CONFLICT (chat_id, company_key) DO NOTHING""",
        (chat_id, key, company_name.strip()),
    )
    return key


def unblock_name(cursor, chat_id, company_name):
    cursor.execute(
        "DELETE FROM blocked_companies WHERE chat_id = %s AND company_key = %s",
        (chat_id, company_key(company_name)),
    )
    return cursor.rowcount


def unblock_id(cursor, chat_id, row_id):
    # chat_id in the WHERE: a forged callback can't unblock someone else's row.
    cursor.execute(
        "DELETE FROM blocked_companies WHERE chat_id = %s AND id = %s RETURNING company_name",
        (chat_id, row_id),
    )
    row = cursor.fetchone()
    return row["company_name"] if row else None


def list_blocked(cursor, chat_id):
    cursor.execute(
        """SELECT id, company_name FROM blocked_companies
           WHERE chat_id = %s ORDER BY lower(company_name)""",
        (chat_id,),
    )
    return cursor.fetchall()


def delete_chat(cursor, chat_id):
    cursor.execute("DELETE FROM blocked_companies WHERE chat_id = %s", (chat_id,))


def drop_blocked(jobs, keys):
    """Split jobs into (kept, dropped_count) against a set of company keys."""
    if not keys:
        return list(jobs), 0
    kept = [j for j in jobs if company_key(j.get("Company")) not in keys]
    return kept, len(jobs) - len(kept)
