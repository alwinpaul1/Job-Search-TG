"""Feature modules that bot.py wires in. None of them import bot.py.

    companies           per-chat company blocklist (pure logic + SQL)
    german_requirement  asks Jev (TypeSafe) whether a job needs German
    job_postings        LinkedIn guest posting fetch: open/closed status + description
    alert_settings      per-alert "I don't speak German" setting
    send_filters        the pre-send filter step used by the alert checker
    saved_status        scheduled sweep that marks closed saved jobs
    keyboards           the inline keyboard on alert job messages
    handlers            Telegram callbacks for all of the above
    broadcast           owner /update: announce a bot update to every user
    access              invite-only gate and admin /invite links
    admins              master admin: add/remove admins, see and cancel invites
"""
