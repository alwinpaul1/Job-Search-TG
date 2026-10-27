# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

JobQuestTG — a Telegram bot for LinkedIn + Indeed job searching with AI-powered relevance matching and automated alerts. Mostly one file (`bot.py`, ~8,900 lines) plus the `features/` package for newer features. Runs on an Ubuntu VPS at 217.154.173.148. Sync python-telegram-bot v13 (no asyncio) — heavy work runs in a global 8-worker `ThreadPoolExecutor`.

Public bot: [@JobQuestTG_Bot](https://t.me/JobQuestTG_Bot)

## Running & Deploying

```bash
# Development
cd /root/Job-Search-TG && source .venv/bin/activate && python bot.py

# Production — managed by SYSTEMD (job-search-bot.service). Use ONLY systemctl:
systemctl restart job-search-bot     # restart (use after a deploy)
systemctl stop|start|status job-search-bot
journalctl -u job-search-bot -f      # live logs (also written to diagnostic.log)

# Deploy: on the VPS — git pull (or: git fetch origin && git reset --hard origin/main)
#         then  systemctl restart job-search-bot
```

**⚠️ NEVER use `run_bot.sh`.** The bot is managed by the systemd service
`job-search-bot.service` (`ExecStart=.venv/bin/python -u bot.py`, `Restart=on-failure`).
`run_bot.sh` is a legacy auto-restart wrapper — running it starts a SECOND instance
alongside the systemd one, and two instances fight over Telegram `getUpdates`
("Conflict: terminated by other getUpdates request") and double-scrape. All bots
on this VPS are systemd services. If you ever see duplicate instances, kill stray
`run_bot.sh`/`bot.py` processes and `systemctl restart job-search-bot` to get a
single clean instance.

## Architecture

New features go in `features/` as helper modules that bot.py imports and calls. They never import bot.py (it runs as `__main__`; importing it would re-run the whole module), so DB access and bot.py handlers are passed in as arguments.

| Module | What it does | Called from |
|--------|--------------|-------------|
| `companies` | Per-chat company blocklist, `company_key()` normalization (drops accents and legal suffixes, exact match only) | checker, live search, handlers, admin delete-user |
| `jev` | TypeSafe's Jev over HTTP (pinned `jev-1.14.0`), several questions per request; billed on input tokens only. Needs `TYPESAFE_API_KEY` in `.env` | `send_filters` |
| `clef` | Cloudflare's Clef (Workers AI REST, model `clef`), same request and answer shape as Jev: the fallback when Jev is down. Its topic scores run lower than Jev's, so it has its own cut-off, 0.65 (46/75 off-topic dropped, 1 related; at 0.7 only 36); German 105/106 right. Needs `CLOUDFLARE_API_TOKEN` (Workers AI permission) and `CLOUDFLARE_ACCOUNT_ID` in `.env`; without them the fallback is off and jobs wait for Jev | `send_filters` |
| `job_fit` | The Jev question "is this job about the alert's keywords?" (match / related / unrelated). Drop when P(unrelated) ≥ 0.7 for Jev, ≥ 0.65 for Clef (the stored answer carries ` clef` so the right cut-off is applied later). Tested 2026-10-23 on 339 real jobs from every alert: Jev dropped 40 of 75 off-topic, 1 related; precise-keyword alerts lost nothing | `send_filters` |
| `workplace` | Remote alerts only (location contains "remote"): asks Jev/Clef in the same request whether the posting is remote / hybrid / on-site / not stated, and drops the job when P(a workplace the alert doesn't allow) ≥ 0.6. Allowed: remote and not stated, plus hybrid / on-site when the alert's workplace filter includes them. Why: LinkedIn treats "Remote" as a place name and returns on-site jobs worldwide (f_WT=2 makes no difference, checked 2026-10-23); none of 33 jobs Harry's #52/#53 got was remote | `send_filters` |
| `german_requirement` | The Jev question "does the posting require German?": required / optional / not_stated. Skip ONLY when the posting states German is required; not_stated (any ad language) and optional are sent | `send_filters` |
| `job_postings` | LinkedIn guest `jobPosting/<id>` fetch → open/closed + full description; 6 h cache. Chrome-impersonating curl_cffi session (like JobQuest), one request every 1.5–3 s (random) shared by all threads. LinkedIn's refusals (429 / 999 / block page) are usually one-offs (measured 2026-10-23: the next request succeeds), so a refused page is retried after 5 s, then 10 s (or Retry-After ≤ 30 s); only 3 refusals in a row pause all job-page requests (1 min, doubling to 15 min, reset on success), during which `fetch()` returns UNKNOWN without a request | `send_filters`, `saved_status` |
| `alert_settings` | `alerts.match_language` column: `any` or `nogerman` ("I don't speak German") | checker, alert details |
| `send_filters` | Pre-send step in the checker: blocklist, then ONE Jev request per job on the FULL description: topic check for every alert, German check for `nogerman` alerts, workplace check for remote alerts. Judged only on the full description (Alwin, 2026-10-23): when Jev is unreachable the identical request goes to Cloudflare's Clef (`clef`); no description, or neither model answering → the job is deferred (not sent, not recorded, retried next cycle), never judged on its title and never sent unchecked. LinkedIn text always comes from the posting page (≤25 fetches per alert per cycle; closed jobs dropped); Indeed text is the scrape's full `Description` (never truncated: Indeed can't be fetched later). Answers and closed status are stored in `job_checks` (pruned after 14 days), so a job is fetched and judged once, across restarts. The per-job check is `send_filters.Check`, shared with live search (`search_pages`) | `check_single_alert` |
| `search_pages` | 🚀 Start Search, judged like alerts: the same `send_filters.Check` (full description, Jev then Clef, `job_checks` answers shared with alerts on the same keywords), but a page at a time, because hundreds of results can't be read up front (LinkedIn pages at 1.5–3 s each). Before a page is shown, enough jobs are judged to fill it (the user sees "Reading the job descriptions..."); the next page is judged in the background on the global `executor`. Rejected jobs are deleted from `user_data["jobs"]` past the checked part only, so a page already shown never changes. Unlike alerts, a job that can't be judged right now is shown (the user is waiting; no next round). Indeed descriptions live in an in-memory session per user (too big for the pickled `user_data`); after a restart the rest of a search is shown unjudged. A "remote" search gets the workplace check too | `run_scrape_threaded`, `page_navigation` |
| `saved_status` | 3-hourly sweep setting `saved_jobs.closed_at`; "Remove closed" button | scheduler, Saved Jobs |
| `keyboards` | Alert job message keyboard; Save and Block swap one button each so neither wipes the other | checker, save/unsave callbacks |
| `handlers` | Callbacks `bc_*`, `ams_*`, `aml_*`, `sj_rmclosed` in dispatcher group 3. `aml_*` is the admin copy of the German-language screen (🌐 German Language on `/admin`'s alert screen): it edits anyone's alert, checks `ADMIN_USER_IDS`, and its Back button returns to `adm_va_`. Not `adm_`-prefixed on purpose, so the stale admin fallback never catches it | `main()` |
| `broadcast` | Update announcements to every chat in `user_settings` ∪ `alerts` (~20 msg/s). No chat command: they are sent only from the server with `scripts/announce.py` (preview by default, `--send` to send). Spam guards in the `broadcasts` table: a send claims its row under a table lock; 24 h cooldown; identical text never sent twice. Imports no telegram code (talks to the Bot API over HTTPS) | `scripts/announce.py` |
| `access` | Invite-only gate. A `TypeHandler` in dispatcher group -1 runs before every handler and stops updates from anyone who is not an admin (`ADMIN_USER_IDS`) or in `allowed_users`, with an invite-only reply that names the admins' @usernames (read from `user_settings`, so a renamed admin shows up after their next /start). Admins send `/invite` for a one-time link (`t.me/<bot>?start=inv_<code>`, 24 hours, row in `invites`); opening it redeems the code atomically and /start then runs as normal, and the inviting admin gets a message. Allowed IDs are cached and reloaded every 5 min (last good list kept if the DB fails). `allowed_users` was filled once, when created, from the users already there | `main()`, admin delete-user (`revoke`) |
| `admins` | Master-admin (owner) controls. `/admins` or 👑 Admins in `/admin`: add an admin (picked from invited users) or remove one, each with a confirm step; the owner can't be removed. 📨 Invites in `/admin`: the latest 15 invite links with status, ✖️ cancels an unused one whoever made it (`invites.cancelled_at`). Every other admin gets 📨 My invites on the same callback (`own_invites`), scoped to the links they made, with who used each and when; they can cancel only their own (Aravind, 2026-10-27: his invite didn't show as his). Admins live in the `admins` table (seeded once from bot.py's `ADMIN_USER_IDS`); at startup and after each change the module rewrites the `ADMIN_USER_IDS` set in place, so every `in ADMIN_USER_IDS` check is live. Callbacks `own_*` (owner; `own_invites`/`own_ic_` also any admin, scoped) and `acc_invite` (🔗 Invite someone, any admin) | `main()`, `/admin` user list (`panel_rows`) |
| `ops_alerts` | Problem reports to the owner (master admin) in Telegram: one line per problem (Alwin: "simple, one line what's the issue"), under a `🚨 JobQuest · hh:mm` or `✅ JobQuest · hh:mm` header. Covers: alert checks failing (grouped by error), LinkedIn or Indeed failing for ≥ half the alerts in a round, ≥ 20 jobs waiting on unreadable LinkedIn pages, Jev down (Clef judging), Jev and Clef both down, the bot restarting itself, and a start after an unclean stop (crash / OOM kill; detected by the `.clean_exit` marker written on every planned exit). Each problem: first report at once, repeats at most every 6 h, one ✅ message when it clears. Single 429s and memory clean-ups are not reported (the bot handles them) | checker, `send_filters`, `main()`, `trigger_self_restart` |
| `watchdog/` (Cloudflare Worker, not Python) | Outside watchdog for what the bot can't report itself. The bot POSTs `/beat` every 5 min (`ops_alerts.beat`, which also checks the database) with the time its last round finished; a cron every 10 min messages Alwin in Telegram if no beat for 30 min (server, bot or network down) or no finished round for 2 h, and again when it recovers. Runs on Alwin's **personal** Cloudflare account (alwin.paulpv@gmail.com, `83f9c9…`, the same account as the Clef token) at `jobquest-watchdog.alwin-paulpv.workers.dev`, state in KV `jobquest-watchdog-state`. **Never the nexdash account** (Alwin, 2026-10-23: it was deleted there). wrangler's personal login is kept apart from the nexdash one: `XDG_CONFIG_HOME=~/.config/wrangler-personal wrangler deploy` (run in `watchdog/`). Without `WATCHDOG_URL` in the VPS `.env` the bot skips the ping (the database check still runs). Secrets (`wrangler secret put`): `WATCHDOG_SECRET` (same value as `WATCHDOG_SECRET` in the VPS `.env`) and `TELEGRAM_BOT_TOKEN`. The VPS `.env` also needs `WATCHDOG_URL` | VPS ↔ Cloudflare |
| `schema` | Idempotent DDL for the above, run from `init_db()` | `init_db()` |

The German setting lives in its own `alerts` column, NOT in the `filters` JSON: the edit-alert and admin filter flows rewrite `filters` wholesale.

Key sections of `bot.py` in order:

| Lines (approx) | Section |
|----------------|---------|
| 1–130 | Config, constants, env loading, global thread pool |
| 130–470 | Infrastructure classes: `DeadlockDetectableLock`, `CrashMonitor`, `CPUTracker` |
| 470–730 | `DatabasePool` — hand-rolled LIFO pool (adaptive sizing `10 + 0.7 × active_alerts`, clamp 10–50) |
| 885–950 | **Canonicalization functions** (`canonical_link`, `canonical_text`) — central to dedup. `ADMIN_USER_IDS` (a set: Alwin + Aravind) is hardcoded just above (~1004), NOT env-read |
| 950–1075 | Date parsing, `escape_markdown`, utilities |
| 1075–1350 | `init_db()` — schema creation, migrations, index creation (each block commits its own transaction) |
| 1674–1760 | User info tracking (`upsert_user_info`, `backfill_user_info`) |
| 1760–2190 | Telegram handlers (start, search flow, alert CRUD, preferences, saved jobs) |
| 2189–2330 | Admin stats — registered as **`/stats`** (not `/adminstats`) |
| 2337–3480 | **Admin panel** — user list, alert details, pause/resume/delete, delete user, edit keywords/location/filters, Pause/Resume-All |
| 3755–4490 | ML classifiers: `JobRelevanceEngine`†, `DynamicTermClassifier`, `TFIDFTermClassifier`, `CorpusOnlyClassifier`†, `PureMathematicalClassifier`, `UltraPureDynamicClassifier` († = dead code, never instantiated) |
| 4493–4690 | Model lifecycle (`get_jobbert_model`, unload/reload under memory pressure) |
| 4690–5400 | `AdaptiveJobBERTMatcher` — primary semantic matching (TechWolf/JobBERT-v3) |
| 5401–5990 | Scrapers: `scrape_linkedin` (guest API), `_jobquest_scrape_multi_board` (LinkedIn + Indeed concurrent), `scrape_linkedin_with_adaptive_jobbert` |
| 6339–6470 | New-alert baseline population (`setup_alert_threaded`) |
| 7201–7330 | Alert-update flow (edit alert → rescrape → dedup → baseline) |
| 7497–7940 | **Alert checker** (`check_all_alerts` / `check_single_alert`) — the 30-min loop that sends new jobs (send → insert+commit per job) |
| 7941–8797 | `main()`, handler registration, scheduler setup, stale callback fallbacks (my-alerts, saved-jobs, admin) |

## Database

PostgreSQL (`job_alerts` database, user `jobbot`). Config via `DATABASE_URL` env var or individual `POSTGRES_*` vars in `.env`.

**Tables:** `alerts`, `sent_jobs`, `user_settings`, `saved_jobs`, `job_details_cache`, `blocked_companies` (from `features/companies.py`), `broadcasts` (from `features/broadcast.py`), `allowed_users` + `invites` (from `features/access.py`), `admins` (from `features/admins.py`), `job_checks` (from `features/send_filters.py`)

**`user_settings`** also stores `first_name` and `username` (updated on every `/start`) for admin panel display. Existing users are backfilled from the Telegram API on startup.

**Schema migrations** are inline in `init_db()` (~line 1030) using `ADD COLUMN IF NOT EXISTS` pattern. No migration framework — columns are added idempotently on startup.

**Foreign key cascades:** `sent_jobs` and `job_details_cache` both have `FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE`. Deleting an alert automatically removes its sent jobs and cached details. `saved_jobs` has no cascade — it's keyed by `chat_id` and must be deleted separately.

## Dedup System (Critical Path)

The checker dedups per **chat** (not per alert) with four paths, in one SELECT (~line 7608):
1. same `job_id` (numeric LinkedIn ID from `canonical_link()`, most precise)
2. same `job_link` (the `sent_jobs` PK with alert_id — catches job_id-rule changes)
3. same `canonical_title` + exact `canonical_company`, **within 14 days**
4. same `canonical_title` + first word of `canonical_company` (≥4 chars), within 14 days

The 14-day window on paths 3–4 is the compromise between blocking cross-board duplicates and allowing legitimate reposts (a company closing and re-listing a role gets a fresh LinkedIn ID; after 14 days the fuzzy match no longer blocks it). A 48h recency buffer vs `last_checked` additionally filters old postings (intentionally wide — job dates are day-granular).

`sent_jobs` PK is `(alert_id, job_link)`; all inserts use `ON CONFLICT DO NOTHING`. Three code paths insert into `sent_jobs` (all must stay in sync, identical column lists):
- **Baseline population** (~line 6390): when a new alert is created
- **Alert-update** (~line 7290): when alert filters are edited (uses an in-memory `sent_job_ids` set, job_id-only membership; ON CONFLICT covers the rest)
- **Main alert loop** (~line 7790): per-job insert+commit immediately after each successful Telegram send (at-least-once delivery — a crash between send and commit re-sends at most one job)

## Admin Panel

Entry point: `/admin` (restricted to the `ADMIN_USER_IDS` set). The set in bot.py (~line 1004) is only the starting list: the `admins` table is the source of truth, managed by the owner through `/admins` (features/admins.py). To change admins, use that screen, not the constant. Uses a persistent `ConversationHandler` with six states:

| State | Purpose |
|-------|---------|
| `ADMIN_MENU` | Paginated user list: everyone in `alerts` ∪ `allowed_users` (shows names, alert counts; a new joiner shows "No alerts set"), built by `_admin_users_page` / `_admin_user_button` for both `/admin` and the page buttons |
| `ADMIN_USER_ALERTS` | Single user's alerts list + delete-user flow + Pause/Resume-All toggle |
| `ADMIN_ALERT_DETAILS` | Single alert detail view + pause/resume/delete + edit entry points |
| `ADMIN_EDIT_KEYWORDS` | Text input for new keywords |
| `ADMIN_EDIT_LOCATION` | Text input for new location |
| `ADMIN_EDIT_FILTERS` | Filter category/value picker (`adm_flt_*` callbacks) |

**Callback data prefixes:** `adm_user_`, `adm_va_`, `adm_pause_`, `adm_resume_`, `adm_pauseall_`, `adm_resumeall_`, `adm_editkw_`, `adm_editloc_`, `adm_editflt_`, `adm_flt_*`, `adm_delstart_`, `adm_delconf_`, `adm_deluserstart_`, `adm_deluserconf_`, `adm_users`, `adm_upage_`, `adm_back_user_`, `adm_cancel`.

**Pause/Resume-All** (`admin_paused` column): Pause-All sets `is_active=0, admin_paused=1` on the user's active alerts; Resume-All re-activates only `admin_paused=1` rows, never a user's own individually-paused alerts. All four `is_active` writers maintain this invariant.

**Delete user flow:** `admin_delete_user_start` → confirmation → `admin_delete_user_confirm` deletes `saved_jobs`, `alerts` (cascades to `sent_jobs`/`job_details_cache`), and `user_settings`, and revokes access (`allowed_users`), so the person needs a new invite to come back.

**Restarts:** the admin `ConversationHandler` is `persistent=True` (same `conversation_states.pickle` as the main conversation, since 2026-10-26), so a restart keeps the admin's place and any edit in progress. Restarts happen without a deploy too: Ubuntu's unattended-upgrades restarts services after a library update (24 Oct 06:24, zlib).

**Stale callback fallback** (~line 8700): A standalone `CallbackQueryHandler` (pattern `^adm_`) in group 2 catches admin button presses the conversation doesn't hold (for example a panel message from before the persistence change, or after `/cancel`) and re-routes them. Edit flows (`adm_editkw_/editloc_/editflt_/flt_*`) can't work without live conversation state, so the fallback answers "session expired, run /admin". Similar stale fallbacks exist in group 0 for My-Alerts buttons and saved-jobs buttons (🗑️/Prev/Next).

## Invite-only access

The bot is invite-only. To add someone, an admin sends `/invite` (or taps 🔗 Invite someone in `/admin`) and forwards the link; it works for one person and expires after 24 hours. When someone joins with another admin's invite, that admin and Alwin (master admin) both get a message; Alwin's own invites join silently (his call). Every join shows under 📨 Invites. Alwin alone can add or remove admins and see or cancel every invite (`/admins`, or 👑 Admins / 📨 Invites in `/admin`). To remove someone, use `/admin` → user → Delete user, which also revokes access. Deleting rows by hand in SQL leaves the `allowed_users` row behind: delete it too (`DELETE FROM allowed_users WHERE chat_id = …`), or the person can come back without an invite.

## Update announcements

There is no `/update` command. Announcements are sent only from the server, from a Claude Code session, with `scripts/announce.py`:

```bash
cd /root/Job-Search-TG
.venv/bin/python scripts/announce.py --file msg.txt          # preview: recipient count, exact text, whether a send is allowed now
.venv/bin/python scripts/announce.py --file msg.txt --test   # send one copy to Alwin only, to check the look (not recorded)
.venv/bin/python scripts/announce.py --file msg.txt --send   # send to every chat in user_settings ∪ alerts
```

The text is Telegram HTML and goes out exactly as written. At most one update per 24 hours, and the same text never twice, so bundle changes into one message.

How to write the text:
- Write it with the `unslop` skill. No hype, no filler.
- Format it (Alwin, 2026-10-23): a bold title, then one block per change with a bold heading and a short line under it, two empty lines between blocks (the script enforces this: any blank-line gap in the file becomes exactly two). Escape `&`, `<`, `>` as `&amp;` `&lt;` `&gt;`. Shape:

  ```
  📢 <b>JobQuest Update</b>

  🚫 <b>Block a company</b>
  Tap Block company under any job alert. Undo it in Preferences → Blocked Companies.

  🌐 <b>…next change…</b>
  …

  Tap /start to try it.
  ```
- Keep it short: about three changes, each readable in a few seconds on a phone.
- Send a `--test` copy to Alwin before `--send`, so he sees the formatting on his phone. One test copy per change, not one per tweak.
- `--send --force` skips the 24 h limit (never the same-text guard). Use it only when Alwin explicitly asks for that send.
- Say what changed and where to find it ("My Alerts → pick an alert → 🌐 German Language"), not how it was built.
- Only what users need (Alwin, 2026-10-23). Never mention admin features (invites, `/admin`, admin screens or rights): every user gets this message.
- At most one emoji per line, used as a marker.
- Show the draft to Alwin and get a yes before running with `--send`. Never send one on your own.

## Key Globals & Singletons

- `db_pool` — `DatabasePool` instance (connection pool)
- `_global_jobbert_model` — cached SentenceTransformer model (~500MB RAM)
- `_global_adaptive_matcher` — `AdaptiveJobBERTMatcher` instance
- `crash_monitor` — `CrashMonitor` instance
- Locks: `db_lock`, `model_lock`, `scheduler_lock`, `alert_ai_lock`, `memory_cleanup_lock`

## Background Scheduler (APScheduler)

| Job | Interval | Purpose |
|-----|----------|---------|
| `memory_aware_check_alerts` | 30 min | Scrape LinkedIn, send new jobs |
| `periodic_memory_cleanup` | 15 min | GC + model unload if needed |
| `cleanup_stuck_operations` | 5 min | Clear timed-out user operations |
| `heartbeat_check` | 2 min | Log health status |
| `scheduler_watchdog` | 10 min | Detect hung scheduler |
| `saved_job_status` | 3 h | `features/saved_status.sweep`: mark closed saved LinkedIn jobs |

## Dependency Security Setup (do not "simplify" away)

- `bot.py` starts with a **vendored-urllib3 shim** (before `import telegram`): it loads the repo's `ptb_vendor/urllib3` (1.26.20) under `telegram.vendor.ptb_urllib3.urllib3` in `sys.modules`. Reason: PTB v13's own bundled urllib3 (2016) crashes on Python 3.12 (`ssl.wrap_socket` removed → `NameError: PROTOCOL_SSLv23` on first connect), and PTB's fallback to system urllib3 requires urllib3\<2 — which had 4 high CVEs. With the shim, PTB uses the private py3.12-compatible v1 copy (api.telegram.org traffic only) while the **system urllib3 stays v2** for requests/scrapling/jobquest.
- Removing the shim or `ptb_vendor/` while system urllib3 is v2 crash-loops the bot at startup.
- `python-telegram-bot` must stay **13.13** (13.14/13.15 hard-pin `tornado==6.1`, which has high CVEs; 13.13 allows `tornado>=6.1`).
- `jobquest` was installed from a wheel at `/tmp/jobquest-0.1.12-py3-none-any.whl` that no longer exists. Its source is Alwin's repo github.com/alwinpaul1/JobQuest: commit `db4ff80` matches the installed 0.1.12 file for file (checked 2026-10-23), and `requirements.lock` pins it there. Don't change its version as a side effect of other work.
- `requirements.lock` is the exact installed set on the VPS (`uv pip freeze`); rebuild a venv from it, not from `requirements.txt`. Regenerate it after any package change.
- transformers 5.10.0, sentence-transformers 5.6.0, torch 2.13.0 (2026-10-23, to clear their CVEs). JobBERT-v3 embeddings were compared before and after: identical to 6e-8.
- setuptools must stay below 81: APScheduler 3.6.3 (hard-pinned by PTB 13.13) imports `pkg_resources`, which setuptools 81 removed. Its open advisory only affects building sdists.
- The venv has no pip; use uv (`uv pip sync --python .venv/bin/python requirements.lock`).

## Common Pitfalls

- **Transaction boundaries in `init_db()`**: Column additions, data migrations, and index creation each need their own `conn.commit()` — a rollback in one block (e.g., backfill) will undo uncommitted work from prior blocks.
- **`canonical_link()` regex order AND anchoring matter**: The first matching pattern wins, and the plain-numeric pattern must stay anchored (`/jobs/view/(\d+)(?:[/?#]|$)`) — unanchored, it eats the leading digits of digit-leading slugs (`3d-artist-…` → job_id `"3"`) and silently suppresses future jobs chat-wide.
- **LinkedIn scraping is fragile**: HTML structure changes can silently break parsing. The scraper has no tests — verify manually after any changes.
- **Memory pressure**: JobBERT model is ~500MB. The bot auto-unloads it under memory pressure and reloads on demand. Don't hold references to the model outside `get_jobbert_model()`.
- **Multiple bot.py processes**: Other bots (claude-tracker, opencode-tracker) also run `python bot.py` on this server. The YouTube bot (@Hebius_Bot, `/root/YTTGBot`) was stopped and unregistered on 2026-10-23; its files and unit file (`/root/YTTGBot/yttgbot.service`) are kept. Don't blindly `killall python` or `pkill -f bot.py`.

## Environment

- **Server:** Ubuntu VPS, 8GB RAM
- **Python:** 3.x in `.venv/`
- **DB:** PostgreSQL (local), database `job_alerts`, user `jobbot`
- **Logs:** `diagnostic.log` (main), `alert_monitor.log`, `bot_wrapper.log`
- **Telegram token and DB creds:** in `.env` (gitignored)

## Git Commit Rules

- **Never** include `Co-Authored-By` lines in commit messages.
