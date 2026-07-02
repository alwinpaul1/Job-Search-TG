# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

JobQuestTG — a Telegram bot for LinkedIn + Indeed job searching with AI-powered relevance matching and automated alerts. Single-file monolith (`bot.py`, ~8,800 lines) running on an Ubuntu VPS at 217.154.173.148. Sync python-telegram-bot v13 (no asyncio) — heavy work runs in a global 8-worker `ThreadPoolExecutor`.

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
("Conflict: terminated by other getUpdates request") and double-scrape. All 5 bots
on this VPS are systemd services. If you ever see duplicate instances, kill stray
`run_bot.sh`/`bot.py` processes and `systemctl restart job-search-bot` to get a
single clean instance.

## Architecture

Everything lives in `bot.py`. Key sections in order:

| Lines (approx) | Section |
|----------------|---------|
| 1–130 | Config, constants, env loading, global thread pool |
| 130–470 | Infrastructure classes: `DeadlockDetectableLock`, `CrashMonitor`, `CPUTracker` |
| 470–730 | `DatabasePool` — hand-rolled LIFO pool (adaptive sizing `10 + 0.7 × active_alerts`, clamp 10–50) |
| 885–950 | **Canonicalization functions** (`canonical_link`, `canonical_text`) — central to dedup. `ADMIN_USER_ID` is hardcoded just above (~884), NOT env-read |
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

**Tables:** `alerts`, `sent_jobs`, `user_settings`, `saved_jobs`, `job_details_cache`

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

Entry point: `/admin` (restricted to `ADMIN_USER_ID`, hardcoded at ~line 884). Uses a `ConversationHandler` with six states:

| State | Purpose |
|-------|---------|
| `ADMIN_MENU` | Paginated user list (shows names, alert counts) |
| `ADMIN_USER_ALERTS` | Single user's alerts list + delete-user flow + Pause/Resume-All toggle |
| `ADMIN_ALERT_DETAILS` | Single alert detail view + pause/resume/delete + edit entry points |
| `ADMIN_EDIT_KEYWORDS` | Text input for new keywords |
| `ADMIN_EDIT_LOCATION` | Text input for new location |
| `ADMIN_EDIT_FILTERS` | Filter category/value picker (`adm_flt_*` callbacks) |

**Callback data prefixes:** `adm_user_`, `adm_va_`, `adm_pause_`, `adm_resume_`, `adm_pauseall_`, `adm_resumeall_`, `adm_editkw_`, `adm_editloc_`, `adm_editflt_`, `adm_flt_*`, `adm_delstart_`, `adm_delconf_`, `adm_deluserstart_`, `adm_deluserconf_`, `adm_users`, `adm_upage_`, `adm_back_user_`, `adm_cancel`.

**Pause/Resume-All** (`admin_paused` column): Pause-All sets `is_active=0, admin_paused=1` on the user's active alerts; Resume-All re-activates only `admin_paused=1` rows, never a user's own individually-paused alerts. All four `is_active` writers maintain this invariant.

**Delete user flow:** `admin_delete_user_start` → confirmation → `admin_delete_user_confirm` deletes `saved_jobs`, `alerts` (cascades to `sent_jobs`/`job_details_cache`), and `user_settings`.

**Stale callback fallback** (~line 8700): A standalone `CallbackQueryHandler` (pattern `^adm_`) in group 2 catches admin button presses that arrive after a bot restart (when `ConversationHandler` state is lost) and re-routes them. Edit flows (`adm_editkw_/editloc_/editflt_/flt_*`) can't work without live conversation state, so the fallback answers "session expired, run /admin". Similar stale fallbacks exist in group 0 for My-Alerts buttons and saved-jobs buttons (🗑️/Prev/Next).

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

## Dependency Security Setup (do not "simplify" away)

- `bot.py` starts with a **vendored-urllib3 shim** (before `import telegram`): it loads the repo's `ptb_vendor/urllib3` (1.26.20) under `telegram.vendor.ptb_urllib3.urllib3` in `sys.modules`. Reason: PTB v13's own bundled urllib3 (2016) crashes on Python 3.12 (`ssl.wrap_socket` removed → `NameError: PROTOCOL_SSLv23` on first connect), and PTB's fallback to system urllib3 requires urllib3\<2 — which had 4 high CVEs. With the shim, PTB uses the private py3.12-compatible v1 copy (api.telegram.org traffic only) while the **system urllib3 stays v2** for requests/scrapling/jobquest.
- Removing the shim or `ptb_vendor/` while system urllib3 is v2 crash-loops the bot at startup.
- `python-telegram-bot` must stay **13.13** (13.14/13.15 hard-pin `tornado==6.1`, which has high CVEs; 13.13 allows `tornado>=6.1`).
- `jobquest` was installed from a wheel at `/tmp/jobquest-0.1.12-py3-none-any.whl` that **no longer exists** — never `pip install --force-reinstall` it or touch its version; there is no source to reinstall from.
- transformers is NOT upgraded to 5.x (its high-CVE fix) because pinned sentence-transformers 5.1.0 predates transformers v5; the CVE requires loading untrusted model files, which the bot never does (only TechWolf/JobBERT-v3).

## Common Pitfalls

- **Transaction boundaries in `init_db()`**: Column additions, data migrations, and index creation each need their own `conn.commit()` — a rollback in one block (e.g., backfill) will undo uncommitted work from prior blocks.
- **`canonical_link()` regex order AND anchoring matter**: The first matching pattern wins, and the plain-numeric pattern must stay anchored (`/jobs/view/(\d+)(?:[/?#]|$)`) — unanchored, it eats the leading digits of digit-leading slugs (`3d-artist-…` → job_id `"3"`) and silently suppresses future jobs chat-wide.
- **LinkedIn scraping is fragile**: HTML structure changes can silently break parsing. The scraper has no tests — verify manually after any changes.
- **Memory pressure**: JobBERT model is ~500MB. The bot auto-unloads it under memory pressure and reloads on demand. Don't hold references to the model outside `get_jobbert_model()`.
- **Multiple bot.py processes**: Other bots (odoo-time-tracker, claude-tracker, opencode-tracker, yttgbot) also run `python bot.py` on this server. Don't blindly `killall python` or `pkill -f bot.py`.

## Environment

- **Server:** Ubuntu VPS, 8GB RAM
- **Python:** 3.x in `.venv/`
- **DB:** PostgreSQL (local), database `job_alerts`, user `jobbot`
- **Logs:** `diagnostic.log` (main), `alert_monitor.log`, `bot_wrapper.log`
- **Telegram token and DB creds:** in `.env` (gitignored)

## Git Commit Rules

- **Never** include `Co-Authored-By` lines in commit messages.
