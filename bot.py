import logging
import os
import warnings
import time
import json
import sqlite3
import html
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
try:
    from telegram.constants import ParseMode
except ImportError:
    # For older versions of python-telegram-bot
    from telegram import ParseMode

# Handle different filter names across versions
try:
    # Try newer version first
    from telegram.ext import filters
    if hasattr(filters, 'Text'):
        TEXT_FILTER = filters.Text
        COMMAND_FILTER = filters.COMMAND
    elif hasattr(filters, 'TEXT'):
        TEXT_FILTER = filters.TEXT
        COMMAND_FILTER = filters.COMMAND
    else:
        # Fallback for very old versions
        from telegram.ext import Filters
        TEXT_FILTER = Filters.text
        COMMAND_FILTER = Filters.command
        filters = Filters
except ImportError:
    # For older versions
    from telegram.ext import Filters
    TEXT_FILTER = Filters.text
    COMMAND_FILTER = Filters.command
    filters = Filters

from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler,
)
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from datetime import datetime, timedelta
import re
import pytz

import unicodedata

# --- Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global thread pool for concurrent operations
executor = ThreadPoolExecutor(max_workers=10)

# Global lock for database operations
db_lock = threading.Lock()

# User-specific operation tracking
user_operations = {}
user_operations_lock = threading.Lock()

# Optional imports for enhanced features
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    logger.warning("⚠️ scikit-learn not available. Using basic relevance scoring only.")
    SKLEARN_AVAILABLE = False
    
    # Fallback cosine similarity function
    def cosine_similarity(a, b):
        """Simple cosine similarity fallback when sklearn is not available"""
        import numpy as np
        if np is not None:
            dot_product = np.dot(a, b.T)
            norm_a = np.linalg.norm(a, axis=1, keepdims=True)
            norm_b = np.linalg.norm(b, axis=1, keepdims=True)
            return dot_product / (norm_a * norm_b.T)
        else:
            # Very basic fallback - just return random similarities
            if hasattr(a, 'shape'):
                return [[0.5] * len(b) for _ in range(len(a))]
            else:
                return [[0.5]]

# Advanced semantic matching imports
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
    logger.info("✅ Sentence transformers available for advanced semantic matching")
except ImportError:
    logger.warning("⚠️ sentence-transformers not available. Install with: pip install sentence-transformers")
    SENTENCE_TRANSFORMERS_AVAILABLE = False

# Import numpy separately for statistical operations
try:
    import numpy as np
except ImportError:
    logger.warning("⚠️ numpy not available for statistical analysis")
    np = None

warnings.filterwarnings(
    action='ignore',
    message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message.",
    category=UserWarning,
    module='telegram.ext.conversationhandler'
)

# Load environment variables
load_dotenv()



# --- Text and Link Canonicalization Functions ---
def canonical_link(url: str) -> str:
    """Extract only the numeric LinkedIn job ID for consistent deduplication."""
    # Try multiple patterns to extract LinkedIn job ID
    patterns = [
        r'/jobs/view/(\d+)',  # Standard: /jobs/view/123456
        r'/jobs/(\d+)/',      # Alternative: /jobs/123456/
        r'job[_-](\d+)',      # Job ID in parameter: job_123456 or job-123456
        r'jobId[=:](\d+)',    # JobId parameter: jobId=123456 or jobId:123456
    ]

    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)

    # If no job ID found, normalize the URL by removing query params and fragments
    return url.lower().split('?')[0].split('#')[0].rstrip('/')

def canonical_text(txt: str) -> str:
    """Normalize text: lowercase, strip accents, normalize spaces."""
    if not txt:
        return ""
    # Remove accents and non-ASCII characters
    txt = unicodedata.normalize('NFKD', txt).encode('ascii', 'ignore').decode()
    # Normalize whitespace and convert to lowercase
    return re.sub(r'\s+', ' ', txt).strip().lower()

def parse_date_posted_to_datetime(date_str):
    """Convert LinkedIn's 'X days ago' format to actual datetime."""
    date_str = date_str.lower().strip()
    now = datetime.now(pytz.UTC)

    # Handle various formats
    if 'hour' in date_str:
        hours = int(re.search(r'\d+', date_str).group()) if re.search(r'\d+', date_str) else 1
        return now - timedelta(hours=hours)
    elif 'day' in date_str:
        days = int(re.search(r'\d+', date_str).group()) if re.search(r'\d+', date_str) else 1
        return now - timedelta(days=days)
    elif 'week' in date_str:
        weeks = int(re.search(r'\d+', date_str).group()) if re.search(r'\d+', date_str) else 1
        return now - timedelta(weeks=weeks)
    elif 'month' in date_str:
        months = int(re.search(r'\d+', date_str).group()) if re.search(r'\d+', date_str) else 1
        return now - timedelta(days=30 * months)
    elif 'year' in date_str:
        years = int(re.search(r'\d+', date_str).group()) if re.search(r'\d+', date_str) else 1
        return now - timedelta(days=365 * years)
    else:
        return now  # Default to now if we can't parse it

# --- Helper Functions ---
def safe_answer_callback_query(query):
    """Safely answer callback queries with timeout handling."""
    try:
        query.answer()
    except (telegram.error.TimedOut, telegram.error.BadRequest):
        pass  # Ignore timeout and expired callback errors

def safe_edit_message(query, text, reply_markup=None, parse_mode=None, disable_web_page_preview=None):
    """Safely edit messages with error handling."""
    try:
        if reply_markup:
            query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        else:
            query.edit_message_text(text=text, parse_mode=parse_mode)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Failed to edit message: {e}")
    except telegram.error.TimedOut:
        logger.warning("Message edit timed out")
    except Exception as e:
        logger.error(f"Unexpected error editing message: {e}")

# --- Constants and State Definitions ---
(
    MAIN_MENU, PREFERENCES_MENU, GET_SEARCH_KEYWORD, GET_SEARCH_LOCATION,
    EXPERIENCE_MENU, JOB_TYPE_MENU, DATE_POSTED_MENU, WORKPLACE_MENU, BROWSING,
    ALERTS_MENU, MY_ALERTS, ADD_ALERT_KEYWORD, ADD_ALERT_LOCATION, ALERT_PREFERENCES,
    EDIT_ALERT_PREFERENCES, SET_TIMEZONE
) = range(16)

JOBS_PER_PAGE = 5
MAX_SCRAPE_PAGES = 5

DATE_POSTED_OPTIONS = {"Past 24 hours": "r86400", "Past Week": "r604800", "Past Month": "r2592000"}
EXPERIENCE_LEVELS = {"Internship": "1", "Entry level": "2", "Associate": "3", "Mid-Senior level": "4", "Director": "5", "Executive": "6"}
JOB_TYPES = {"Full-time": "F", "Part-time": "P", "Contract": "C", "Temporary": "T", "Internship": "I"}
WORKPLACE_TYPES = {"On-site": "1", "Remote": "2", "Hybrid": "3"}

