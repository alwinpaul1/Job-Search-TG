import logging
import os
import warnings
import time
import json
import sqlite3
from dotenv import load_dotenv
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Bot
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
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
import google.generativeai as genai
import unicodedata

# --- Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

warnings.filterwarnings(
    action='ignore',
    message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message.",
    category=UserWarning,
    module='telegram.ext.conversationhandler'
)

# Load environment variables
load_dotenv()

# Configure Google AI
try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("✅ Successfully configured Google AI with Gemini 1.5 Flash.")
except Exception as e:
    logger.error(f"❌ Failed to configure Google AI: {e}")
    gemini_model = None

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
    """Get a database connection."""
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
        "Developed by Alwin with assistance from Gemini.\n"
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
    context.user_data['search_location'] = update.message.text
    progress_msg = update.message.reply_text("🚀 Kicking off the search...")
    return run_scrape(update, context, progress_msg)

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
        # Use HTML formatting instead of Markdown to avoid parsing issues
        message_text += f"<b>{job['Title']}</b>\n"
        message_text += f"<i>{job['Company']}</i> - {job['Location']}\n"
        message_text += f"Posted: {job['Date Posted']}\n"
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

def filter_jobs_with_llm(jobs, user_keywords, progress_msg=None):
    """Use Gemini 1.5 Flash to filter jobs based on relevance to user keywords."""
    if not jobs or not user_keywords:
        return jobs
    
    # Check if Gemini model is available
    if not gemini_model:
        logger.warning("❌ Google AI SDK not configured, skipping LLM filtering - results may include irrelevant jobs")
        return jobs
    
    try:
        # Increased batch size and optimized processing
        batch_size = 20  # Increased from 10 to reduce API calls
        filtered_jobs = []
        
        logger.info(f"Starting LLM filtering for {len(jobs)} jobs...")
        
        total_batches = (len(jobs) + batch_size - 1) // batch_size
        
        for i in range(0, len(jobs), batch_size):
            batch = jobs[i:i+batch_size]
            batch_num = i // batch_size + 1
            
            # Update progress bar for LLM processing
            if progress_msg:
                progress = "🤖" * batch_num
                progress_empty = "⬜️" * (total_batches - batch_num)
                progress_text = f"AI Processing...\nBatch {batch_num}/{total_batches}\n[{progress}{progress_empty}]"
                try:
                    progress_msg.edit_text(text=progress_text)
                except telegram.error.BadRequest as e:
                    if 'not modified' not in str(e).lower():
                        logger.warning(f"Progress bar update failed: {e}")
            
            try:
                # Prepare job data for LLM - include title, company, and location for better context
                job_summaries = []
                for idx, job in enumerate(batch):
                    # Include more context for better matching
                    job_summaries.append(f"{idx}: {job['Title']} at {job['Company']} ({job['Location']})")
                
                prompt = f"""You are a job title screener. Your job is to check if a job title matches a user's search query based on a strict set of rules.

**User's Search Query**: "{user_keywords}"

**Rules of Analysis:**

1.  **Identify Core Keywords**: Break down the user's query into essential parts. For "{user_keywords}", the core concepts must be identified (e.g., "Working Student" and "AI").

2.  **Strict Keyword Matching**: The job title **MUST** contain keywords related to **ALL** the core concepts from the user's query.
    - It is not enough for just one part to match. A partial match is a failure.

3.  **Domain Check**: The domain/function described in the job title must match the domain/function of the query. For example, "Communications" is not the "AI" domain.

4.  **IGNORE Company Name**: Do not consider the company name for relevance.

**Example of a definite failure to avoid:**
- User Query: "Working Student AI"
- Job Title: "Working Student: Events & protocol - Communications (d/f/m)"
- **Analysis**: This is a **BAD** match. While it is a "Working Student" position, its domain is "Communications", which is completely unrelated to "AI". You MUST exclude this.

**Your Goal**: Be extremely literal. If the job title doesn't explicitly match all core parts of the query, discard it.

---
**Job List to Analyze**:
{chr(10).join(job_summaries)}

---
**Output Instructions**:
Return ONLY the numbers of the relevant jobs, separated by a comma. If none are relevant, return "none".

Example: `1, 4`
Example: `none`

Numbers only:"""

                generation_config = genai.types.GenerationConfig(
                    candidate_count=1,
                    max_output_tokens=50,
                    temperature=0.1,
                )
                response = gemini_model.generate_content(
                    prompt,
                    generation_config=generation_config,
                    # Add a timeout if the SDK supports it, or handle it in the calling code
                )
                
                result = response.text.strip().lower()
                logger.info(f"🤖 LLM response for batch {i//batch_size + 1}: '{result}'")
                
                if result != "none":
                    try:
                        # Parse the returned job numbers
                        job_numbers = [int(x.strip()) for x in result.split(',') if x.strip().isdigit()]
                        logger.info(f"📊 Selected job indices: {job_numbers} out of {len(batch)} jobs")
                        for job_num in job_numbers:
                            if 0 <= job_num < len(batch):
                                # Only fetch description for selected jobs to save time
                                selected_job = batch[job_num]
                                filtered_jobs.append(selected_job)
                                logger.debug(f"✅ Included: {selected_job['Title']} at {selected_job['Company']}")
                    except ValueError:
                        # If parsing fails, include all jobs from this batch
                        logger.warning(f"❌ Failed to parse LLM response: '{result}' - including all jobs from batch")
                        filtered_jobs.extend(batch)
                else:
                    logger.info(f"🚫 LLM returned 'none' - no relevant jobs in this batch")
                
            except Exception as batch_error:
                # Check for rate limit error specifically
                if "429" in str(batch_error) and "quota" in str(batch_error).lower():
                    logger.error(f"RATE LIMIT on batch {i//batch_size + 1}. Skipping remaining batches for this search/alert to avoid irrelevant results.")
                    break
                else:
                    logger.warning(f"LLM batch {i//batch_size + 1} failed, including all jobs from batch. Error: {batch_error}")
                    filtered_jobs.extend(batch)
            
            # Reduced rate limiting since we're making fewer calls
            time.sleep(0.5)
        
        logger.info(f"LLM filtered {len(jobs)} jobs down to {len(filtered_jobs)} relevant jobs")
        
        # Re-sort filtered jobs by date posted (newest first) since LLM filtering disrupts original order
        return sorted(filtered_jobs, key=lambda job: parse_date_posted(job['Date Posted']), reverse=True)
        
    except Exception as e:
        logger.error(f"LLM filtering failed completely: {e}")
        # Return original jobs if LLM fails, but ensure they're sorted by date
        return sorted(jobs, key=lambda job: parse_date_posted(job['Date Posted']), reverse=True)

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

