# JobQuestTG

A powerful Telegram bot for intelligent job searching and automated job alerts using LinkedIn scraping and AI-powered relevance scoring.

## Try It Now!

The bot is live on Telegram! Start using it immediately without any setup:

**[@JobQuestTG_Bot](https://t.me/JobQuestTG_Bot)**

Just click the link or search for `@JobQuestTG_Bot` in Telegram and send `/start` to begin your job search journey!

## Features

### Smart Job Search
- **LinkedIn Integration**: Scrape job listings directly from LinkedIn
- **AI-Powered Relevance**: Advanced semantic matching using JobBERT and adaptive algorithms
- **Smart Filtering**: Date posted, workplace type, experience level, and more
- **Real-time Results**: Get job listings with detailed descriptions and direct links

### Automated Job Alerts
- **Custom Alerts**: Set up personalized job alerts with specific criteria
- **Background Monitoring**: Automatic checking every 60 minutes
- **Instant Notifications**: Get notified when new matching jobs are found
- **Alert Management**: Create, edit, enable/disable, and delete alerts

### Intelligent Matching
- **Semantic Search**: Uses sentence transformers for understanding job context
- **Adaptive Thresholds**: Dynamic relevance scoring based on search results
- **Multi-classifier System**: Combines TF-IDF, corpus analysis, and mathematical properties
- **Quality Scoring**: Evaluates job quality based on multiple factors

### Advanced Features
- **Timezone Support**: Configure your local timezone for accurate scheduling
- **Concurrent Operations**: Handle multiple users simultaneously
- **Progress Tracking**: Real-time progress updates during searches
- **Pagination**: Navigate through job results easily
- **Error Handling**: Robust error handling and recovery

## Quick Start

### Prerequisites
- Python 3.8 or higher
- [uv](https://github.com/astral-sh/uv) - Fast Python package manager (recommended) or pip
- Telegram Bot Token (get from [@BotFather](https://t.me/botfather))
- LinkedIn access (for job scraping)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/alwinpaul1/Job-Search-TG.git
   cd Job-Search-TG
   ```

2. **Install uv (if not already installed)**
   ```bash
   # macOS/Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   
   # Windows
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
   
   # Alternative: pip install uv
   pip install uv
   ```

3. **Install dependencies**
   ```bash
   # Using uv (recommended - faster)
   uv pip install -r requirements.txt
   
   # Alternative: using pip
   pip install -r requirements.txt
   ```

4. **Set up PostgreSQL database**
   ```bash
   # Install PostgreSQL (if not already installed)
   # macOS: brew install postgresql
   # Ubuntu: sudo apt install postgresql
   
   # Create database
   createdb job_alerts
   ```

5. **Set up environment variables**
   Create a `.env` file in the project root:
   ```env
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
   
   # PostgreSQL Database Configuration
   # Option 1: Use DATABASE_URL (recommended for cloud deployments)
   # DATABASE_URL=postgres://user:password@host:5432/job_alerts
   
   # Option 2: Use individual environment variables
   POSTGRES_HOST=localhost
   POSTGRES_PORT=5432
   POSTGRES_DB=job_alerts
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=your_password
   
   # Optional: Override database connection pool size (auto-calculated by default)
   # DB_POOL_SIZE=20
   ```

6. **Migrate existing data (if upgrading from SQLite)**
   ```bash
   python migrate_to_postgres.py
   ```

7. **Run the bot**
   ```bash
   python bot.py
   ```

## Dependencies

### Core Dependencies
- `python-telegram-bot==13.15` - Telegram Bot API wrapper
- `beautifulsoup4==4.13.5` - Web scraping
- `requests==2.32.4` - HTTP requests
- `python-dotenv==1.1.1` - Environment variable management
- `cachetools==4.2.2` - LRU cache implementation
- `tornado==6.1` - Web framework and asynchronous networking
- `psycopg2-binary==2.9.10` - PostgreSQL database adapter

### AI & Machine Learning
- `scikit-learn==1.7.1` - Machine learning algorithms
- `sentence-transformers==5.1.0` - Semantic text embeddings
- `numpy==2.3.2` - Numerical computing

### Scheduling & Utilities
- `APScheduler==3.6.3` - Background job scheduling
- `pytz==2025.2` - Timezone handling
- `psutil==7.0.0` - System monitoring

### Additional Dependencies
- `lxml==6.0.1` - XML/HTML processing
- `urllib3==2.5.0` - HTTP client library
- `openai==1.102.0` - OpenAI API client
- `certifi==2025.4.26` - Certificate validation
- `charset-normalizer==3.4.2` - Character encoding detection
- `idna==3.10` - Internationalized domain names
- `six==1.17.0` - Python 2/3 compatibility utilities
- `soupsieve==2.8` - CSS selector library
- `tzlocal==5.3.1` - System timezone detection

## Usage

### Basic Commands
- `/start` - Start the bot and show main menu
- `/about` - Show information about the bot
- `/cancel` - Cancel current operation

### Job Search
1. Click "Search Jobs" from main menu
2. Enter job keywords (e.g., "Python Developer")
3. Enter location (e.g., "San Francisco")
4. Configure filters (date posted, workplace type, etc.)
5. View results with pagination

### Job Alerts
1. Click "Job Alerts" from main menu
2. Choose "Add Alert"
3. Set keywords, location, and preferences
4. Enable the alert to start monitoring
5. Manage alerts with edit/delete options

### Timezone Setup
- Use "Settings" to configure your timezone
- Ensures accurate alert scheduling

## Architecture

### Core Components
- **Job Scraper**: LinkedIn job listing extraction
- **Relevance Engine**: AI-powered job matching
- **Alert System**: Automated job monitoring
- **Database**: PostgreSQL with connection pooling for high performance
- **Scheduler**: Background task management

### AI Matching System
- **JobBERT Matcher**: Semantic similarity using transformer models
- **Adaptive Thresholds**: Dynamic relevance scoring
- **Multi-classifier**: Combines multiple classification approaches
- **Quality Assessment**: Job quality evaluation

## Configuration

### Environment Variables
```env
# Required
TELEGRAM_BOT_TOKEN=your_bot_token

# PostgreSQL Database (one of these options required)
# Option 1: DATABASE_URL (recommended for cloud/Heroku deployments)
DATABASE_URL=postgres://user:password@host:5432/job_alerts

# Option 2: Individual variables
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=job_alerts
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# Optional
DB_POOL_SIZE=20  # Database connection pool size (auto-calculated if not set)
                 # Formula: 10 + (active_alerts * 0.5)
                 # Default: 10-30 connections based on active alerts
                 # Fallback: 10 connections if calculation fails
```

### Database
The bot uses PostgreSQL for storing:
- User preferences
- Job alerts
- Search history
- Timezone settings

PostgreSQL provides:
- Better concurrency and performance
- Connection pooling for efficient resource usage
- ACID compliance for data integrity
- Better handling of concurrent alert checks

### Scheduling
- Alert checks: Every 30 minutes
- Background tasks: Managed by APScheduler
- Concurrent operations: Up to 10 workers

## Auto-Restart & Crash Recovery

The bot includes robust crash detection and auto-restart mechanisms to ensure continuous operation.

### Built-in Crash Monitor
The bot has an internal crash monitoring system that:
- Tracks consecutive failures and triggers automatic restart
- Monitors memory usage and performs emergency recovery
- Detects hung processes via scheduler watchdog
- Automatically restarts on critical failures

### Running with Auto-Restart Wrapper

For production deployments, use the included wrapper script:

```bash
# Make executable (first time only)
chmod +x run_bot.sh

# Start with auto-restart
./run_bot.sh start

# Check status
./run_bot.sh status

# Stop
./run_bot.sh stop

# Restart
./run_bot.sh restart
```

The wrapper provides:
- Automatic restart on crash
- Memory monitoring (restarts if >2.2GB)
- Hung process detection
- Exponential backoff for rapid failures
- Detailed logging to `bot_wrapper.log`

### macOS LaunchAgent (Recommended for macOS)

For macOS, use launchd for system-level management:

```bash
# Copy the plist file
cp com.jobquest.bot.plist ~/Library/LaunchAgents/

# Load and start
launchctl load ~/Library/LaunchAgents/com.jobquest.bot.plist

# Control commands
launchctl start com.jobquest.bot    # Start
launchctl stop com.jobquest.bot     # Stop
launchctl list | grep jobquest      # Check status

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.jobquest.bot.plist
```

### Linux systemd Service

For Linux servers, use systemd:

```bash
# Copy service file
sudo cp jobquest-bot.service /etc/systemd/system/

# Edit and update paths
sudo nano /etc/systemd/system/jobquest-bot.service

# Reload, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable jobquest-bot
sudo systemctl start jobquest-bot

# Control commands
sudo systemctl status jobquest-bot   # Check status
sudo systemctl restart jobquest-bot  # Restart
sudo systemctl stop jobquest-bot     # Stop
sudo journalctl -u jobquest-bot -f   # View logs
```

### Supervisord (Alternative)

For cross-platform process management:

```bash
# Install supervisor
pip install supervisor

# Start with included config
supervisord -c supervisord.conf

# Control commands
supervisorctl -c supervisord.conf status
supervisorctl -c supervisord.conf restart jobquest-bot
supervisorctl -c supervisord.conf tail -f jobquest-bot
```

### Configuration Options

The auto-restart behavior can be configured in `bot.py`:

```python
AUTO_RESTART_ON_CRITICAL = True  # Enable/disable auto-restart
MAX_CONSECUTIVE_FAILURES = 3    # Failures before restart
MAX_MEMORY_MB = 2500            # Memory limit in MB
```

### Log Files

- `diagnostic.log` - Detailed bot operation logs
- `alert_monitor.log` - Alert check timing and status
- `bot_wrapper.log` - Wrapper script logs
- `restart_history.log` - Record of all restarts

