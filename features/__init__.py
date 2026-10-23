"""Feature modules that bot.py wires in. None of them import bot.py.

    companies           per-chat company blocklist (pure logic + SQL)
    jev                 TypeSafe's Jev model over HTTP (several questions per request)
    german_requirement  the Jev question: does the posting require German?
    job_fit             the Jev question: is the job about the alert's keywords?
    job_postings        LinkedIn guest posting fetch: open/closed status + description
    alert_settings      per-alert "I don't speak German" setting
    send_filters        the pre-send filter step used by the alert checker (full description, no fallback)
    saved_status        scheduled sweep that marks closed saved jobs
    keyboards           the inline keyboard on alert job messages
    handlers            Telegram callbacks for all of the above
    broadcast           update announcements, sent by scripts/announce.py (no chat command)
    access              invite-only gate and admin /invite links
    admins              master admin: add/remove admins, see and cancel invites
"""