# --- Database Setup ---
def init_db():
    """Initialize the SQLite database and create/update tables."""
    conn = sqlite3.connect('job_alerts.db', check_same_thread=False)
    cursor = conn.cursor()

    # Table for storing user alerts
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            keywords TEXT NOT NULL,
            location TEXT NOT NULL,
            filters TEXT,
            is_active INTEGER DEFAULT 1,
            last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Table for tracking jobs sent, now with robust deduplication
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_jobs (
            alert_id INTEGER,
            chat_id INTEGER NOT NULL,
            job_link TEXT NOT NULL,
            job_id TEXT NOT NULL,
            job_title TEXT NOT NULL,
            company TEXT NOT NULL,
            canonical_title TEXT NOT NULL,
            canonical_company TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (alert_id, job_link),
            FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
        )
    ''')

    # Add new user_settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id INTEGER PRIMARY KEY,
            timezone TEXT
        )
    ''')

    # --- Safe Table Migration ---
    # Check if new columns exist and add them if they don't for backwards compatibility
    try:
        cursor.execute("SELECT job_title, company FROM sent_jobs LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("Upgrading sent_jobs table: adding job_title and company columns...")
        try:
            cursor.execute("ALTER TABLE sent_jobs ADD COLUMN job_title TEXT NOT NULL DEFAULT 'N/A'")
        except sqlite3.OperationalError: pass # Column might exist from a partial migration
        try:
            cursor.execute("ALTER TABLE sent_jobs ADD COLUMN company TEXT NOT NULL DEFAULT 'N/A'")
        except sqlite3.OperationalError: pass # Column might exist

    # Check and add new columns for robust deduplication
    columns_to_add = [
        ("chat_id", "INTEGER NOT NULL DEFAULT 0"),
        ("job_id", "TEXT NOT NULL DEFAULT ''"),
        ("canonical_title", "TEXT NOT NULL DEFAULT ''"),
        ("canonical_company", "TEXT NOT NULL DEFAULT ''"),
        ("sent_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    ]

    for col_name, col_def in columns_to_add:
        try:
            cursor.execute(f"SELECT {col_name} FROM sent_jobs LIMIT 1")
        except sqlite3.OperationalError:
            logger.info(f"Adding {col_name} column to sent_jobs table...")
            try:
                cursor.execute(f"ALTER TABLE sent_jobs ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError as e:
                logger.warning(f"Failed to add {col_name}: {e}")

    # Migrate existing data to new format
    try:
        # Update job_id for existing records
        cursor.execute("UPDATE sent_jobs SET job_id = ? WHERE job_id = '' OR job_id IS NULL", ("",))
        rows = cursor.execute("SELECT rowid, job_link, job_title, company FROM sent_jobs WHERE job_id = ''").fetchall()
        for row in rows:
            job_id = canonical_link(row[1])
            canonical_title = canonical_text(row[2])
            canonical_company = canonical_text(row[3])
            cursor.execute(
                "UPDATE sent_jobs SET job_id = ?, canonical_title = ?, canonical_company = ? WHERE rowid = ?",
                (job_id, canonical_title, canonical_company, row[0])
            )

        # Update chat_id for existing records by joining with alerts
        cursor.execute("""
            UPDATE sent_jobs
            SET chat_id = (SELECT chat_id FROM alerts WHERE alerts.id = sent_jobs.alert_id)
            WHERE chat_id = 0 OR chat_id IS NULL
        """)

        logger.info("Migrated existing sent_jobs data to new format")
    except Exception as e:
        logger.warning(f"Migration warning (non-critical): {e}")

    # Create indexes for efficient deduplication
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_jobid ON sent_jobs(alert_id, job_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_jobid ON sent_jobs(chat_id, job_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_canonical ON sent_jobs(chat_id, canonical_title, canonical_company)")
        logger.info("Created deduplication indexes")
    except Exception as e:
        logger.warning(f"Index creation warning: {e}")

    conn.commit()
    conn.close()
    logger.info("Database initialized and schema updated successfully.")

def get_db_connection():
    """Get a database connection with proper locking."""
    with db_lock:
        conn = sqlite3.connect('job_alerts.db', check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

# --- Data Persistence Helper ---
def get_user_prefs(context: CallbackContext) -> dict:
    """Safely get user preferences, initializing if not present."""
    if 'preferences' not in context.user_data:
        context.user_data['preferences'] = {
            'experience': {},
            'job_types': {},
            'date_posted': {},
            'workplace': {}
        }
    return context.user_data['preferences']

# --- Concurrency Management ---
def register_user_operation(user_id: int, operation_type: str):
    """Register that a user is performing an operation."""
    with user_operations_lock:
        if user_id not in user_operations:
            user_operations[user_id] = {}
        user_operations[user_id][operation_type] = time.time()

def unregister_user_operation(user_id: int, operation_type: str):
    """Unregister a user operation."""
    with user_operations_lock:
        if user_id in user_operations and operation_type in user_operations[user_id]:
            del user_operations[user_id][operation_type]
            if not user_operations[user_id]:  # Remove user if no operations
                del user_operations[user_id]

def is_user_busy(user_id: int, operation_type: str = None) -> bool:
    """Check if a user is currently busy with any operation or a specific one."""
    with user_operations_lock:
        if user_id not in user_operations:
            return False
        if operation_type:
            return operation_type in user_operations[user_id]
        return len(user_operations[user_id]) > 0

def safe_progress_update(progress_msg, text: str, parse_mode=None):
    """Safely update progress message without interfering with other users."""
    if not progress_msg:
        return
    try:
        progress_msg.edit_text(text=text, parse_mode=parse_mode)
    except telegram.error.BadRequest as e:
        if 'not modified' not in str(e).lower():
            logger.warning(f"Progress message update failed: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error updating progress: {e}")

def run_concurrent_operation(func, *args, **kwargs):
    """Run an operation in a separate thread to avoid blocking other users."""
    return executor.submit(func, *args, **kwargs)

# --- UI Generation Functions ---
def make_main_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    text = "👋 Welcome to Job Quest!"
    keyboard = [
        [InlineKeyboardButton("🚀 Start Search", callback_data="start_search")],
        [InlineKeyboardButton("🔔 Set Alert", callback_data="set_alert")],
        [InlineKeyboardButton("📋 Preferences", callback_data="prefs")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def make_preferences_menu(context: CallbackContext, chat_id: int) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    experience = ", ".join(prefs['experience'].keys()) or "Not Set"
    job_types = ", ".join(prefs['job_types'].keys()) or "Not Set"
    date_posted = list(prefs['date_posted'].keys())[0] if prefs['date_posted'] else "Any"
    workplace = list(prefs['workplace'].keys())[0] if prefs['workplace'] else "Any"

    # Get user timezone
    conn = get_db_connection()
    tz_row = conn.execute("SELECT timezone FROM user_settings WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    user_timezone = tz_row['timezone'] if tz_row and tz_row['timezone'] else "Not Set (UTC)"

    text = (
        "⚙️ *Preferences*\n\n"
        f"∙ *Timezone:* `{user_timezone}`\n"
        f"∙ *Date Posted:* `{date_posted}`\n"
        f"∙ *Workplace:* `{workplace}`\n"
        f"∙ *Experience:* `{experience}`\n"
        f"∙ *Job Types:* `{job_types}`"
    )
    keyboard = [
        [InlineKeyboardButton("🗓️ Set Date Posted", callback_data="set_date_posted"), InlineKeyboardButton("🏢 Set Workplace", callback_data="set_workplace")],
        [InlineKeyboardButton("🎓 Set Experience", callback_data="set_experience"), InlineKeyboardButton("📝 Set Job Types", callback_data="set_job_types")],
        [InlineKeyboardButton("🌍 Set Timezone", callback_data="set_timezone")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def make_date_posted_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    selected_value = list(prefs['date_posted'].values())[0] if prefs['date_posted'] else None

    text = "🗓️ Choose Date Posted Filter"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"dp_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="dp_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="dp_done")])
    return text, InlineKeyboardMarkup(keyboard)

def make_workplace_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    selected_value = list(prefs['workplace'].values())[0] if prefs['workplace'] else None

    text = "🏢 Choose Workplace Type"
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"wt_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="wt_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="wt_done")])
    return text, InlineKeyboardMarkup(keyboard)

def make_multi_select_menu(context: CallbackContext, menu_type: str) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)

    if menu_type == 'experience':
        title = "🎓 Choose Your Experience Levels"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs['experience']
        callback_prefix = "exp"
    else: # job_type
        title = "📝 Choose Your Job Types"
        options_dict = JOB_TYPES
        selected_options = prefs['job_types']
        callback_prefix = "jt"

    text = f"{title}\n\n" \
           "▫️ Click to select/deselect options\n" \
           "▫️ Click 'Done' when finished."

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"{callback_prefix}_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data=f"{callback_prefix}_done")])
    return text, InlineKeyboardMarkup(keyboard)


# --- Start & Main Menu ---
def start(update: Update, context: CallbackContext):
    # Debounce the /start command
    user_id = update.effective_user.id
    now = time.time()
    last_call = context.user_data.get('last_start_call', 0)
    if now - last_call < 2:
        return
    context.user_data['last_start_call'] = now

    text, keyboard = make_main_menu(context)
    update.message.reply_text(text, reply_markup=keyboard)
    return MAIN_MENU

def main_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_main_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
    return MAIN_MENU

def about(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.edit_message_text(
        "This bot helps you find jobs on LinkedIn.\n\n"
        "Developed by Alwin.\n"
        "Use /start to begin.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
    )
    return MAIN_MENU

# --- Search and Preferences Flow ---
def start_search_flow(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.edit_message_text("Please enter the job title or keywords.")
    return GET_SEARCH_KEYWORD

def keyword_received(update: Update, context: CallbackContext):
    context.user_data['search_keywords'] = update.message.text
    update.message.reply_text("Great. Now, what location are you interested in?")
    return GET_SEARCH_LOCATION

def location_received(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if user is already busy with a search
    if is_user_busy(user_id, 'search'):
        update.message.reply_text("⏳ You already have a search in progress. Please wait for it to complete.")
        return GET_SEARCH_LOCATION

    context.user_data['search_location'] = update.message.text
    progress_msg = update.message.reply_text("🚀 Kicking off the search...")

    # Register the search operation
    register_user_operation(user_id, 'search')

    # Run search in background thread
    future = run_concurrent_operation(run_scrape_threaded, update, context, progress_msg)
    context.user_data['search_future'] = future

    return BROWSING

# --- Preferences Flow ---
def preferences_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_preferences_menu(context, query.from_user.id)
    query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return PREFERENCES_MENU

def show_date_posted_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_date_posted_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
    return DATE_POSTED_MENU

def date_posted_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_user_prefs(context)

    _, option_id, option_text = update.callback_query.data.split('_', 2)

    if option_id == 'clear':
        prefs['date_posted'] = {}
    else:
        # Check if this option is already selected - if yes, deselect it
        if option_id in prefs['date_posted'].values():
            prefs['date_posted'] = {}
        else:
            prefs['date_posted'] = {option_text: option_id}

    # Re-render the menu to show the change
    text, keyboard = make_date_posted_menu(context)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return DATE_POSTED_MENU

def show_workplace_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_workplace_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
    return WORKPLACE_MENU

def workplace_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_user_prefs(context)

    _, option_id, option_text = update.callback_query.data.split('_', 2)

    if option_id == 'clear':
        prefs['workplace'] = {}
    else:
        # Check if this option is already selected - if yes, deselect it
        if option_id in prefs['workplace'].values():
            prefs['workplace'] = {}
        else:
            prefs['workplace'] = {option_text: option_id}

    # Re-render the menu to show the change
    text, keyboard = make_workplace_menu(context)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return WORKPLACE_MENU

def ask_for_preference(update: Update, context: CallbackContext, pref_type: str):
    query = update.callback_query
    query.answer()

    if pref_type == 'keywords':
        query.edit_message_text("Please send your job keywords, separated by a comma (e.g., AI Engineer, Python Developer).")
        return GET_KEYWORD
    else: # locations
        query.edit_for_preference(update, context, pref_type)
        return GET_LOCATION

def save_text_preference(update: Update, context: CallbackContext, pref_type: str):
    prefs = get_user_prefs(context)
    user_input = [item.strip() for item in update.message.text.split(',')]
    prefs[pref_type] = user_input

    update.message.reply_text(f"✅ Your {pref_type} have been saved!")

    text, keyboard = make_preferences_menu(context, update.message.chat_id)
    update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return PREFERENCES_MENU

# --- Multi-Select Menu Flow (Experience & Job Type) ---
def show_multi_select_menu(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()
    text, keyboard = make_multi_select_menu(context, menu_type)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return EXPERIENCE_MENU if menu_type == 'experience' else JOB_TYPE_MENU

def toggle_multi_select_option(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()
    prefs = get_user_prefs(context)

    _, option_id, option_text = query.data.split('_', 2)

    if menu_type == 'experience':
        selected_dict = prefs['experience']
    else: # job_type
        selected_dict = prefs['job_types']

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next((k for k, v in selected_dict.items() if v == option_id), None)
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    text, keyboard = make_multi_select_menu(context, menu_type)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return EXPERIENCE_MENU if menu_type == 'experience' else JOB_TYPE_MENU


# --- Scraping Logic ---
def parse_date_posted(date_str):
    date_str = date_str.lower().strip()
    now = datetime.now()
    if 'hour' in date_str: return now - timedelta(hours=int(re.search(r'\d+', date_str).group()))
    if 'day' in date_str: return now - timedelta(days=int(re.search(r'\d+', date_str).group()))
    if 'week' in date_str: return now - timedelta(weeks=int(re.search(r'\d+', date_str).group()))
    if 'month' in date_str: return now - timedelta(days=30 * int(re.search(r'\d+', date_str).group()))
    if 'year' in date_str: return now - timedelta(days=365 * int(re.search(r'\d+', date_str).group()))
    return now

def create_paginated_job_message(jobs, page):
    start_index = page * JOBS_PER_PAGE
    end_index = start_index + JOBS_PER_PAGE
    total_pages = (len(jobs) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE

    message_text = f"<b>Displaying page {page + 1} of {total_pages}</b>\n\n"
    for job in jobs[start_index:end_index]:
        # Use HTML formatting and escape special characters to prevent parsing errors
        title = html.escape(job['Title'])
        company = html.escape(job['Company'])
        location = html.escape(job['Location'])
        date_posted = html.escape(job['Date Posted'])

        message_text += f"<b>{title}</b>\n"
        message_text += f"<i>{company}</i> - {location}\n"
        message_text += f"Posted: {date_posted}\n"
        message_text += f"<a href='{job['Link']}'>View Job</a>\n\n"

    if not jobs[start_index:end_index]: return "No jobs to display.", None

    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page - 1}"))

    if total_pages > 1:
        row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ignore"))

    if end_index < len(jobs):
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page + 1}"))

    buttons = [row, [InlineKeyboardButton("❌ Close", callback_data="close")]]
    return message_text, InlineKeyboardMarkup(buttons)

def get_job_description(job_link):
    """Fetch job description from LinkedIn job page with rate limiting."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36',
        }

        # Add delay to avoid rate limiting
        time.sleep(2)

        response = requests.get(job_link, headers=headers, timeout=15)

        # Handle rate limiting gracefully
        if response.status_code == 429:
            logger.warning(f"Rate limited for {job_link}, using title only")
            return "Description unavailable due to rate limiting"

        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')

        # Try multiple selectors for job description
        description_selectors = [
            '.show-more-less-html__markup',
            '.description__text',
            '[data-automation-id="jobPostingDescription"]',
            '.jobs-description-content__text'
        ]

        for selector in description_selectors:
            desc_elem = soup.select_one(selector)
            if desc_elem:
                return desc_elem.get_text(strip=True)[:1000]  # Reduced to 1000 chars

        return "No description available"
    except Exception as e:
        logger.warning(f"Failed to fetch job description for {job_link}: {e}")
        return "No description available"

# --- Enhanced Job Relevance Engine ---
class JobRelevanceEngine:
    def __init__(self):
        # Weightings for each factor (adjust as needed)
        self.weights = {
            'title_relevance': 0.40,      # Job title match (highest priority)
            'company_relevance': 0.15,    # Company relevance
            'recency_boost': 0.15,        # Recent posting bonus
            'location_match': 0.10,       # Location relevance
            'exclusion_penalty': 0.20     # Penalty for excluded terms (optional)
        }
        # You can optionally add exclusion terms here if you want to filter out certain jobs
        self.exclusion_terms = set()
        # You can also add negative keywords at runtime if needed
        # self.exclusion_terms.update(['intern', 'sales', 'marketing'])

    def calculate_relevance_score(self, job, search_query):
        """Calculate comprehensive job relevance score (0-1)"""
        total_score = 0.0

        # Title relevance (case-insensitive, word boundary preferred)
        title_score = self._calculate_field_relevance(job['Title'], search_query)
        total_score += title_score * self.weights['title_relevance']

        # Company relevance
        company_score = self._calculate_field_relevance(job['Company'], search_query)
        total_score += company_score * self.weights['company_relevance']

        # Recency boost (newer jobs score higher)
        recency_score = self._calculate_recency_score(job['Date Posted'])
        total_score += recency_score * self.weights['recency_boost']

        # Location match (case-insensitive, simple contains)
        location_score = self._calculate_field_relevance(job['Location'], search_query)
        total_score += location_score * self.weights['location_match']

        # Exclusion penalty (optional)
        if self.exclusion_terms:
            exclusion_penalty = self._calculate_exclusion_penalty(job)
            total_score -= exclusion_penalty * self.weights['exclusion_penalty']

        return max(0.0, min(1.0, total_score))  # Clamp between 0-1

    def _calculate_field_relevance(self, field_text, query):
        """Calculate relevance for a field (title, company, location)"""
        field_lower = field_text.lower()
        query_words = query.lower().split()
        if not query_words:
            return 0.0

        score = 0.0
        for word in query_words:
            # Word boundary match (preferred)
            if re.search(r'\b' + re.escape(word) + r'\b', field_lower):
                score += 1.0
            # Partial match (lower score)
            elif word in field_lower:
                score += 0.5

        return score / len(query_words)

    def _calculate_recency_score(self, date_posted):
        """Calculate recency score (0-1)"""
        try:
            posted_date = parse_date_posted_to_datetime(date_posted)
            now = datetime.now(pytz.UTC)
            hours_old = (now - posted_date).total_seconds() / 3600

            # Exponential decay scoring
            if hours_old <= 24:
                return 1.0  # Perfect score for last 24 hours
            elif hours_old <= 168:  # 1 week
                return 0.8
            elif hours_old <= 720:  # 1 month
                return 0.5
            else:
                return 0.1
        except:
            return 0.3  # Default for unparseable dates

    def _calculate_exclusion_penalty(self, job):
        """Penalize jobs with unwanted terms"""
        job_text = f"{job['Title']} {job['Company']}".lower()
        penalty = 0.0

        for term in self.exclusion_terms:
            if term in job_text:
                penalty += 0.3  # Heavy penalty for exclusion terms

        return min(penalty, 1.0)  # Cap penalty at 1.0

# --- Dynamic Term Classification System ---
class DynamicTermClassifier:
    def __init__(self):
        self.job_type_indicators = set()
        self.domain_terms = set()
        self.analyzed = False

    def analyze_job_corpus(self, jobs):
        """Dynamically identify job type vs domain terms from actual job data"""
        if self.analyzed:
            return

        from collections import Counter
        import re

        # Extract all terms from job titles
        all_terms = []
        job_titles = [job['Title'] for job in jobs]

        for title in job_titles:
            # Extract meaningful words (2+ characters, alphabetic)
            terms = re.findall(r'\b[a-zA-Z]{2,}\b', title.lower())
            all_terms.extend(terms)

        # Calculate term frequencies
        term_counts = Counter(all_terms)
        total_jobs = len(job_titles)

        # Categorize terms based on usage patterns
        for term, count in term_counts.items():
            occurrence_rate = count / total_jobs

            # Terms appearing in 30%+ of jobs are likely job type indicators
            if occurrence_rate >= 0.3:
                self.job_type_indicators.add(term)
            # Terms appearing in 5-25% are likely domain-specific
            elif 0.05 <= occurrence_rate <= 0.25:
                self.domain_terms.add(term)

        self.analyzed = True
        logger.info(f"Identified {len(self.job_type_indicators)} job type terms, {len(self.domain_terms)} domain terms")

    def classify_query_terms(self, query, jobs):
        """Classify query terms as job type vs domain terms"""
        if not self.analyzed:
            self.analyze_job_corpus(jobs)

        query_words = query.lower().split()

        job_type_terms = [w for w in query_words if w in self.job_type_indicators]
        domain_terms = [w for w in query_words if w in self.domain_terms]

        return {
            'job_type_terms': job_type_terms,
            'domain_terms': domain_terms,
            'has_domain_specificity': len(domain_terms) > 0,
            'job_type_ratio': len(job_type_terms) / len(query_words) if query_words else 0
        }

class TFIDFTermClassifier:
    def __init__(self):
        if SKLEARN_AVAILABLE:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.vectorizer = TfidfVectorizer(
                ngram_range=(1, 1),
                max_features=1000,
                stop_words='english'
            )
        else:
            self.vectorizer = None
        self.job_type_threshold = 0.1  # Lower TF-IDF = more common/generic
        self.domain_threshold = 0.3    # Higher TF-IDF = more specific

    def analyze_terms(self, jobs):
        """Use TF-IDF to classify terms by importance"""
        if not SKLEARN_AVAILABLE or not self.vectorizer:
            return {'job_type_terms': set(), 'domain_terms': set()}

        job_titles = [job['Title'] for job in jobs]

        try:
            # Fit TF-IDF on job titles
            tfidf_matrix = self.vectorizer.fit_transform(job_titles)
            feature_names = self.vectorizer.get_feature_names_out()

            # Calculate average TF-IDF score for each term
            if np is not None:
                mean_scores = np.array(tfidf_matrix.mean(axis=0)).flatten()
            else:
                # Fallback without numpy
                mean_scores = [tfidf_matrix[:, i].mean() for i in range(tfidf_matrix.shape[1])]

            # Classify terms based on TF-IDF scores
            job_type_terms = []
            domain_terms = []

            for i, term in enumerate(feature_names):
                score = mean_scores[i]

                if score <= self.job_type_threshold:
                    # Low TF-IDF = common across many jobs = job type indicator
                    job_type_terms.append(term)
                elif score >= self.domain_threshold:
                    # High TF-IDF = specific to certain jobs = domain term
                    domain_terms.append(term)

            return {
                'job_type_terms': set(job_type_terms),
                'domain_terms': set(domain_terms)
            }
        except Exception as e:
            logger.warning(f"TF-IDF analysis failed: {e}")
            return {'job_type_terms': set(), 'domain_terms': set()}

    def classify_query(self, query, jobs):
        """Classify query terms using TF-IDF analysis"""
        term_classification = self.analyze_terms(jobs)
        query_words = set(query.lower().split())

        job_type_matches = query_words.intersection(term_classification['job_type_terms'])
        domain_matches = query_words.intersection(term_classification['domain_terms'])

        return {
            'job_type_terms': list(job_type_matches),
            'domain_terms': list(domain_matches),
            'has_domain_specificity': len(domain_matches) > 0
        }

class CorpusOnlyClassifier:
    """100% Pattern-Free Classifier - learns everything from data"""
    def __init__(self):
        self.learned_morphemes = {'job_type': set(), 'domain': set()}
        self.analyzed = False

    def learn_morphemes_from_corpus(self, jobs):
        """Learn morphological patterns directly from job data - no hardcoding"""
        if self.analyzed:
            return

        from collections import Counter

        # Extract all words from job titles
        job_titles = [job['Title'] for job in jobs]
        all_words = []
        for title in job_titles:
            words = re.findall(r'\b[a-zA-Z]{3,}\b', title.lower())
            all_words.extend(words)

        word_counter = Counter(all_words)
        total_words = len(all_words)

        # Learn suffix patterns from high-frequency words
        suffix_counter = Counter()
        prefix_counter = Counter()

        for word, count in word_counter.items():
            if count >= 3 and len(word) >= 4:  # Only learn from words that appear multiple times
                # Learn suffixes (last 2-3 characters)
                suffix_counter[word[-2:]] += count
                suffix_counter[word[-3:]] += count

                # Learn prefixes (first 3 characters)
                prefix_counter[word[:3]] += count

        # Identify frequent morphemes (appearing in 1%+ of corpus)
        min_frequency = total_words * 0.01

        frequent_suffixes = {suffix for suffix, count in suffix_counter.items()
                           if count >= min_frequency and len(suffix) >= 2}
        frequent_prefixes = {prefix for prefix, count in prefix_counter.items()
                           if count >= min_frequency}

        # Categorize morphemes based on their distribution across job types vs domains
        # (This would require more sophisticated analysis in a real implementation)

        self.learned_morphemes['job_type'] = frequent_suffixes.union(frequent_prefixes)
        self.analyzed = True

        logger.info(f"Learned {len(self.learned_morphemes['job_type'])} morphological patterns from corpus")

    def classify_terms_by_learned_patterns(self, query_words, jobs):
        """Classify terms using patterns learned from the actual job corpus"""
        if not self.analyzed:
            self.learn_morphemes_from_corpus(jobs)

        job_type_terms = []
        domain_terms = []

        for word in query_words:
            word_lower = word.lower()

            # Check against learned patterns
            has_job_type_morpheme = any(
                word_lower.endswith(suffix) or word_lower.startswith(prefix)
                for suffix in self.learned_morphemes['job_type']
                for prefix in self.learned_morphemes['job_type']
            )

            if has_job_type_morpheme:
                job_type_terms.append(word_lower)
            else:
                # Everything else is potentially domain-specific
                domain_terms.append(word_lower)

        return {
            'job_type_terms': job_type_terms,
            'domain_terms': domain_terms,
            'has_domain_specificity': len(domain_terms) > 0
        }

class PureMathematicalClassifier:
    """Ultra-Pure Classifier using only mathematical/statistical methods"""
    def __init__(self):
        pass

    def classify_by_mathematical_properties(self, query_words, jobs):
        """Classify using only mathematical properties of words in the corpus"""
        from collections import Counter

        # Get all job titles
        job_titles = [job['Title'] for job in jobs]
        all_corpus_words = []
        for title in job_titles:
            words = re.findall(r'\b[a-zA-Z]{2,}\b', title.lower())
            all_corpus_words.extend(words)

        corpus_counter = Counter(all_corpus_words)
        total_corpus_words = len(all_corpus_words)

        job_type_terms = []
        domain_terms = []

        for word in query_words:
            word_lower = word.lower()
            word_frequency = corpus_counter.get(word_lower, 0)
            word_probability = word_frequency / total_corpus_words if total_corpus_words > 0 else 0

            # Mathematical classification based on statistical properties only:

            # 1. Frequency-based classification
            if word_probability > 0.02:  # Appears in >2% of corpus
                job_type_terms.append(word_lower)

            # 2. Length-based classification (shorter words tend to be job types)
            elif len(word) <= 4:
                job_type_terms.append(word_lower)

            # 3. Character distribution analysis
            else:
                # Words with more vowels tend to be more generic/job-type
                vowel_ratio = sum(1 for char in word_lower if char in 'aeiou') / len(word_lower)
                if vowel_ratio > 0.4:
                    job_type_terms.append(word_lower)
                else:
                    domain_terms.append(word_lower)

        return {
            'job_type_terms': job_type_terms,
            'domain_terms': domain_terms,
            'has_domain_specificity': len(domain_terms) > 0
        }

class UltraPureDynamicClassifier:
    """Completely pattern-free classifier using only mathematical and corpus-based methods"""
    def __init__(self):
        self.corpus_classifier = DynamicTermClassifier()
        self.tfidf_classifier = TFIDFTermClassifier()
        self.mathematical_classifier = PureMathematicalClassifier()

    def classify_query_comprehensively(self, query, jobs):
        """Use only mathematical and corpus-based methods - zero hardcoded patterns"""

        # Method 1: Corpus-based analysis
        corpus_result = self.corpus_classifier.classify_query_terms(query, jobs)

        # Method 2: TF-IDF based analysis
        tfidf_result = self.tfidf_classifier.classify_query(query, jobs)

        # Method 3: Pure mathematical analysis
        query_words = query.lower().split()
        math_result = self.mathematical_classifier.classify_by_mathematical_properties(query_words, jobs)

        # Combine results with weighted voting (no linguistic patterns involved)
        job_type_votes = {}
        domain_votes = {}

        # Count votes from each method
        for term in query_words:
            job_type_votes[term] = 0
            domain_votes[term] = 0

            # Corpus method vote
            if term in corpus_result['job_type_terms']:
                job_type_votes[term] += 1
            if term in corpus_result['domain_terms']:
                domain_votes[term] += 1

            # TF-IDF method vote
            if term in tfidf_result['job_type_terms']:
                job_type_votes[term] += 1
            if term in tfidf_result['domain_terms']:
                domain_votes[term] += 1

            # Mathematical method vote
            if term in math_result['job_type_terms']:
                job_type_votes[term] += 1
            if term in math_result['domain_terms']:
                domain_votes[term] += 1

        # Final classification based on majority vote
        final_job_type_terms = [term for term, votes in job_type_votes.items() if votes >= 2]
        final_domain_terms = [term for term, votes in domain_votes.items() if votes >= 2]

        return {
            'job_type_terms': final_job_type_terms,
            'domain_terms': final_domain_terms,
            'has_domain_specificity': len(final_domain_terms) > 0,
            'confidence_scores': {
                'job_type_confidence': sum(job_type_votes.values()) / (len(query_words) * 3) if query_words else 0,
                'domain_confidence': sum(domain_votes.values()) / (len(query_words) * 3) if query_words else 0
            },
            'method': 'ultra_pure_mathematical'  # Indicator that no patterns were used
        }

# Global model instance to avoid reloading
_global_jobbert_model = None
_model_load_attempted = False

def get_jobbert_model():
    """Get or load the JobBERT model (singleton pattern)"""
    global _global_jobbert_model, _model_load_attempted

    if _model_load_attempted:
        return _global_jobbert_model

    _model_load_attempted = True

    if not SENTENCE_TRANSFORMERS_AVAILABLE:
        logger.warning("❌ Sentence transformers not available")
        return None

    try:
        logger.info("🤖 Loading JobBERT-v2 model...")
        _global_jobbert_model = SentenceTransformer('TechWolf/JobBERT-v2')
        logger.info("✅ JobBERT-v2 loaded successfully")
        return _global_jobbert_model
    except Exception as e:
        logger.error(f"❌ Failed to load JobBERT-v2: {e}")
        try:
            logger.info("🔄 Fallback: Loading all-MiniLM-L6-v2...")
            _global_jobbert_model = SentenceTransformer('all-MiniLM-L6-v2')
            logger.info("✅ Fallback model loaded successfully")
            return _global_jobbert_model
        except Exception as e2:
            logger.error(f"❌ Failed to load fallback model: {e2}")
            _global_jobbert_model = None
            return None

# Use this instead of IntegratedDynamicClassifier if you want zero linguistic patterns
class AdaptiveJobBERTMatcher:
    def __init__(self):
        self.model = get_jobbert_model()  # Use singleton
        # CHOICE: Use UltraPureDynamicClassifier for zero patterns, or keep IntegratedDynamicClassifier for current approach
        self.dynamic_classifier = UltraPureDynamicClassifier()  # 100% pattern-free

    def calculate_adaptive_relevance(self, jobs, query):
        """Use JobBERT's natural understanding to filter jobs adaptively"""
        if not self.model:
            logger.info("🔄 JobBERT not available, using fallback filtering")
            return self._fallback_basic_filter(jobs, query)

        try:
            logger.debug(f"🤖 Processing {len(jobs)} jobs with JobBERT...")

            # Step 1: Encode everything with JobBERT
            job_texts = [f"{job['Title']} {job['Company']}" for job in jobs]

            # Batch processing with error handling
            try:
                job_embeddings = self.model.encode(job_texts, show_progress_bar=False, convert_to_tensor=False)
                query_embedding = self.model.encode([query], show_progress_bar=False, convert_to_tensor=False)
            except Exception as e:
                logger.warning(f"❌ JobBERT encoding failed: {e}")
                return self._fallback_basic_filter(jobs, query)

            # Step 2: Calculate semantic similarities
            try:
                similarities = cosine_similarity(query_embedding, job_embeddings)[0]
            except Exception as e:
                logger.warning(f"❌ Similarity calculation failed: {e}")
                return self._fallback_basic_filter(jobs, query)

            # Step 3: Adaptive threshold based on query specificity
            try:
                threshold = self._calculate_adaptive_threshold(query, similarities)
            except Exception as e:
                logger.warning(f"❌ Threshold calculation failed: {e}")
                threshold = 0.3  # Safe fallback threshold

            # Step 4: Multi-factor scoring
            relevant_jobs = []
            for i, job in enumerate(jobs):
                try:
                    semantic_score = float(similarities[i])

                    if semantic_score >= threshold:
                        # Calculate comprehensive relevance
                        final_score = self._calculate_comprehensive_relevance(
                            job, query, semantic_score
                        )

                        job['semantic_score'] = semantic_score
                        job['final_score'] = final_score
                        job['threshold_used'] = threshold
                        relevant_jobs.append(job)
                except Exception as e:
                    logger.warning(f"❌ Job scoring failed for '{job.get('Title', 'Unknown')}': {e}")
                    continue  # Skip this job and continue with others

            logger.info(f"✅ JobBERT processing complete: {len(relevant_jobs)} relevant jobs")
            return sorted(relevant_jobs, key=lambda x: x.get('final_score', 0), reverse=True)

        except Exception as e:
            logger.error(f"❌ JobBERT processing failed completely: {e}")
            logger.info("🔄 Falling back to basic keyword filtering")
            return self._fallback_basic_filter(jobs, query)

    def _calculate_adaptive_threshold(self, query, similarities):
        """Improved adaptive threshold calculation with domain-aware adjustment"""

        # Convert to numpy for better statistical operations
        if np is not None:
            similarities = np.array(similarities)
        else:
            # Fallback without numpy
            return self._fallback_threshold_calculation(query, similarities)

        # --- Step 1: Check for Domain Specificity ---
        # Quick domain detection without full classification (for performance)
        query_words = query.lower().split()

        # Estimate domain specificity by checking for technical/domain-specific terms
        potential_domain_indicators = 0
        for word in query_words:
            # Short technical terms, acronyms, or longer specialized words likely indicate domain specificity
            if (len(word) <= 4 and word.isupper()) or len(word) > 8 or any(char.isdigit() for char in word):
                potential_domain_indicators += 1

        has_likely_domain_terms = potential_domain_indicators > 0
        domain_ratio = potential_domain_indicators / len(query_words) if query_words else 0

        # --- Step 2: Query Complexity Analysis ---
        word_count = len(query_words)
        avg_word_length = sum(len(word) for word in query_words) / max(word_count, 1)

        # Normalized complexity score (0-1)
        complexity_score = min(
            (word_count / 6.0) * 0.6 +  # Word count factor (max 6 words)
            (avg_word_length / 12.0) * 0.4,  # Avg length factor (max 12 chars)
            1.0
        )

        # --- Step 3: Statistical Distribution Analysis ---
        sim_mean = float(similarities.mean())
        sim_std = float(similarities.std())
        sim_median = float(np.median(similarities))
        sim_75th = float(np.percentile(similarities, 75))
        sim_90th = float(np.percentile(similarities, 90))

        # --- Step 4: Adaptive Base Threshold Selection ---
        # Use percentile-based approach for more robust thresholding
        if sim_std > 0.15:
            # High variance = clear semantic distinction exists
            # Use 75th percentile as base, but cap it
            base_threshold = min(sim_75th * 0.8, 0.45)
        elif sim_std > 0.08:
            # Medium variance = some distinction
            # Use median + small offset
            base_threshold = min(sim_median + 0.1, 0.4)
        else:
            # Low variance = all jobs similar or all different
            # Use mean-based approach
            if sim_mean > 0.6:
                # High overall similarity - be more selective
                base_threshold = min(sim_mean * 0.9, 0.5)
            else:
                # Low overall similarity - be more inclusive
                base_threshold = max(sim_mean * 0.7, 0.25)

        # --- Step 5: Domain-Aware Threshold Adjustment ---
        if has_likely_domain_terms:
            # Domain-specific query detected - be much more selective
            domain_adjustment = 0.12 + (domain_ratio * 0.18)  # Increased from 0.08-0.20 to 0.12-0.30
            logger.debug(f"Domain-specific query detected: applying +{domain_adjustment:.3f} threshold boost")
        else:
            domain_adjustment = 0.0

        # --- Step 6: Complexity-Based Adjustment ---
        # More complex queries get slightly higher thresholds, but cap the impact
        complexity_adjustment = complexity_score * 0.10  # Reduced from 0.15

        # --- Step 7: Sample Size Consideration ---
        # Larger samples allow for more confident thresholding
        sample_size = len(similarities)
        if sample_size > 200:
            # Large sample - can be more selective
            size_adjustment = 0.03
        elif sample_size < 50:
            # Small sample - be more inclusive
            size_adjustment = -0.03
        else:
            size_adjustment = 0.0

        # --- Step 8: Final Threshold Calculation ---
        final_threshold = base_threshold + domain_adjustment + complexity_adjustment + size_adjustment

        # --- Step 9: Safety Bounds (Adjusted for Domain Specificity) ---
        if has_likely_domain_terms:
            # For domain-specific queries, allow higher thresholds
            min_threshold = 0.20  # Slightly higher minimum
            max_threshold = 0.65  # Higher maximum allowed
        else:
            # For generic queries, use original bounds
            min_threshold = 0.15  # Very inclusive minimum
            max_threshold = 0.55  # Reasonable maximum

        final_threshold = max(min_threshold, min(max_threshold, final_threshold))

        # --- Step 10: Logging for Debugging ---
        logger.debug(f"Threshold calculation: base={base_threshold:.3f}, "
                    f"domain_adj={domain_adjustment:.3f}, complexity_adj={complexity_adjustment:.3f}, "
                    f"size_adj={size_adjustment:.3f}, final={final_threshold:.3f}, "
                    f"domain_detected={has_likely_domain_terms}")

        return final_threshold

    def _fallback_threshold_calculation(self, query, similarities):
        """Fallback threshold calculation with domain awareness when numpy is not available"""
        # Simple fallback logic
        sim_values = list(similarities)
        sim_mean = sum(sim_values) / len(sim_values) if sim_values else 0.3

        query_words = query.lower().split()
        complexity_score = min(len(query_words) / 6.0, 1.0)

        # Check for domain specificity (simple version)
        potential_domain_indicators = 0
        for word in query_words:
            if (len(word) <= 4 and word.isupper()) or len(word) > 8 or any(char.isdigit() for char in word):
                potential_domain_indicators += 1

        has_likely_domain_terms = potential_domain_indicators > 0
        domain_ratio = potential_domain_indicators / len(query_words) if query_words else 0

        base_threshold = max(sim_mean * 0.8, 0.25)
        complexity_adjustment = complexity_score * 0.1

        # Domain adjustment
        if has_likely_domain_terms:
            domain_adjustment = 0.12 + (domain_ratio * 0.18)  # Match main threshold calculation
            max_threshold = 0.70  # Allow higher thresholds for domain queries
            min_threshold = 0.25  # Higher minimum
        else:
            domain_adjustment = 0.0
            max_threshold = 0.55
            min_threshold = 0.15

        final_threshold = base_threshold + complexity_adjustment + domain_adjustment

        return max(min_threshold, min(max_threshold, final_threshold))

    def _calculate_comprehensive_relevance(self, job, query, semantic_score):
        """Enhanced relevance calculation with adaptive weighting based on domain specificity"""

        try:
            # First, determine if this is a domain-specific query
            classification = self.dynamic_classifier.classify_query_comprehensively(query, [job])
            domain_terms = classification['domain_terms']
            has_domain_specificity = len(domain_terms) > 0
        except Exception as e:
            logger.warning(f"❌ Domain classification failed: {e}")
            has_domain_specificity = False
            domain_terms = []

        # --- ADAPTIVE WEIGHTING BASED ON QUERY TYPE ---
        if has_domain_specificity:
            # Domain-specific query (e.g., "Working Student AI")
            # Boost domain coherence importance, reduce semantic similarity weight
            semantic_weight = 0.25      # Reduced from 45% to 25%
            keyword_weight = 0.20       # Keep the same
            recency_weight = 0.10       # Reduced from 15% to 10%
            quality_weight = 0.05       # Keep the same
            domain_weight = 0.40        # MAJOR boost from 15% to 40%

            logger.debug(f"Domain-specific query detected for '{query}': using domain-focused weighting")
        else:
            # Generic query (e.g., "Working Student", "Engineer")
            # Use semantic-focused weighting
            semantic_weight = 0.45      # Original weight
            keyword_weight = 0.20       # Original weight
            recency_weight = 0.15       # Original weight
            quality_weight = 0.05       # Original weight
            domain_weight = 0.15        # Original weight

            logger.debug(f"Generic query detected for '{query}': using semantic-focused weighting")

        # --- Factor 2: Keyword coverage (Focus on title for domain, include company for general keywords) ---
        try:
            query_words = set(query.lower().split())

            # For domain-specific queries, focus keyword matching on job title
            if has_domain_specificity:
                job_words = set(job['Title'].lower().split())  # Title only for domain queries
                logger.debug(f"Domain-specific query: using title-only keyword matching for '{job['Title']}'")
            else:
                job_words = set(f"{job['Title']} {job['Company']}".lower().split())  # Include company for general queries
                logger.debug(f"General query: using title+company keyword matching")

            # Enhanced keyword matching
            exact_matches = len(query_words.intersection(job_words))
            keyword_coverage = exact_matches / len(query_words) if query_words else 0

            # Partial matching bonus
            partial_matches = 0
            for q_word in query_words:
                for j_word in job_words:
                    if len(q_word) > 3 and q_word in j_word:
                        partial_matches += 0.5

            enhanced_keyword_score = min((keyword_coverage + partial_matches * 0.3), 1.0)
        except Exception as e:
            logger.warning(f"❌ Keyword scoring failed: {e}")
            enhanced_keyword_score = 0.0

        # --- Factor 3: Recency ---
        try:
            recency_score = self._calculate_recency_score(job['Date Posted'])
        except Exception as e:
            logger.warning(f"❌ Recency scoring failed: {e}")
            recency_score = 0.3  # Default score

        # --- Factor 4: Job quality ---
        try:
            quality_score = self._calculate_enhanced_job_quality(job)
        except Exception as e:
            logger.warning(f"❌ Quality scoring failed: {e}")
            quality_score = 0.5  # Default score

        # --- Factor 5: Domain coherence ---
        try:
            domain_coherence_score = self._calculate_domain_coherence(job, query, [job])
        except Exception as e:
            logger.warning(f"❌ Domain coherence calculation failed: {e}")
            domain_coherence_score = 1.0 if not has_domain_specificity else 0.3  # Safe fallback

        # --- STRICTER DOMAIN FILTERING ---
        if has_domain_specificity and domain_coherence_score < 0.4:  # Increased from 0.3 to 0.4
            # For domain-specific queries, heavily penalize jobs with poor domain match
            logger.debug(f"Domain-specific job '{job['Title']}' failed domain coherence check: {domain_coherence_score:.3f} < 0.4")
            return 0.05  # Even lower score to ensure filtering out

        # Additional penalty for very low domain scores in domain-specific queries
        if has_domain_specificity and domain_coherence_score < 0.6:
            domain_penalty = (0.6 - domain_coherence_score) * 0.3  # Additional penalty
            logger.debug(f"Applying domain penalty of {domain_penalty:.3f} to '{job['Title']}'")
        else:
            domain_penalty = 0.0

        # --- Final Score Calculation with Adaptive Weights ---
        try:
            final_score = (
                semantic_score * semantic_weight +
                enhanced_keyword_score * keyword_weight +
                recency_score * recency_weight +
                quality_score * quality_weight +
                domain_coherence_score * domain_weight
            ) - domain_penalty  # Apply domain penalty

            # Ensure final score doesn't go below 0
            final_score = max(final_score, 0.0)
        except Exception as e:
            logger.warning(f"❌ Final score calculation failed: {e}")
            final_score = semantic_score * 0.5  # Fallback to simple semantic score

        # Log scoring breakdown for debugging
        if has_domain_specificity:
            logger.debug(f"Adaptive scoring for '{job['Title']}': "
                        f"semantic={semantic_score:.3f}*{semantic_weight} + "
                        f"keyword={enhanced_keyword_score:.3f}*{keyword_weight} + "
                        f"recency={recency_score:.3f}*{recency_weight} + "
                        f"quality={quality_score:.3f}*{quality_weight} + "
                        f"domain={domain_coherence_score:.3f}*{domain_weight} = {final_score:.3f}")

        return final_score

    def _calculate_recency_score(self, date_posted):
        """Calculate recency score"""
        try:
            posted_date = parse_date_posted_to_datetime(date_posted)
            now = datetime.now(pytz.UTC)
            hours_old = (now - posted_date).total_seconds() / 3600

            if hours_old <= 24:
                return 1.0  # Perfect score for last 24 hours
            elif hours_old <= 168:  # 1 week
                return 0.8
            elif hours_old <= 720:  # 1 month
                return 0.5
            else:
                return 0.1
        except:
            return 0.3  # Default for unparseable dates

    def _calculate_enhanced_job_quality(self, job):
        """Enhanced job quality calculation"""
        quality_score = 0.5  # Base score

        title = job['Title']
        company = job['Company']
        location = job['Location']

        # Title quality indicators
        if 10 <= len(title) <= 100:
            quality_score += 0.2

        # Company name quality
        if 3 <= len(company) <= 50:
            quality_score += 0.15

        # Location specificity (specific locations often indicate real jobs)
        location_lower = location.lower()
        if any(indicator in location_lower for indicator in ['remote', 'hybrid', 'city', 'street', 'avenue']):
            quality_score += 0.1

        # Title specificity indicators
        title_lower = title.lower()

        # Positive indicators
        if any(positive in title_lower for positive in ['senior', 'junior', 'lead', 'principal', 'manager']):
            quality_score += 0.05

        # Check for overly generic titles (negative indicator)
        generic_titles = ['assistant', 'associate', 'specialist', 'representative', 'coordinator']
        if any(generic in title_lower for generic in generic_titles):
            quality_score -= 0.1

        # Check for suspicious patterns
        if title.count('!') > 3 or title.count('$') > 0:
            quality_score -= 0.2

        # Word count quality - not too short, not too long
        word_count = len(title.split())
        if 2 <= word_count <= 8:
            quality_score += 0.1

        return min(max(quality_score, 0.1), 1.0)  # Clamp between 0.1 and 1.0

    def _calculate_domain_coherence(self, job, query, jobs):
        """Calculate domain coherence using ultra-pure dynamic term classification with enhanced strictness"""

        # Use ultra-pure dynamic classifier (zero hardcoded patterns)
        classification = self.dynamic_classifier.classify_query_comprehensively(query, jobs)

        # Extract domain terms dynamically
        domain_terms = classification['domain_terms']
        job_type_terms = classification['job_type_terms']

        # --- FALLBACK DOMAIN DETECTION ---
        # For reliability, also check for common domain terms that might not be caught by dynamic analysis
        query_lower = query.lower()
        fallback_domain_terms = []

        # Common domain terms that should be caught
        domain_indicators = {
            'ai': ['ai', 'artificial intelligence', 'machine learning', 'ml', 'deep learning'],
            'web': ['web', 'frontend', 'backend', 'javascript', 'react', 'vue', 'angular'],
            'data': ['data science', 'data analyst', 'analytics', 'sql', 'python', 'r'],
            'mobile': ['mobile', 'ios', 'android', 'swift', 'kotlin', 'flutter'],
            'cloud': ['cloud', 'aws', 'azure', 'gcp', 'devops', 'kubernetes'],
            'security': ['security', 'cybersecurity', 'infosec', 'penetration testing']
        }

        for domain, keywords in domain_indicators.items():
            if any(keyword in query_lower for keyword in keywords):
                fallback_domain_terms.extend([kw for kw in keywords if kw in query_lower])

        # Combine dynamic and fallback domain terms
        all_domain_terms = list(set(domain_terms + fallback_domain_terms))

        # Log classification results for debugging
        if all_domain_terms or job_type_terms:
            logger.debug(f"DOMAIN COHERENCE for '{query}': "
                        f"Dynamic={domain_terms}, Fallback={fallback_domain_terms}, Combined={all_domain_terms}, "
                        f"JobType={job_type_terms}, Method={classification.get('method', 'unknown')}")

        if not all_domain_terms:
            return 1.0  # No specific domain requirement

        # ✅ FIX: Focus domain matching on job title primarily
        job_title = job['Title'].lower()
        job_company = job['Company'].lower()

        # Calculate exact domain term overlap for TITLE (primary)
        title_exact_matches = 0
        for term in all_domain_terms:
            if term in job_title:
                title_exact_matches += 1
                logger.debug(f"   ✅ TITLE MATCH: '{term}' found in '{job['Title']}'")

        # Calculate exact domain term overlap for COMPANY (secondary, much lower weight)
        company_exact_matches = 0
        for term in all_domain_terms:
            if term in job_company:
                company_exact_matches += 1
                logger.debug(f"   🔸 COMPANY MATCH: '{term}' found in '{job['Company']}'")

        # Weighted scoring: Title gets 90% weight, Company gets 10% weight
        title_domain_score = title_exact_matches / len(all_domain_terms) if all_domain_terms else 0
        company_domain_score = company_exact_matches / len(all_domain_terms) if all_domain_terms else 0

        # Combined score heavily favoring job title
        combined_domain_score = (title_domain_score * 0.9) + (company_domain_score * 0.1)

        # Calculate partial domain term matching for TITLE only (more strict)
        partial_matches = 0
        for domain_term in all_domain_terms:
            for job_word in job_title.split():
                # Stricter partial match: domain term contained in job word (minimum 3 chars)
                if len(domain_term) >= 3 and len(job_word) >= 3:
                    if domain_term in job_word and len(domain_term) >= len(job_word) * 0.6:  # Significant overlap required
                        partial_matches += 0.3  # Reduced from 0.5
                        logger.debug(f"   🔸 PARTIAL TITLE MATCH: '{domain_term}' ~ '{job_word}'")
                    break

        partial_domain_score = min(partial_matches / len(all_domain_terms), 1.0) if all_domain_terms else 0

        # Final combined score: (title exact + partial) with company as minor factor
        final_combined_score = combined_domain_score + (partial_domain_score * 0.05)  # Very small partial contribution

        # Use JobBERT for semantic domain matching if available
        if self.model:
            try:
                domain_query = ' '.join(all_domain_terms)
                domain_embedding = self.model.encode([domain_query])

                # ✅ FIX: Use TITLE ONLY for semantic matching
                title_embedding = self.model.encode([job_title])

                semantic_score = cosine_similarity(domain_embedding, title_embedding)[0][0]

                # --- MUCH STRICTER COMBINATION LOGIC ---
                # For domain-specific queries, require BOTH lexical AND semantic evidence
                if final_combined_score >= 0.5:  # Strong lexical evidence in title
                    # Strong lexical evidence - trust it more
                    final_score = (final_combined_score * 0.8) + (float(semantic_score) * 0.2)
                elif final_combined_score >= 0.2:  # Some lexical evidence
                    # Moderate lexical evidence - require high semantic score
                    final_score = (final_combined_score * 0.6) + (float(semantic_score) * 0.4)
                    if semantic_score < 0.6:  # Semantic score too low
                        final_score *= 0.7  # Additional penalty
                else:
                    # No/minimal lexical evidence - semantic score must be very high
                    final_score = float(semantic_score) * 0.4  # Heavy penalty for lack of lexical evidence
                    if semantic_score < 0.8:  # Very high semantic threshold required
                        final_score *= 0.5  # Additional heavy penalty

                # Log domain matching results for debugging
                logger.debug(f"DOMAIN COHERENCE for '{job['Title']}': "
                           f"title_exact={title_domain_score:.3f}, company_exact={company_domain_score:.3f}, "
                           f"partial={partial_domain_score:.3f}, combined={final_combined_score:.3f}, "
                           f"semantic={semantic_score:.3f}, final={final_score:.3f}")

                # --- HARD MINIMUM THRESHOLD ---
                # For domain-specific queries, apply a hard minimum
                if len(all_domain_terms) > 0 and final_score < 0.2:
                    logger.debug(f"   ❌ HARD MINIMUM FAIL: {final_score:.3f} < 0.2 for domain query")
                    return 0.05  # Very low score to filter out

                return final_score

            except Exception as e:
                logger.warning(f"JobBERT semantic matching failed: {e}")
                # Fallback to lexical only with stricter threshold
                return final_combined_score if final_combined_score >= 0.4 else 0.1

        # Fallback without JobBERT - use title-focused lexical matching
        if final_combined_score >= 0.4:  # Increased threshold
            return final_combined_score
        else:
            logger.debug(f"   ❌ LEXICAL FAIL: {final_combined_score:.3f} < 0.4 threshold")
            return 0.1  # Low score to likely filter out

    def _fallback_basic_filter(self, jobs, query):
        """Fallback when JobBERT is not available - basic keyword matching"""
        query_words = set(query.lower().split())
        filtered_jobs = []

        for job in jobs:
            job_text = f"{job['Title']} {job['Company']}".lower()
            job_words = set(job_text.split())

            # Calculate keyword coverage
            coverage = len(query_words.intersection(job_words)) / len(query_words)

            if coverage >= 0.5:  # At least 50% keyword coverage
                job['semantic_score'] = coverage
                job['final_score'] = coverage
                job['threshold_used'] = 0.5
                filtered_jobs.append(job)

        return sorted(filtered_jobs, key=lambda x: x['final_score'], reverse=True)

def scrape_linkedin(keyword, location, filters_dict, max_pages=None):
    """Reusable and DYNAMIC scraping function.

    Set max_pages to a number to limit page scraping, or None to scrape all pages.
    """
    all_jobs_data = []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Connection': 'keep-alive',
    }

    filter_params = "".join([f"&{key}={quote_plus(value)}" for key, value in filters_dict.items() if value])

    page_number = 0
    while True:
        if max_pages and page_number >= max_pages:
            logger.info(f"Reached max_pages limit of {max_pages}. Stopping scrape.")
            break

        start_index = page_number * 25
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote_plus(keyword)}&location={quote_plus(location)}&start={start_index}{filter_params}"

        try:
            time.sleep(1.5) # Be respectful to LinkedIn's servers
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'lxml')
            job_cards = soup.find_all('div', class_='base-card')

            if not job_cards:
                logger.info(f"No more job cards found on page {page_number}. Stopping scrape.")
                break

            for job in job_cards:
                try:
                    raw_link = job.find('a', class_='base-card__full-link')['href']
                    clean_link = raw_link.split('?')[0]
                    all_jobs_data.append({
                        'Title': job.find('h3', class_='base-search-card__title').text.strip(),
                        'Company': job.find('h4', class_='base-search-card__subtitle').text.strip(),
                        'Location': job.find('span', class_='job-search-card__location').text.strip(),
                        'Date Posted': (job.find('time', class_='job-search-card__listdate') or job.find('time')).text.strip(),
                        'Link': clean_link
                    })
                except (AttributeError, TypeError):
                    continue

            page_number += 1

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                logger.info(f"LinkedIn pagination limit reached at start={start_index}. Stopping scrape.")
                break  # Break the loop and return what we have so far
            else:
                logger.error(f"HTTP error for url {url}: {e}")
                break
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for url {url}: {e}")
            break  # Break instead of returning empty, so we keep existing jobs

    return sorted(all_jobs_data, key=lambda job: parse_date_posted(job['Date Posted']), reverse=True)

def scrape_linkedin_with_adaptive_jobbert(keyword, location, filters_dict, max_pages=None, progress_msg=None, user_id=None):
    """Adaptive JobBERT filtering without hardcoded patterns - now thread-safe"""
    all_scraped_jobs = []
    seen_job_ids = set()
    seen_canonical_pairs = set()

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Connection': 'keep-alive',
    }

    filter_params = "".join([f"&{key}={quote_plus(value)}" for key, value in filters_dict.items() if value])

    page_number = 0

    # --- Step 1: Scrape all pages first ---
    while True:
        # Check if user operation was cancelled
        if user_id and not is_user_busy(user_id, 'search'):
            logger.info(f"Search cancelled for user {user_id}")
            break

        if max_pages and page_number >= max_pages:
            logger.info(f"Reached max_pages limit of {max_pages}. Stopping scrape.")
            break

        start_index = page_number * 25
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote_plus(keyword)}&location={quote_plus(location)}&start={start_index}{filter_params}"

        # Update progress message for scraping phase with activity indicator
        if progress_msg:
            # Create a pulsing activity indicator (no percentage since we don't know total pages)
            pulse_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            pulse_char = pulse_chars[page_number % len(pulse_chars)]

            progress_text = (
                f"🔍 **Searching LinkedIn** {pulse_char}\n\n"
                f"⏳ _Finding relevant opportunities..._"
            )
            safe_progress_update(progress_msg, progress_text, ParseMode.MARKDOWN)

        try:
            time.sleep(1.5)  # Be respectful to LinkedIn's servers
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'lxml')
            job_cards = soup.find_all('div', class_='base-card')

            if not job_cards:
                logger.info(f"No more job cards found on page {page_number + 1}. Stopping scrape.")
                break

            # Extract jobs from current page
            for job in job_cards:
                try:
                    raw_link = job.find('a', class_='base-card__full-link')['href']
                    clean_link = raw_link.split('?')[0]
                    job_data = {
                        'Title': job.find('h3', class_='base-search-card__title').text.strip(),
                        'Company': job.find('h4', class_='base-search-card__subtitle').text.strip(),
                        'Location': job.find('span', class_='job-search-card__location').text.strip(),
                        'Date Posted': (job.find('time', class_='job-search-card__listdate') or job.find('time')).text.strip(),
                        'Link': clean_link
                    }

                    # Check for duplicates before adding
                    job_id = canonical_link(job_data['Link'])
                    canonical_title = canonical_text(job_data['Title'])
                    canonical_company = canonical_text(job_data['Company'])
                    canonical_pair = (canonical_title, canonical_company)

                    if job_id not in seen_job_ids and canonical_pair not in seen_canonical_pairs:
                        seen_job_ids.add(job_id)
                        seen_canonical_pairs.add(canonical_pair)
                        all_scraped_jobs.append(job_data)

                except (AttributeError, TypeError):
                    continue

            logger.info(f"📄 Page {page_number + 1}: scraped {len([j for j in all_scraped_jobs if canonical_link(j['Link']) not in seen_job_ids])} new jobs (total unique: {len(all_scraped_jobs)})")
            page_number += 1

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                logger.info(f"LinkedIn pagination limit reached at start={start_index}. Stopping scrape.")
                break
            else:
                logger.error(f"HTTP error for url {url}: {e}")
                break
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for url {url}: {e}")
            break

    logger.info(f"🏁 Scraping complete: {len(all_scraped_jobs)} total jobs found.")

    # --- Step 2: AGGRESSIVE AI Filtering with Debug Info ---
    if not all_scraped_jobs:
        return []

    if progress_msg:
        # Create loading animation for AI filtering
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[0]  # Start with first character
        safe_progress_update(progress_msg, f"🤖 **AI Filtering** {loading_char}\n\n⚡ _Analyzing relevance..._", ParseMode.MARKDOWN)

    # Apply Ultra-Strict Filtering with comprehensive debugging
    adaptive_matcher = AdaptiveJobBERTMatcher()

    # --- ENHANCED DEBUGGING AND STRICT FILTERING ---
    logger.info(f"🔍 FILTER DEBUG: Starting analysis of query '{keyword}'")

    # Pre-filter with domain keywords for immediate strictness
    keyword_lower = keyword.lower()
    has_ai_terms = any(term in keyword_lower for term in ['ai', 'artificial intelligence', 'machine learning', 'ml', 'deep learning', 'neural', 'data science'])

    if has_ai_terms:
        logger.info(f"🎯 DOMAIN-SPECIFIC QUERY DETECTED: '{keyword}' contains AI terms")

        # Apply immediate pre-filtering for AI terms
        pre_filtered_jobs = []
        ai_keywords = ['ai', 'artificial intelligence', 'machine learning', 'ml', 'deep learning', 'neural', 'data science', 'computer vision', 'nlp', 'robotics', 'algorithm', 'tensorflow', 'pytorch', 'python']

        for job in all_scraped_jobs:
            # ✅ FIX: Focus ONLY on job title for domain matching
            job_title_text = job['Title'].lower()

            # Check for AI-related terms in job TITLE only
            ai_match_score = 0
            for ai_term in ai_keywords:
                if ai_term in job_title_text:
                    ai_match_score += 1

            # For AI queries, require at least some AI-related content in TITLE
            if ai_match_score > 0:
                pre_filtered_jobs.append(job)
                logger.debug(f"✅ PRE-FILTER PASS: '{job['Title']}' (AI score: {ai_match_score})")
            else:
                logger.debug(f"❌ PRE-FILTER FAIL: '{job['Title']}' (No AI keywords found in title)")

        logger.info(f"🎯 PRE-FILTER RESULTS: {len(all_scraped_jobs)} → {len(pre_filtered_jobs)} jobs after AI keyword filtering")
        jobs_to_analyze = pre_filtered_jobs
    else:
        logger.info(f"📝 GENERIC QUERY: '{keyword}' - applying standard filtering")
        jobs_to_analyze = all_scraped_jobs

    # Show enhanced progress
    if progress_msg:
        # Update loading animation
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[2]  # Use different character for progress
        progress_text = (
            f"🤖 **AI Filtering** {loading_char}\n\n"
            f"🧠 _Applying semantic analysis..._"
        )
        safe_progress_update(progress_msg, progress_text, ParseMode.MARKDOWN)

    # Perform the actual semantic analysis
    final_jobs = adaptive_matcher.calculate_adaptive_relevance(jobs_to_analyze, keyword)

    # Enhanced final logging
    logger.info(f"🎯 FINAL FILTERING RESULTS:")
    logger.info(f"   📊 Original scraped: {len(all_scraped_jobs)}")
    logger.info(f"   🎯 After pre-filter: {len(jobs_to_analyze)}")
    logger.info(f"   ✅ Final relevant: {len(final_jobs)}")
    logger.info(f"   📈 Filter efficiency: {((len(all_scraped_jobs) - len(final_jobs)) / len(all_scraped_jobs) * 100):.1f}% filtered out")

    # Log some examples of what passed/failed
    if final_jobs:
        logger.info(f"🎉 TOP MATCHES:")
        for i, job in enumerate(final_jobs[:3]):
            score = job.get('final_score', 0)
            logger.info(f"   {i+1}. '{job['Title']}' at {job['Company']} (score: {score:.3f})")

    # Show completion
    if progress_msg:
        # Final loading animation
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[5]  # Use different character for final stage
        completion_text = (
            f"✅ **AI Filtering Complete** {loading_char}\n\n"
            f"🚀 _Preparing your results..._"
        )
        safe_progress_update(progress_msg, completion_text, ParseMode.MARKDOWN)

    # Sort by date posted (most recent first), with relevance as secondary factor
    final_jobs = sorted(final_jobs, key=lambda x: (parse_date_posted_to_datetime(x['Date Posted']), x.get('final_score', 0)), reverse=True)

    return final_jobs

def run_scrape_threaded(update: Update, context: CallbackContext, progress_msg):
    """Thread-safe version of run_scrape that doesn't block other users."""
    user_id = update.effective_user.id
    chat_id = update.message.chat_id

    try:
        # Get transient search terms and persistent preferences
        search_keyword = context.user_data.get('search_keywords')
        search_location = context.user_data.get('search_location')
        prefs = get_user_prefs(context)

        filters = {
            'f_E': ",".join(prefs['experience'].values()),
            'f_JT': ",".join(prefs['job_types'].values()),
            'f_TPR': list(prefs['date_posted'].values())[0] if prefs['date_posted'] else None,
            'f_WT': list(prefs['workplace'].values())[0] if prefs['workplace'] else None
        }

        # Show scraping message (no progress bar)
        safe_progress_update(progress_msg, "🔍 **Starting Search** ⠋\n\n⏳ _Connecting to LinkedIn..._", ParseMode.MARKDOWN)

        # Scrape jobs with AI semantic matching
        sorted_jobs = scrape_linkedin_with_adaptive_jobbert(
            search_keyword, search_location, filters,
            progress_msg=progress_msg, user_id=user_id
        )

        if not sorted_jobs:
            safe_progress_update(progress_msg, "Search complete. No jobs found with these criteria.")
            time.sleep(2)
            # Go back to main menu after a delay
            text, kbd = make_main_menu(context)
            safe_progress_update(progress_msg, text)
            if progress_msg:
                try:
                    progress_msg.edit_reply_markup(reply_markup=kbd)
                except:
                    pass
            return

        # Show final results with nice formatting
        results_text = (
            f"🎉 **Search Complete!**\n\n"
            f"📋 _Loading your job listings..._"
        )
        safe_progress_update(progress_msg, results_text, ParseMode.MARKDOWN)
        time.sleep(1)

        context.user_data['jobs'] = sorted_jobs
        context.user_data['page'] = 0
        message_text, reply_markup = create_paginated_job_message(sorted_jobs, 0)

        if progress_msg:
            try:
                progress_msg.edit_text(
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Failed to update final results: {e}")

    except Exception as e:
        logger.error(f"Search failed for user {user_id}: {e}")
        safe_progress_update(progress_msg, f"❌ Search failed: {str(e)}")
    finally:
        # Always unregister the operation
        unregister_user_operation(user_id, 'search')

def run_scrape(update: Update, context: CallbackContext, progress_msg):
    """Legacy function for backwards compatibility - now just calls threaded version."""
    return run_scrape_threaded(update, context, progress_msg)

# --- Browsing and End Handlers ---
def page_navigation(update: Update, context: CallbackContext):
    query = update.callback_query
    safe_answer_callback_query(query)

    page = int(query.data.split('_')[1])
    context.user_data['page'] = page
    message_text, reply_markup = create_paginated_job_message(context.user_data['jobs'], page)

    safe_edit_message(query, message_text, reply_markup, ParseMode.HTML, True)
    return BROWSING

def ignore_callback(update: Update, context: CallbackContext):
    """An empty callback function to handle unclickable buttons."""
    safe_answer_callback_query(update.callback_query)

def close_browsing(update: Update, context: CallbackContext):
    query = update.callback_query
    safe_answer_callback_query(query)
    return main_menu(update, context)

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text('Operation canceled.')
    text, keyboard = make_main_menu(context)
    update.message.reply_text(text, reply_markup=keyboard)
    return MAIN_MENU

# --- Alert System Functions ---

def alerts_menu(update: Update, context: CallbackContext):
    """Display the main alerts menu."""
    query = update.callback_query
    query.answer()
    keyboard = [
        [InlineKeyboardButton("➕ Add New Alert", callback_data="add_alert")],
        [InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    text = "🔔 *Alerts Menu*\n\nManage your job alerts here."
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return ALERTS_MENU

def add_alert_start(update: Update, context: CallbackContext):
    """Start the process of adding a new alert."""
    query = update.callback_query
    query.answer()
    query.edit_message_text("First, what job keywords should I look for? (e.g. AI Engineer)")
    return ADD_ALERT_KEYWORD

def add_alert_keyword_received(update: Update, context: CallbackContext):
    """Receive the keywords for the new alert."""
    context.user_data['alert_keywords'] = update.message.text
    update.message.reply_text("Got it. Now, what location are you interested in?")
    return ADD_ALERT_LOCATION

def add_alert_location_received(update: Update, context: CallbackContext):
    """Receive the location and ask about preferences for this alert."""
    context.user_data['alert_location'] = update.message.text
    keywords = context.user_data.get('alert_keywords')
    location = update.message.text

    keyboard = [
        [InlineKeyboardButton("🚀 Skip Filters (Any)", callback_data="alert_skip_filters")],
        [InlineKeyboardButton("⚙️ Set Filters", callback_data="alert_set_filters")]
    ]

    text = f"Alert Setup:\n📝 Keywords: '{keywords}'\n📍 Location: '{location}'\n\nWould you like to set specific filters for this alert?"
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES

def alert_skip_filters(update: Update, context: CallbackContext):
    """Save alert without specific filters."""
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    chat_id = query.from_user.id
    keywords = context.user_data.get('alert_keywords')
    location = context.user_data.get('alert_location')

    # Check if user is already busy with alert setup
    if is_user_busy(user_id, 'alert_setup'):
        query.edit_message_text("⏳ Alert setup already in progress. Please wait...")
        return ALERT_PREFERENCES

    # Register alert setup operation
    register_user_operation(user_id, 'alert_setup')

    # Show progress message
    query.edit_message_text("📡 Setting up your alert and checking for existing jobs...")

    # Run alert setup in background thread
    future = run_concurrent_operation(setup_alert_threaded, query, context, keywords, location, {})
    context.user_data['alert_future'] = future

    return MAIN_MENU

def setup_alert_threaded(query, context, keywords, location, prefs):
    """Thread-safe alert setup that doesn't block other users."""
    user_id = query.from_user.id
    chat_id = query.from_user.id

    try:
        # Use provided preferences or empty ones
        if not prefs:
            prefs = {
                'experience': {},
                'job_types': {},
                'date_posted': {},
                'workplace': {}
            }
        filters_json = json.dumps(prefs)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (chat_id, keywords, location, filters) VALUES (?, ?, ?, ?)",
            (chat_id, keywords, location, filters_json)
        )
        alert_id = cursor.lastrowid
        conn.commit()

        # Populate baseline jobs to avoid spam
        filter_dict = {
            'f_E': ",".join(prefs['experience'].values()),
            'f_JT': ",".join(prefs['job_types'].values()),
            'f_TPR': list(prefs['date_posted'].values())[0] if prefs['date_posted'] else None,
            'f_WT': list(prefs['workplace'].values())[0] if prefs['workplace'] else None
        }

        baseline_jobs = scrape_linkedin_with_adaptive_jobbert(
            keywords, location, filter_dict,
            progress_msg=None, user_id=user_id
        )

        for job in baseline_jobs:
            job_id = canonical_link(job['Link'])
            canonical_title = canonical_text(job['Title'])
            canonical_company = canonical_text(job['Company'])
            cursor.execute("""
                INSERT OR IGNORE INTO sent_jobs
                (alert_id, chat_id, job_link, job_id, job_title, company, canonical_title, canonical_company)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (alert_id, chat_id, job['Link'], job_id, job['Title'], job['Company'], canonical_title, canonical_company))
        conn.commit()
        conn.close()

        logger.info(f"Populated {len(baseline_jobs)} baseline jobs for new alert ID {alert_id}")

        # Update the message with success
        try:
            query.edit_message_text(f"✅ Alert for '{keywords}' in '{location}' has been set and is now active. I've recorded {len(baseline_jobs)} existing jobs so you'll only get notified about truly new opportunities!")

            # Go back to the main menu after a delay
            time.sleep(3)
            text, keyboard = make_main_menu(context)
            query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Failed to update alert success message: {e}")

    except Exception as e:
        logger.error(f"Failed to setup alert for user {user_id}: {e}")
        try:
            query.edit_message_text(f"❌ Failed to setup alert: {str(e)}")
        except:
            pass
    finally:
        # Always unregister the operation
        unregister_user_operation(user_id, 'alert_setup')

def alert_set_filters(update: Update, context: CallbackContext):
    """Start setting filters specifically for this alert."""
    query = update.callback_query
    query.answer()

    # Initialize alert-specific preferences
    context.user_data['alert_preferences'] = {
        'experience': {},
        'job_types': {},
        'date_posted': {},
        'workplace': {}
    }

    text, keyboard = make_alert_preferences_menu(context)
    query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return ALERT_PREFERENCES

def get_alert_prefs(context: CallbackContext) -> dict:
    """Get alert-specific preferences."""
    if 'alert_preferences' not in context.user_data:
        context.user_data['alert_preferences'] = {
            'experience': {},
            'job_types': {},
            'date_posted': {},
            'workplace': {}
        }
    return context.user_data['alert_preferences']

def make_alert_preferences_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    """Create the alert preferences menu."""
    prefs = get_alert_prefs(context)
    keywords = context.user_data.get('alert_keywords', 'N/A')
    location = context.user_data.get('alert_location', 'N/A')

    experience = ", ".join(prefs['experience'].keys()) or "Any"
    job_types = ", ".join(prefs['job_types'].keys()) or "Any"
    date_posted = list(prefs['date_posted'].keys())[0] if prefs['date_posted'] else "Any"
    workplace = list(prefs['workplace'].keys())[0] if prefs['workplace'] else "Any"

    text = (
        f"⚙️ *Alert Filters*\n\n"
        f"📝 *Keywords:* {keywords}\n"
        f"📍 *Location:* {location}\n\n"
        f"*Current Filters:*\n"
        f"∙ *Date Posted:* `{date_posted}`\n"
        f"∙ *Workplace:* `{workplace}`\n"
        f"∙ *Experience:* `{experience}`\n"
        f"∙ *Job Types:* `{job_types}`"
    )
    keyboard = [
        [InlineKeyboardButton("🗓️ Date Posted", callback_data="alert_set_date_posted"), InlineKeyboardButton("🏢 Workplace", callback_data="alert_set_workplace")],
        [InlineKeyboardButton("🎓 Experience", callback_data="alert_set_experience"), InlineKeyboardButton("📝 Job Types", callback_data="alert_set_job_types")],
        [InlineKeyboardButton("✅ Save Alert", callback_data="alert_save_final")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def alert_save_final(update: Update, context: CallbackContext):
    """Save the alert with the configured preferences and populate baseline jobs."""
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    chat_id = query.from_user.id
    keywords = context.user_data.get('alert_keywords')
    location = context.user_data.get('alert_location')
    prefs = get_alert_prefs(context)

    # Check if user is already busy with alert setup
    if is_user_busy(user_id, 'alert_setup'):
        query.edit_message_text("⏳ Alert setup already in progress. Please wait...")
        return ALERT_PREFERENCES

    # Register alert setup operation
    register_user_operation(user_id, 'alert_setup')

    # Show progress message
    query.edit_message_text("📡 Setting up your alert and checking for existing jobs...")

    # Run alert setup in background thread
    future = run_concurrent_operation(setup_alert_threaded, query, context, keywords, location, prefs)
    context.user_data['alert_future'] = future

    # Clean up alert-specific data
    context.user_data.pop('alert_keywords', None)
    context.user_data.pop('alert_location', None)
    context.user_data.pop('alert_preferences', None)

    return MAIN_MENU

# Alert-specific preference handlers (similar to global ones but use alert_preferences)
def show_alert_date_posted_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = list(prefs['date_posted'].values())[0] if prefs['date_posted'] else None

    text = "🗓️ Choose Date Posted Filter for This Alert"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"alert_dp_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="alert_dp_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="alert_dp_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES

def alert_date_posted_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, option_id, option_text = update.callback_query.data.split('_', 3)

    if option_id == 'clear':
        prefs['date_posted'] = {}
    else:
        # Check if this option is already selected - if yes, deselect it
        if option_id in prefs['date_posted'].values():
            prefs['date_posted'] = {}
        else:
            prefs['date_posted'] = {option_text: option_id}

    # Re-render the menu to show the change
    return show_alert_date_posted_menu(update, context)

def alert_preferences_done(update: Update, context: CallbackContext):
    """Return to alert preferences menu from a sub-menu."""
    query = update.callback_query
    query.answer()
    text, keyboard = make_alert_preferences_menu(context)
    query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return ALERT_PREFERENCES

def show_alert_workplace_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = list(prefs['workplace'].values())[0] if prefs['workplace'] else None

    text = "🏢 Choose Workplace Type for This Alert"
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"alert_wt_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="alert_wt_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="alert_wt_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES

def alert_workplace_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, option_id, option_text = update.callback_query.data.split('_', 3)

    if option_id == 'clear':
        prefs['workplace'] = {}
    else:
        # Check if this option is already selected - if yes, deselect it
        if option_id in prefs['workplace'].values():
            prefs['workplace'] = {}
        else:
            prefs['workplace'] = {option_text: option_id}

    # Re-render the menu to show the change
    return show_alert_workplace_menu(update, context)

def show_alert_multi_select_menu(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)

    if menu_type == 'experience':
        title = "🎓 Choose Experience Levels for This Alert"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs['experience']
        callback_prefix = "alert_exp"
    else: # job_type
        title = "📝 Choose Job Types for This Alert"
        options_dict = JOB_TYPES
        selected_options = prefs['job_types']
        callback_prefix = "alert_jt"

    text = f"{title}\n\n" \
           "▫️ Click to select/deselect options\n" \
           "▫️ Click 'Done' when finished."

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"{callback_prefix}_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data=f"{callback_prefix}_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES

def alert_toggle_multi_select_option(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    if menu_type == 'experience':
        _, _, option_id, option_text = query.data.split('_', 3)
        selected_dict = prefs['experience']
    else: # job_type
        _, _, option_id, option_text = query.data.split('_', 3)
        selected_dict = prefs['job_types']

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next((k for k, v in selected_dict.items() if v == option_id), None)
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    return show_alert_multi_select_menu(update, context, menu_type)

def my_alerts(update: Update, context: CallbackContext):
    """Display a list of user's alerts with manage options in a cleaner two-level UI."""
    query = update.callback_query
    safe_answer_callback_query(query)

    chat_id = query.from_user.id

    conn = get_db_connection()
    cursor = conn.cursor()
    alerts = cursor.execute("SELECT * FROM alerts WHERE chat_id = ?", (chat_id,)).fetchall()
    conn.close()

    if not alerts:
        text = "📋 *Your Alerts*\n\nYou have no alerts set up yet."
        keyboard = [
            [InlineKeyboardButton("➕ Add New Alert", callback_data="add_alert")],
            [InlineKeyboardButton("🔙 Back to Alerts Menu", callback_data="alerts_menu")]
        ]
        if query:
            try:
                query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            except telegram.error.BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise e
        else:
            update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return MY_ALERTS

    text = f"📋 *Your Alerts* ({len(alerts)} active)\n\nClick on any alert to manage it:"
    keyboard = []

    for alert in alerts:
        status_icon = "🟢" if alert['is_active'] else "🔴"
        status_text = "Active" if alert['is_active'] else "Paused"

        # Clean, condensed alert display
        alert_line = f"{status_icon} {alert['keywords']} • {alert['location']}"
        keyboard.append([InlineKeyboardButton(alert_line, callback_data=f"view_alert_{alert['id']}")])

    # Add management buttons at bottom
    keyboard.append([InlineKeyboardButton("➕ Add New Alert", callback_data="add_alert")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Alerts Menu", callback_data="alerts_menu")])

    if query:
        try:
            query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except telegram.error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise e
    else:
        update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return MY_ALERTS

def view_alert_details(update: Update, context: CallbackContext):
    """Show detailed view of a specific alert with management options."""
    query = update.callback_query
    query.answer()

    _, _, alert_id = query.data.split('_')

    conn = get_db_connection()
    cursor = conn.cursor()
    alert = cursor.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()

    if not alert:
        query.edit_message_text("❌ Alert not found.")
        return MY_ALERTS

    # Get filter details
    filters = json.loads(alert['filters'])
    experience = ", ".join(filters['experience'].keys()) or "Any"
    job_types = ", ".join(filters['job_types'].keys()) or "Any"
    date_posted = list(filters['date_posted'].keys())[0] if filters['date_posted'] else "Any"
    workplace = list(filters['workplace'].keys())[0] if filters['workplace'] else "Any"

    # Count jobs sent for this alert
    sent_count = cursor.execute("SELECT COUNT(*) FROM sent_jobs WHERE alert_id = ?", (alert_id,)).fetchone()[0]

    # Fetch user's timezone
    tz_row = cursor.execute("SELECT timezone FROM user_settings WHERE chat_id = ?", (query.from_user.id,)).fetchone()
    conn.close() # Close connection after all DB queries are done

    user_timezone_str = tz_row['timezone'] if tz_row and tz_row['timezone'] else 'UTC'

    # --- FIX: Define status icons before using them ---
    status_icon = "🟢" if alert['is_active'] else "🔴"
    status_text = "Active" if alert['is_active'] else "Paused"

    last_checked_utc_str = alert['last_checked']
    last_checked_display = "Never"
    if last_checked_utc_str:
        try:
            # Timestamp from DB is UTC
            utc_dt = datetime.strptime(last_checked_utc_str.split('.')[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc)

            user_tz = pytz.timezone(user_timezone_str)
            local_dt = utc_dt.astimezone(user_tz)

            last_checked_display = local_dt.strftime('%Y-%m-%d %H:%M')
            if user_timezone_str != 'UTC':
                 last_checked_display += f" ({user_timezone_str.split('/')[-1].replace('_', ' ')})"
        except (ValueError, pytz.UnknownTimeZoneError):
            last_checked_display = last_checked_utc_str[:16] + " (UTC)"

    text = (
        f"🔔 *Alert Details*\n\n"
        f"📝 *Keywords:* {alert['keywords']}\n"
        f"📍 *Location:* {alert['location']}\n"
        f"📊 *Status:* {status_icon} {status_text}\n"
        f"📬 *Jobs Sent:* {sent_count}\n\n"
        f"*Current Filters:*\n"
        f"∙ *Date Posted:* `{date_posted}`\n"
        f"∙ *Workplace:* `{workplace}`\n"
        f"∙ *Experience:* `{experience}`\n"
        f"∙ *Job Types:* `{job_types}`\n\n"
        f"🕒 Last checked: {last_checked_display}"
    )

    # Action buttons based on current status
    action_text = "⏸️ Pause Alert" if alert['is_active'] else "▶️ Resume Alert"
    action_cb = f"pause_alert_{alert_id}" if alert['is_active'] else f"resume_alert_{alert_id}"

    keyboard = [
        [InlineKeyboardButton(action_text, callback_data=action_cb)],
        [InlineKeyboardButton("⚙️ Edit Preferences", callback_data=f"edit_alert_{alert_id}")],
        [InlineKeyboardButton("🗑️ Delete Alert", callback_data=f"delete_alert_start_{alert_id}")],
        [InlineKeyboardButton("⬅️ Back to Alerts", callback_data="my_alerts")]
    ]

    try:
        query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e

    return MY_ALERTS

def toggle_alert_status(update: Update, context: CallbackContext):
    """Pause or resume an alert."""
    query = update.callback_query
    action, _, alert_id = query.data.split('_')
    new_status = 0 if action == 'pause' else 1

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE alerts SET is_active = ? WHERE id = ?", (new_status, alert_id))
    conn.commit()
    conn.close()

    query.answer(f"Alert {'paused' if new_status == 0 else 'resumed'}.")
    # Refresh the list
    return my_alerts(update, context)

def delete_alert_start(update: Update, context: CallbackContext):
    """Ask for confirmation before deleting an alert."""
    query = update.callback_query
    _, _, _, alert_id = query.data.split('_')

    text = "Are you sure you want to permanently delete this alert?"
    keyboard = [
        [
            InlineKeyboardButton("Yes, Delete", callback_data=f"delete_alert_confirm_{alert_id}"),
            InlineKeyboardButton("No, Cancel", callback_data="my_alerts")
        ]
    ]
    query.answer()
    try:
        query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return MY_ALERTS

def delete_alert_confirm(update: Update, context: CallbackContext):
    """Delete the alert and all associated sent jobs from the database."""
    query = update.callback_query
    _, _, _, alert_id = query.data.split('_')

    conn = get_db_connection()
    cursor = conn.cursor()

    # Delete associated sent jobs first (although CASCADE should handle this)
    cursor.execute("DELETE FROM sent_jobs WHERE alert_id = ?", (alert_id,))

    # Delete the alert (this should also cascade delete sent_jobs due to FOREIGN KEY constraint)
    cursor.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))

    conn.commit()
    conn.close()

    query.answer("Alert and all associated job records deleted.")
    return my_alerts(update, context)

def edit_alert_start(update: Update, context: CallbackContext):
    """Start editing an existing alert's preferences."""
    query = update.callback_query
    query.answer()

    _, _, alert_id = query.data.split('_')

    # Load existing alert data
    conn = get_db_connection()
    cursor = conn.cursor()
    alert = cursor.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    conn.close()

    if not alert:
        query.edit_message_text("❌ Alert not found.")
        return MY_ALERTS

    # Store alert data for editing
    context.user_data['editing_alert_id'] = alert_id
    context.user_data['alert_keywords'] = alert['keywords']
    context.user_data['alert_location'] = alert['location']

    # Load existing preferences
    existing_prefs = json.loads(alert['filters'])
    context.user_data['alert_preferences'] = existing_prefs

    text, keyboard = make_edit_alert_preferences_menu(context)
    query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return EDIT_ALERT_PREFERENCES

def make_edit_alert_preferences_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    """Create the edit alert preferences menu."""
    prefs = get_alert_prefs(context)
    keywords = context.user_data.get('alert_keywords', 'N/A')
    location = context.user_data.get('alert_location', 'N/A')
    alert_id = context.user_data.get('editing_alert_id', 'N/A')

    experience = ", ".join(prefs['experience'].keys()) or "Any"
    job_types = ", ".join(prefs['job_types'].keys()) or "Any"
    date_posted = list(prefs['date_posted'].keys())[0] if prefs['date_posted'] else "Any"
    workplace = list(prefs['workplace'].keys())[0] if prefs['workplace'] else "Any"

    text = (
        f"⚙️ *Edit Alert Preferences*\n\n"
        f"📝 *Keywords:* {keywords}\n"
        f"📍 *Location:* {location}\n\n"
        f"*Current Filters:*\n"
        f"∙ *Date Posted:* `{date_posted}`\n"
        f"∙ *Workplace:* `{workplace}`\n"
        f"∙ *Experience:* `{experience}`\n"
        f"∙ *Job Types:* `{job_types}`"
    )
    keyboard = [
        [InlineKeyboardButton("🗓️ Date Posted", callback_data="edit_alert_set_date_posted"), InlineKeyboardButton("🏢 Workplace", callback_data="edit_alert_set_workplace")],
        [InlineKeyboardButton("🎓 Experience", callback_data="edit_alert_set_experience"), InlineKeyboardButton("📝 Job Types", callback_data="edit_alert_set_job_types")],
        [InlineKeyboardButton("✅ Save Changes", callback_data="edit_alert_save_final")],
        [InlineKeyboardButton("❌ Cancel", callback_data="my_alerts")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def edit_alert_save_final(update: Update, context: CallbackContext):
    """Save the updated alert preferences."""
    query = update.callback_query
    query.answer()

    alert_id = context.user_data.get('editing_alert_id')
    keywords = context.user_data.get('alert_keywords')
    location = context.user_data.get('alert_location')
    prefs = get_alert_prefs(context)
    filters_json = json.dumps(prefs)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE alerts SET filters = ? WHERE id = ?",
        (filters_json, alert_id)
    )
    conn.commit()
    conn.close()

    # Clean up editing data
    context.user_data.pop('editing_alert_id', None)
    context.user_data.pop('alert_keywords', None)
    context.user_data.pop('alert_location', None)
    context.user_data.pop('alert_preferences', None)

    query.edit_message_text(f"✅ Alert preferences for '{keywords}' in '{location}' have been updated successfully!")

    # Go back to My Alerts after a delay
    time.sleep(2)
    return my_alerts(update, context)

def edit_alert_preferences_done(update: Update, context: CallbackContext):
    """Return to edit alert preferences menu from a sub-menu."""
    query = update.callback_query
    query.answer()
    text, keyboard = make_edit_alert_preferences_menu(context)
    query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return EDIT_ALERT_PREFERENCES

# Alert preference handlers for editing (reuse existing logic but with different callbacks)
def show_edit_alert_date_posted_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = list(prefs['date_posted'].values())[0] if prefs['date_posted'] else None

    text = "🗓️ Choose Date Posted Filter for This Alert"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"edit_alert_dp_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="edit_alert_dp_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="edit_alert_dp_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES

def edit_alert_date_posted_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, _, option_id, option_text = update.callback_query.data.split('_', 4)

    if option_id == 'clear':
        prefs['date_posted'] = {}
    else:
        # Check if this option is already selected - if yes, deselect it
        if option_id in prefs['date_posted'].values():
            prefs['date_posted'] = {}
        else:
            prefs['date_posted'] = {option_text: option_id}

    # Re-render the menu to show the change
    return show_edit_alert_date_posted_menu(update, context)

# Similar handlers for workplace, experience, and job types for editing...
def show_edit_alert_workplace_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = list(prefs['workplace'].values())[0] if prefs['workplace'] else None

    text = "🏢 Choose Workplace Type for This Alert"
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"edit_alert_wt_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("Clear Filter", callback_data="edit_alert_wt_clear_None")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="edit_alert_wt_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES

def edit_alert_workplace_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, _, option_id, option_text = update.callback_query.data.split('_', 4)

    if option_id == 'clear':
        prefs['workplace'] = {}
    else:
        if option_id in prefs['workplace'].values():
            prefs['workplace'] = {}
        else:
            prefs['workplace'] = {option_text: option_id}

    return show_edit_alert_workplace_menu(update, context)

def show_edit_alert_multi_select_menu(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)

    if menu_type == 'experience':
        title = "🎓 Choose Experience Levels for This Alert"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs['experience']
        callback_prefix = "edit_alert_exp"
    else: # job_type
        title = "📝 Choose Job Types for This Alert"
        options_dict = JOB_TYPES
        selected_options = prefs['job_types']
        callback_prefix = "edit_alert_jt"

    text = f"{title}\n\n" \
           "▫️ Click to select/deselect options\n" \
           "▫️ Click 'Done' when finished."

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display_text, callback_data=f"{callback_prefix}_{option_id}_{option_text}")])

    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data=f"{callback_prefix}_done")])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES

def edit_alert_toggle_multi_select_option(update: Update, context: CallbackContext, menu_type: str):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    if menu_type == 'experience':
        _, _, option_id, option_text = query.data.split('_', 3)
        selected_dict = prefs['experience']
    else: # job_type
        _, _, option_id, option_text = query.data.split('_', 3)
        selected_dict = prefs['job_types']

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next((k for k, v in selected_dict.items() if v == option_id), None)
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    return show_edit_alert_multi_select_menu(update, context, menu_type)

def check_all_alerts(bot: Bot):
    """Scheduled job to check all active alerts with robust deduplication - now thread-safe."""
    logger.info("Scheduler running: Checking all active alerts...")

    # Use a separate thread pool for alert checking to avoid blocking the main bot
    with ThreadPoolExecutor(max_workers=5) as alert_executor:
        conn = get_db_connection()
        cursor = conn.cursor()

        active_alerts = cursor.execute("SELECT * FROM alerts WHERE is_active = 1").fetchall()
        conn.close()

        # Process alerts concurrently
        futures = []
        for alert in active_alerts:
            future = alert_executor.submit(check_single_alert, alert, bot)
            futures.append(future)

        # Wait for all alerts to complete
        for future in futures:
            try:
                future.result(timeout=300)  # 5 minute timeout per alert
            except Exception as e:
                logger.error(f"Alert check failed: {e}")

    logger.info("Scheduler finished checking alerts.")

def check_single_alert(alert, bot: Bot):
    """Check a single alert - thread-safe."""
    try:
        logger.info(f"Checking alert ID {alert['id']} for chat ID {alert['chat_id']}...")
        filters = json.loads(alert['filters'])

        filter_dict = {
            'f_E': ",".join(filters['experience'].values()),
            'f_JT': ",".join(filters['job_types'].values()),
            'f_TPR': list(filters['date_posted'].values())[0] if filters['date_posted'] else None,
            'f_WT': list(filters['workplace'].values())[0] if filters['workplace'] else None
        }

        # Scrape all pages dynamically and apply AI semantic matching
        # Use a unique user_id for scheduler operations
        scheduler_user_id = f"scheduler_{alert['id']}"
        found_jobs = scrape_linkedin_with_adaptive_jobbert(
            alert['keywords'], alert['location'], filter_dict,
            progress_msg=None, user_id=scheduler_user_id
        )

        # --- Robust De-duplication ---
        # Get all jobs already sent for this chat (across all alerts) for better deduplication
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT job_id, canonical_title, canonical_company FROM sent_jobs WHERE chat_id = ?", (alert['chat_id'],))
        sent_jobs = cursor.fetchall()
        sent_job_ids = {row['job_id'] for row in sent_jobs}
        sent_canonical_pairs = {(row['canonical_title'], row['canonical_company']) for row in sent_jobs}

        # Parse last_checked for date filtering
        last_checked = None
        if alert['last_checked']:
            try:
                last_checked = datetime.strptime(alert['last_checked'].split('.')[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.UTC)
            except ValueError:
                logger.warning(f"Could not parse last_checked timestamp for alert {alert['id']}")

        new_jobs_found = 0
        for job in found_jobs:
            # Extract job ID and canonical text
            job_id = canonical_link(job['Link'])
            canonical_title = canonical_text(job['Title'])
            canonical_company = canonical_text(job['Company'])

            # Check if this job is a duplicate using robust methods
            is_duplicate = (
                job_id in sent_job_ids or
                (canonical_title, canonical_company) in sent_canonical_pairs
            )

            # Optional: Skip jobs older than last check (with 5-minute grace period)
            if last_checked and not is_duplicate:
                try:
                    job_posted_time = parse_date_posted_to_datetime(job['Date Posted'])
                    if job_posted_time < last_checked - timedelta(minutes=5):
                        logger.debug(f"Skipping old job: {job['Title']} (posted {job_posted_time}, last checked {last_checked})")
                        continue
                except Exception as e:
                    logger.warning(f"Could not parse job date '{job['Date Posted']}': {e}")
                    # Continue processing if date parsing fails

            if not is_duplicate:
                new_jobs_found += 1

                # Escape HTML special characters to prevent parsing errors
                title = html.escape(job['Title'])
                company = html.escape(job['Company'])
                location = html.escape(job['Location'])
                date_posted = html.escape(job['Date Posted'])
                keywords = html.escape(alert['keywords'])
                alert_location = html.escape(alert['location'])

                message = (
                    f"🔔 <b>New Job Alert!</b>\n\n"
                    f"<b>{title}</b>\n"
                    f"<i>{company}</i> - {location}\n"
                    f"Posted: {date_posted}\n\n"
                    f"From your alert for: <b>{keywords}</b> in <b>{alert_location}</b>"
                )
                keyboard = [[InlineKeyboardButton("View Job", url=job['Link']), InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts")]]

                try:
                    bot.send_message(
                        chat_id=alert['chat_id'],
                        text=message,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.HTML
                    )
                    # Add to sent_jobs table with all the new fields
                    cursor.execute("""
                        INSERT OR IGNORE INTO sent_jobs
                        (alert_id, chat_id, job_link, job_id, job_title, company, canonical_title, canonical_company)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (alert['id'], alert['chat_id'], job['Link'], job_id, job['Title'], job['Company'], canonical_title, canonical_company))
                    conn.commit()

                    # Update our in-memory sets for this session
                    sent_job_ids.add(job_id)
                    sent_canonical_pairs.add((canonical_title, canonical_company))

                    time.sleep(1.2)  # Rate limit: max 20 messages per 30 seconds per chat
                except telegram.error.BadRequest as e:
                    logger.error(f"Failed to send alert to {alert['chat_id']}: {e}")
                except Exception as e:
                    logger.error(f"An unexpected error occurred sending to {alert['chat_id']}: {e}")

        if new_jobs_found > 0:
            logger.info(f"Sent {new_jobs_found} new job(s) for alert ID {alert['id']}.")

        # Update last_checked timestamp
        cursor.execute("UPDATE alerts SET last_checked = CURRENT_TIMESTAMP WHERE id = ?", (alert['id'],))
        conn.commit()
        conn.close()

        time.sleep(2)  # Reduced stagger time since we're running concurrently

    except Exception as e:
        logger.error(f"Failed to check alert {alert['id']}: {e}")

# --- New Timezone Functions ---
def set_timezone_start(update: Update, context: CallbackContext):
    """Start the timezone setting process."""
    query = update.callback_query
    query.answer()
    text = (
        "Please send me your timezone identifier.\n\n"
        "You can find your identifier on this list: [List of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)\n\n"
        "For example: `America/New_York`, `Europe/London`, `Asia/Kolkata`"
    )
    query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    return SET_TIMEZONE

def timezone_received(update: Update, context: CallbackContext):
    """Validate and save the user's timezone."""
    user_timezone = update.message.text.strip()
    try:
        # Validate timezone
        pytz.timezone(user_timezone)

        # Save to DB
        chat_id = update.message.chat_id
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO user_settings (chat_id, timezone) VALUES (?, ?)",
            (chat_id, user_timezone)
        )
        conn.commit()
        conn.close()

        update.message.reply_text(f"✅ Timezone set to `{user_timezone}`.", parse_mode=ParseMode.MARKDOWN)

        # Go back to preferences menu
        text, keyboard = make_preferences_menu(context, chat_id)
        update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
        return PREFERENCES_MENU

    except pytz.UnknownTimeZoneError:
        update.message.reply_text(
            "❌ Invalid timezone identifier. Please check the list and try again.\n"
            "Example: `Europe/Berlin`"
        )
        return SET_TIMEZONE

def main():
    import argparse
    import sys

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="JobQuestTG Bot")
    parser.add_argument("--migrate-only", action="store_true",
                        help="Run DB migrations then exit (for safe schema upgrades)")
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN not found.")
        return

    # Initialize DB (includes migration)
    init_db()

    # Exit early if only migration was requested
    if args.migrate_only:
        print("✅ Migration completed – exiting as requested.")
        print("🚀 You can now start the bot normally with: python bot.py")
        sys.exit(0)

    # Set up scheduler
    scheduler = BackgroundScheduler(timezone="UTC")

    # Pass the bot instance to the job
    bot_instance = Bot(token=token)
    scheduler.add_job(check_all_alerts, 'interval', minutes=30, args=[bot_instance])
    scheduler.start()

    updater = Updater(token, use_context=True)
    dispatcher = updater.dispatcher

    # Add error handler for timeout and other errors
    def error_handler(update, context):
        """Handle errors in the bot."""
        if isinstance(context.error, telegram.error.TimedOut):
            logger.warning("Telegram timeout error occurred")
        elif isinstance(context.error, telegram.error.BadRequest):
            logger.warning(f"Telegram BadRequest error: {context.error}")
        else:
            logger.error(f"Unexpected error: {context.error}")

    dispatcher.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(preferences_menu, pattern='^prefs$'),
                CallbackQueryHandler(start_search_flow, pattern='^start_search$'),
                CallbackQueryHandler(alerts_menu, pattern='^set_alert$'),
                CallbackQueryHandler(my_alerts, pattern='^my_alerts$'),
            ],
            PREFERENCES_MENU: [
                CallbackQueryHandler(show_date_posted_menu, pattern='^set_date_posted$'),
                CallbackQueryHandler(show_workplace_menu, pattern='^set_workplace$'),
                CallbackQueryHandler(lambda u, c: show_multi_select_menu(u, c, 'experience'), pattern='^set_experience$'),
                CallbackQueryHandler(lambda u, c: show_multi_select_menu(u, c, 'job_type'), pattern='^set_job_types$'),
                CallbackQueryHandler(set_timezone_start, pattern='^set_timezone$'),
                CallbackQueryHandler(main_menu, pattern='^main_menu$')
            ],
            ALERTS_MENU: [
                CallbackQueryHandler(add_alert_start, pattern='^add_alert$'),
                CallbackQueryHandler(my_alerts, pattern='^my_alerts$'),
                CallbackQueryHandler(main_menu, pattern='^main_menu$'),
            ],
            MY_ALERTS: [
                CallbackQueryHandler(add_alert_start, pattern='^add_alert$'),
                CallbackQueryHandler(alerts_menu, pattern='^alerts_menu$'),
                CallbackQueryHandler(view_alert_details, pattern='^view_alert_'),
                CallbackQueryHandler(toggle_alert_status, pattern='^(pause|resume)_alert_'),
                CallbackQueryHandler(edit_alert_start, pattern='^edit_alert_'),
                CallbackQueryHandler(delete_alert_start, pattern='^delete_alert_start_'),
                CallbackQueryHandler(delete_alert_confirm, pattern='^delete_alert_confirm_'),
                CallbackQueryHandler(my_alerts, pattern='^my_alerts$'), # To refresh after cancel
            ],
            ADD_ALERT_KEYWORD: [MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, add_alert_keyword_received)],
            ADD_ALERT_LOCATION: [MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, add_alert_location_received)],
            SET_TIMEZONE: [MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, timezone_received)],
            GET_SEARCH_KEYWORD: [MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, keyword_received)],
            GET_SEARCH_LOCATION: [MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, location_received)],
            DATE_POSTED_MENU: [
                CallbackQueryHandler(preferences_menu, pattern='^dp_done$'),
                CallbackQueryHandler(date_posted_selected, pattern='^dp_')
            ],
            WORKPLACE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern='^wt_done$'),
                CallbackQueryHandler(workplace_selected, pattern='^wt_')
            ],
            EXPERIENCE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern='^exp_done$'),
                CallbackQueryHandler(lambda u, c: toggle_multi_select_option(u, c, 'experience'), pattern='^exp_')
            ],
            JOB_TYPE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern='^jt_done$'),
                CallbackQueryHandler(lambda u, c: toggle_multi_select_option(u, c, 'job_type'), pattern='^jt_')
            ],
            BROWSING: [
                CallbackQueryHandler(page_navigation, pattern='^page_'),
                CallbackQueryHandler(ignore_callback, pattern='^ignore$'),
                CallbackQueryHandler(close_browsing, pattern='^close$'),
                CallbackQueryHandler(my_alerts, pattern='^my_alerts$'),
            ],
            ALERT_PREFERENCES: [
                CallbackQueryHandler(alert_skip_filters, pattern='^alert_skip_filters$'),
                CallbackQueryHandler(alert_set_filters, pattern='^alert_set_filters$'),
                CallbackQueryHandler(show_alert_date_posted_menu, pattern='^alert_set_date_posted$'),
                CallbackQueryHandler(show_alert_workplace_menu, pattern='^alert_set_workplace$'),
                CallbackQueryHandler(lambda u, c: show_alert_multi_select_menu(u, c, 'experience'), pattern='^alert_set_experience$'),
                CallbackQueryHandler(lambda u, c: show_alert_multi_select_menu(u, c, 'job_type'), pattern='^alert_set_job_types$'),
                CallbackQueryHandler(alert_preferences_done, pattern='^alert_dp_done$'),
                CallbackQueryHandler(alert_preferences_done, pattern='^alert_wt_done$'),
                CallbackQueryHandler(alert_preferences_done, pattern='^alert_exp_done$'),
                CallbackQueryHandler(alert_preferences_done, pattern='^alert_jt_done$'),
                CallbackQueryHandler(alert_date_posted_selected, pattern='^alert_dp_'),
                CallbackQueryHandler(alert_workplace_selected, pattern='^alert_wt_'),
                CallbackQueryHandler(lambda u, c: alert_toggle_multi_select_option(u, c, 'experience'), pattern='^alert_exp_'),
                CallbackQueryHandler(lambda u, c: alert_toggle_multi_select_option(u, c, 'job_type'), pattern='^alert_jt_'),
                CallbackQueryHandler(alert_save_final, pattern='^alert_save_final$'),
            ],
            EDIT_ALERT_PREFERENCES: [
                CallbackQueryHandler(show_edit_alert_date_posted_menu, pattern='^edit_alert_set_date_posted$'),
                CallbackQueryHandler(show_edit_alert_workplace_menu, pattern='^edit_alert_set_workplace$'),
                CallbackQueryHandler(lambda u, c: show_edit_alert_multi_select_menu(u, c, 'experience'), pattern='^edit_alert_set_experience$'),
                CallbackQueryHandler(lambda u, c: show_edit_alert_multi_select_menu(u, c, 'job_type'), pattern='^edit_alert_set_job_types$'),
                CallbackQueryHandler(edit_alert_preferences_done, pattern='^edit_alert_dp_done$'),
                CallbackQueryHandler(edit_alert_preferences_done, pattern='^edit_alert_wt_done$'),
                CallbackQueryHandler(edit_alert_preferences_done, pattern='^edit_alert_exp_done$'),
                CallbackQueryHandler(edit_alert_preferences_done, pattern='^edit_alert_jt_done$'),
                CallbackQueryHandler(edit_alert_date_posted_selected, pattern='^edit_alert_dp_'),
                CallbackQueryHandler(edit_alert_workplace_selected, pattern='^edit_alert_wt_'),
                CallbackQueryHandler(lambda u, c: edit_alert_toggle_multi_select_option(u, c, 'experience'), pattern='^edit_alert_exp_'),
                CallbackQueryHandler(lambda u, c: edit_alert_toggle_multi_select_option(u, c, 'job_type'), pattern='^edit_alert_jt_'),
                CallbackQueryHandler(edit_alert_save_final, pattern='^edit_alert_save_final$'),
                CallbackQueryHandler(my_alerts, pattern='^my_alerts$'),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
        allow_reentry=True
    )
    dispatcher.add_handler(conv_handler)
    logger.info("Bot started polling...")
    # Make sure to gracefully shutdown the scheduler and executor
    try:
        updater.start_polling()
        updater.idle()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down bot...")
        scheduler.shutdown()
        executor.shutdown(wait=True)
        logger.info("Bot shutdown complete.")

if __name__ == '__main__':
    main()
