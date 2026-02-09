# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

JobQuestTG — a Telegram bot for LinkedIn job searching with AI-powered relevance matching and automated alerts. Single-file monolith (`bot.py`, ~7000 lines) running on an Ubuntu VPS at 217.154.173.148.

Public bot: [@JobQuestTG_Bot](https://t.me/JobQuestTG_Bot)

## Running & Deploying

```bash
# Development
cd /root/Job-Search-TG && source .venv/bin/activate && python bot.py

# Production (auto-restart wrapper with memory monitoring)
./run_bot.sh start|stop|status|restart

# The wrapper is managed by run_bot.sh (not systemd for the job bot itself)
# Process ID stored in bot.pid
```

The wrapper (`run_bot.sh`) handles auto-restart on crash, 3-tier memory monitoring (warning 4GB / critical 5.5GB / extreme 6.5GB), and exponential backoff for rapid failures.

## Architecture

Everything lives in `bot.py`. Key sections in order:

| Lines (approx) | Section |
|----------------|---------|
| 1–90 | Config, constants, env loading |
| 130–470 | Infrastructure classes: `DeadlockDetectableLock`, `CrashMonitor`, `DatabasePool`, `CPUTracker` |
| 470–570 | DB connection pool (adaptive sizing: `10 + active_alerts * 0.5`) |
| 730–880 | Memory management, model lifecycle |
| 880–950 | **Canonicalization functions** (`canonical_link`, `canonical_text`) — central to dedup |
| 950–1050 | Date parsing, utility functions |
| 1050–1240 | `init_db()` — schema creation, migrations, index creation |
| 2465–3380 | ML classifiers: `JobRelevanceEngine`, `DynamicTermClassifier`, `TFIDFTermClassifier`, `CorpusOnlyClassifier`, `PureMathematicalClassifier`, `UltraPureDynamicClassifier` |
| 3389–3800 | `AdaptiveJobBERTMatcher` — primary semantic matching (TechWolf/JobBERT-v3) |
| 3800–4400 | LinkedIn scraper (`scrape_linkedin`, `scrape_linkedin_with_adaptive_jobbert`) |
| 4400–5500 | Telegram handlers (search flow, alert CRUD, preferences, saved jobs) |
| 5500–5700 | Alert-update flow (edit alert → rescrape → dedup → baseline) |
| 5800–6200 | **Alert checker** (`check_single_alert`) — the main background loop that sends new jobs |
| 6200–6900 | `main()`, handler registration, scheduler setup |

## Database

PostgreSQL (`job_alerts` database, user `jobbot`). Config via `DATABASE_URL` env var or individual `POSTGRES_*` vars in `.env`.

**Tables:** `alerts`, `sent_jobs`, `user_settings`, `saved_jobs`, `job_details_cache`

**Schema migrations** are inline in `init_db()` (~line 1138) using `ADD COLUMN IF NOT EXISTS` pattern. No migration framework — columns are added idempotently on startup.

## Dedup System (Critical Path)

Jobs are deduped in `sent_jobs` via two conditions:
1. **Exact match:** `(chat_id, job_id)` — `job_id` is the numeric LinkedIn ID extracted by `canonical_link()`
2. **Fuzzy match:** `(chat_id, canonical_title, canonical_company, canonical_location)` — normalized text via `canonical_text()`

Three code paths insert into `sent_jobs` (all must stay in sync):
- **Baseline population** (~line 4800): When a new alert is created
- **Alert-update** (~line 5636): When alert filters are edited
- **Main alert loop** (~line 6090): When new jobs are found during scheduled checks

The in-memory dedup in alert-update (~line 5610) uses a set of `(title, company, location)` tuples and must match the DB query logic.

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

## Common Pitfalls

- **Transaction boundaries in `init_db()`**: Column additions, data migrations, and index creation each need their own `conn.commit()` — a rollback in one block (e.g., backfill) will undo uncommitted work from prior blocks.
- **`canonical_link()` regex order matters**: The first matching pattern wins. The slug pattern (`/jobs/view/slug-here-12345`) must come before the generic fallback.
- **LinkedIn scraping is fragile**: HTML structure changes can silently break parsing. The scraper has no tests — verify manually after any changes.
- **Memory pressure**: JobBERT model is ~500MB. The bot auto-unloads it under memory pressure and reloads on demand. Don't hold references to the model outside `get_jobbert_model()`.
- **Multiple bot.py processes**: Other bots (odoo-time-tracker, claude-tracker, opencode-tracker, yttgbot) also run `python bot.py` on this server. Don't blindly `killall python` or `pkill -f bot.py`.

## Environment

- **Server:** Ubuntu VPS, 8GB RAM
- **Python:** 3.x in `.venv/`
- **DB:** PostgreSQL (local), database `job_alerts`, user `jobbot`
- **Logs:** `diagnostic.log` (main), `alert_monitor.log`, `bot_wrapper.log`
- **Telegram token and DB creds:** in `.env` (gitignored)