def scrape_linkedin_with_llm_filter(keyword, location, filters_dict, max_pages=None, progress_msg=None):
    """Scrapes all jobs from LinkedIn first, then applies a single LLM filter pass to reduce API calls."""
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
        if max_pages and page_number >= max_pages:
            logger.info(f"Reached max_pages limit of {max_pages}. Stopping scrape.")
            break

        start_index = page_number * 25
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote_plus(keyword)}&location={quote_plus(location)}&start={start_index}{filter_params}"
        
        # Update progress message for scraping phase
        if progress_msg:
            try:
                progress_msg.edit_text(text=f"🔍 Scraping page {page_number + 1}... (Found {len(all_scraped_jobs)} jobs so far)")
            except:
                pass
        
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
            
            logger.info(f"📄 Page {page_number + 1}: scraped {len(all_scraped_jobs) - len(seen_job_ids)} new jobs (total unique: {len(all_scraped_jobs)})")
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
    
    # --- Step 2: Apply LLM filtering to the entire list ---
    if not all_scraped_jobs:
        return []

    all_filtered_jobs = []
    if gemini_model:
        logger.info(f"🤖 Applying LLM filtering to all {len(all_scraped_jobs)} jobs...")
        all_filtered_jobs = filter_jobs_with_llm(all_scraped_jobs, keyword, progress_msg)
    else:
        logger.warning("❌ No API key - returning all jobs without LLM filtering.")
        all_filtered_jobs = all_scraped_jobs
    
    logger.info(f"🎯 Filtering complete: {len(all_scraped_jobs)} → {len(all_filtered_jobs)} relevant jobs")
    
    # Sort by date posted (newest first)
    return sorted(all_filtered_jobs, key=lambda job: parse_date_posted(job['Date Posted']), reverse=True)

def run_scrape(update: Update, context: CallbackContext, progress_msg):
    chat_id = update.message.chat_id
    
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
    try:
        progress_msg.edit_text(text="🔍 Scraping LinkedIn jobs...")
    except telegram.error.BadRequest as e:
        if 'not modified' not in str(e).lower():
            logger.warning(f"Progress message update failed: {e}")

    # Scrape jobs with LLM filtering (progress bar will show during LLM processing)
    sorted_jobs = scrape_linkedin_with_llm_filter(search_keyword, search_location, filters, progress_msg=progress_msg)

    if not sorted_jobs:
        progress_msg.edit_text(text="Search complete. No jobs found with these criteria.")
        time.sleep(2)
        # Go back to main menu after a delay
        text, kbd = make_main_menu(context)
        progress_msg.edit_text(text, reply_markup=kbd)
        return MAIN_MENU
        
    progress_msg.edit_text(text=f"✅ Found {len(sorted_jobs)} relevant jobs!")
    time.sleep(1)
    context.user_data['jobs'] = sorted_jobs
    context.user_data['page'] = 0
    message_text, reply_markup = create_paginated_job_message(sorted_jobs, 0)
    progress_msg.edit_text(text=message_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return BROWSING

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
    
    chat_id = query.from_user.id
    keywords = context.user_data.get('alert_keywords')
    location = context.user_data.get('alert_location')
    
    # Show progress message
    query.edit_message_text("📡 Setting up your alert and checking for existing jobs...")
    
    # Use empty preferences (no filters)
    empty_prefs = {
        'experience': {},
        'job_types': {},
        'date_posted': {},
        'workplace': {}
    }
    filters_json = json.dumps(empty_prefs)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO alerts (chat_id, keywords, location, filters) VALUES (?, ?, ?, ?)",
        (chat_id, keywords, location, filters_json)
    )
    alert_id = cursor.lastrowid
    conn.commit()
    
    # Populate baseline jobs to avoid spam (no filters)
    filter_dict = {'f_E': '', 'f_JT': '', 'f_TPR': None, 'f_WT': None}
    
    try:
        baseline_jobs = scrape_linkedin_with_llm_filter(keywords, location, filter_dict, progress_msg=None)
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
        logger.info(f"Populated {len(baseline_jobs)} baseline jobs for new alert ID {alert_id}")
    except Exception as e:
        logger.error(f"Failed to populate baseline jobs: {e}")
    
    conn.close()
    
    query.edit_message_text(f"✅ Alert for '{keywords}' in '{location}' has been set with no filters and is now active. I've recorded {len(baseline_jobs) if 'baseline_jobs' in locals() else 0} existing jobs so you'll only get notified about truly new opportunities!")
    
    # Go back to the main menu after a delay
    time.sleep(3)
    text, keyboard = make_main_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
    return MAIN_MENU

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
    
    chat_id = query.from_user.id
    keywords = context.user_data.get('alert_keywords')
    location = context.user_data.get('alert_location')
    prefs = get_alert_prefs(context)
    filters_json = json.dumps(prefs)

    # Show progress message
    query.edit_message_text("📡 Setting up your alert and checking for existing jobs...")

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
    
    try:
        baseline_jobs = scrape_linkedin_with_llm_filter(keywords, location, filter_dict, progress_msg=None)
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
            time.sleep(1.2)  # Rate limit: max 20 messages per 30 seconds per chat
        logger.info(f"Populated {len(baseline_jobs)} baseline jobs for new alert ID {alert_id}")
    except Exception as e:
        logger.error(f"Failed to populate baseline jobs: {e}")
    
    conn.close()
    
    # Clean up alert-specific data
    context.user_data.pop('alert_keywords', None)
    context.user_data.pop('alert_location', None) 
    context.user_data.pop('alert_preferences', None)
    
    query.edit_message_text(f"✅ Alert for '{keywords}' in '{location}' has been set with your custom filters and is now active. I've recorded {len(baseline_jobs) if 'baseline_jobs' in locals() else 0} existing jobs so you'll only get notified about truly new opportunities!")
    
    # Go back to the main menu after a delay
    time.sleep(3)
    text, keyboard = make_main_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
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
        _, _, _, option_id, option_text = query.data.split('_', 4)
        selected_dict = prefs['experience']
    else: # job_type
        _, _, _, option_id, option_text = query.data.split('_', 4)
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
    """Scheduled job to check all active alerts with robust deduplication."""
    logger.info("Scheduler running: Checking all active alerts...")
    conn = get_db_connection()
    cursor = conn.cursor()
    
    active_alerts = cursor.execute("SELECT * FROM alerts WHERE is_active = 1").fetchall()
    
    for alert in active_alerts:
        logger.info(f"Checking alert ID {alert['id']} for chat ID {alert['chat_id']}...")
        filters = json.loads(alert['filters'])
        
        filter_dict = {
            'f_E': ",".join(filters['experience'].values()),
            'f_JT': ",".join(filters['job_types'].values()),
            'f_TPR': list(filters['date_posted'].values())[0] if filters['date_posted'] else None,
            'f_WT': list(filters['workplace'].values())[0] if filters['workplace'] else None
        }

        # Scrape all pages dynamically and apply LLM filtering
        found_jobs = scrape_linkedin_with_llm_filter(alert['keywords'], alert['location'], filter_dict, progress_msg=None)
        
        # --- Robust De-duplication ---
        # Get all jobs already sent for this chat (across all alerts) for better deduplication
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
                message = (
                    f"🔔 <b>New Job Alert!</b>\n\n"
                    f"<b>{job['Title']}</b>\n"
                    f"<i>{job['Company']}</i> - {job['Location']}\n"
                    f"Posted: {job['Date Posted']}\n\n"
                    f"From your alert for: <b>{alert['keywords']}</b> in <b>{alert['location']}</b>"
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
        time.sleep(5) # Stagger requests between alerts

    conn.close()
    logger.info("Scheduler finished checking alerts.")

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
            ADD_ALERT_KEYWORD: [MessageHandler(Filters.text & ~Filters.command, add_alert_keyword_received)],
            ADD_ALERT_LOCATION: [MessageHandler(Filters.text & ~Filters.command, add_alert_location_received)],
            SET_TIMEZONE: [MessageHandler(Filters.text & ~Filters.command, timezone_received)],
            GET_SEARCH_KEYWORD: [MessageHandler(Filters.text & ~Filters.command, keyword_received)],
            GET_SEARCH_LOCATION: [MessageHandler(Filters.text & ~Filters.command, location_received)],
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
    # Make sure to gracefully shutdown the scheduler
    try:
        updater.start_polling()
        updater.idle()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == '__main__':
    main() 