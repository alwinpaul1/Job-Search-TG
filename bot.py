import hashlib
import html
import json
import logging
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.pool
import psycopg2.extras
from psycopg2 import sql as psql

import telegram
from dotenv import load_dotenv

# Load environment variables early
load_dotenv()

# PostgreSQL Configuration from environment variables
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # Parse DATABASE_URL (e.g., postgres://user:pass@host:port/dbname)
    import urllib.parse
    result = urllib.parse.urlparse(DATABASE_URL)
    DB_CONFIG = {
        "host": result.hostname,
        "port": result.port or 5432,
        "database": result.path[1:],  # Remove leading slash
        "user": result.username,
        "password": result.password,
    }
else:
    # Individual environment variables
    DB_CONFIG = {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "database": os.getenv("POSTGRES_DB", "job_alerts"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
    }
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update

try:
    from telegram.constants import ParseMode
except ImportError:
    # For older versions of python-telegram-bot
    from telegram import ParseMode

# Handle different filter names across versions
try:
    # Try newer version first
    from telegram.ext import filters
    if hasattr(filters, "Text"):
        TEXT_FILTER = filters.Text
        COMMAND_FILTER = filters.COMMAND
    elif hasattr(filters, "TEXT"):
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

import re
import signal
import sys
import subprocess
import unicodedata
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote_plus

# Auto-restart configuration
AUTO_RESTART_ON_CRITICAL = True  # Enable auto-restart on critical failures
MAX_CONSECUTIVE_FAILURES = 3  # Max failures before longer backoff
RESTART_BACKOFF_SECONDS = 30  # Base backoff time
_consecutive_failures = 0
_last_failure_time = None

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    Updater,
)

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Diagnostic logger - logs to both file and console
diagnostic_file_handler = logging.FileHandler("diagnostic.log")
diagnostic_file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logger.addHandler(diagnostic_file_handler)

# Alert monitoring logger
alert_logger = logging.getLogger("alert_monitor")
alert_handler = logging.FileHandler("alert_monitor.log")
alert_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
alert_logger.addHandler(alert_handler)
alert_logger.setLevel(logging.INFO)

# Global thread pool for concurrent operations with proper cleanup
executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="JobQuest")

# Enhanced lock system for thread safety with deadlock detection
class DeadlockDetectableLock:
    """Thread lock with deadlock detection and auto-recovery"""
    def __init__(self, name="UnnamedLock", timeout=300):
        self._lock = threading.RLock()
        self._name = name
        self._timeout = timeout
        self._holder = None
        self._holder_time = None
        self._lock_count = 0
        self._meta_lock = threading.Lock()
        
    def acquire(self, blocking=True, timeout=-1):
        """Acquire lock with deadlock detection"""
        if timeout == -1:
            timeout = self._timeout
            
        acquired = self._lock.acquire(blocking=blocking, timeout=timeout)
        
        if acquired:
            with self._meta_lock:
                self._holder = threading.current_thread().name
                self._holder_time = time.time()
                self._lock_count += 1
                logger.debug(f"🔒 Lock '{self._name}' acquired by {self._holder} (count: {self._lock_count})")
        else:
            with self._meta_lock:
                if self._holder and self._holder_time:
                    held_duration = time.time() - self._holder_time
                    logger.warning(
                        f"⚠️ Failed to acquire lock '{self._name}'. "
                        f"Held by: {self._holder} for {held_duration:.1f}s"
                    )
        
        return acquired
    
    def release(self):
        """Release lock and update tracking"""
        try:
            with self._meta_lock:
                self._lock_count -= 1
                if self._lock_count <= 0:
                    self._holder = None
                    self._holder_time = None
                    self._lock_count = 0
                logger.debug(f"🔓 Lock '{self._name}' released (remaining count: {self._lock_count})")
            self._lock.release()
        except Exception as e:
            logger.error(f"❌ Error releasing lock '{self._name}': {e}")
    
    def force_release(self):
        """Force release in case of deadlock - use with extreme caution"""
        logger.warning(f"🆘 FORCE RELEASING lock '{self._name}' - potential deadlock recovery")
        with self._meta_lock:
            old_holder = self._holder
            old_count = self._lock_count
            self._holder = None
            self._holder_time = None
            self._lock_count = 0
            logger.critical(f"🔓 Force released lock previously held by: {old_holder} (count was: {old_count})")

        # Release the RLock the correct number of times instead of replacing it
        # This ensures waiting threads can acquire the lock
        try:
            for _ in range(max(old_count, 1)):
                try:
                    self._lock.release()
                except RuntimeError:
                    # Lock wasn't held, that's fine
                    break
        except Exception as e:
            logger.error(f"❌ Error during force release: {e}")
            # Last resort: create new lock but log warning about orphaned threads
            logger.critical(f"⚠️ Creating new lock - any waiting threads will be orphaned!")
            self._lock = threading.RLock()
    
    def get_status(self):
        """Get current lock status for monitoring"""
        with self._meta_lock:
            if self._holder and self._holder_time:
                return {
                    'locked': True,
                    'holder': self._holder,
                    'held_duration': time.time() - self._holder_time,
                    'lock_count': self._lock_count
                }
            return {'locked': False}

# Enhanced lock system
db_lock = threading.RLock()
search_ai_lock = threading.Lock()
alert_ai_lock = threading.Lock()
model_lock = DeadlockDetectableLock("ModelLock", timeout=180)  # 3 min timeout
memory_cleanup_lock = threading.Lock()
scheduler_lock = threading.Lock()

# Global state locks
global_state_lock = threading.RLock()
heartbeat_lock = threading.Lock()

# User-specific operation tracking
user_operations = {}
user_operations_lock = threading.Lock()

# Global shutdown flag for graceful termination
shutdown_requested = threading.Event()
heartbeat_active = threading.Event()
heartbeat_active.set()  # Start with heartbeat active

# Scheduler watchdog tracking
last_scheduler_run = {'alert_check': None, 'memory_cleanup': None}
scheduler_watchdog_lock = threading.Lock()

# =============================================================================
# CRASH DETECTION AND AUTO-RESTART SYSTEM
# =============================================================================

class CrashMonitor:
    """
    Monitors bot health and can trigger auto-restart on critical failures.
    This provides an internal safety net in addition to external process managers.
    """
    
    def __init__(self):
        self.consecutive_failures = 0
        self.last_failure_time = None
        self.last_successful_operation = time.time()
        self.memory_warnings = 0
        self.critical_errors = []
        self._lock = threading.Lock()
        self.restart_requested = False
        
    def record_success(self):
        """Record a successful operation"""
        with self._lock:
            self.consecutive_failures = 0
            self.last_successful_operation = time.time()
            
    def record_failure(self, error_msg: str, is_critical: bool = False):
        """Record a failure and check if restart is needed"""
        with self._lock:
            self.consecutive_failures += 1
            self.last_failure_time = time.time()
            
            if is_critical:
                self.critical_errors.append({
                    'time': datetime.now().isoformat(),
                    'error': str(error_msg)[:500]  # Limit error message size
                })
                # Keep only last 10 critical errors
                self.critical_errors = self.critical_errors[-10:]
            
            # Check if restart is needed
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(f"🚨 CRASH MONITOR: {self.consecutive_failures} consecutive failures detected!")
                return True
            
            return False
    
    def record_memory_warning(self, memory_mb: float):
        """Record a memory warning"""
        with self._lock:
            self.memory_warnings += 1
            if self.memory_warnings >= 5:
                logger.critical(f"🚨 CRASH MONITOR: {self.memory_warnings} memory warnings! Current: {memory_mb:.1f}MB")
                return True
            return False
    
    def reset_memory_warnings(self):
        """Reset memory warning counter after successful cleanup"""
        with self._lock:
            self.memory_warnings = 0
    
    def get_health_status(self) -> dict:
        """Get current health status"""
        with self._lock:
            time_since_success = time.time() - self.last_successful_operation
            return {
                'consecutive_failures': self.consecutive_failures,
                'memory_warnings': self.memory_warnings,
                'seconds_since_success': time_since_success,
                'critical_errors_count': len(self.critical_errors),
                'status': 'CRITICAL' if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES else
                         'WARNING' if self.consecutive_failures > 0 else 'HEALTHY'
            }
    
    def should_restart(self) -> bool:
        """Check if bot should restart"""
        with self._lock:
            # Restart if too many consecutive failures
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                return True
            
            # Restart if no successful operations in 30 minutes
            time_since_success = time.time() - self.last_successful_operation
            if time_since_success > 1800:  # 30 minutes
                logger.critical(f"🚨 No successful operations in {time_since_success/60:.1f} minutes!")
                return True
            
            return False

# Global crash monitor instance
crash_monitor = CrashMonitor()


def trigger_self_restart():
    """
    Trigger a self-restart of the bot process.
    This is a last resort when the bot is in an unrecoverable state.
    """
    global crash_monitor
    
    if not AUTO_RESTART_ON_CRITICAL:
        logger.warning("⚠️ Auto-restart is disabled. Manual intervention required.")
        return False
    
    logger.critical("🔄 TRIGGERING SELF-RESTART...")
    logger.critical(f"📊 Crash monitor status: {crash_monitor.get_health_status()}")
    
    try:
        # Log the restart
        with open("restart_history.log", "a") as f:
            f.write(f"{datetime.now().isoformat()} - Self-restart triggered\n")
            f.write(f"  Status: {crash_monitor.get_health_status()}\n")
        
        # Set the restart flag
        crash_monitor.restart_requested = True
        
        # Signal shutdown
        shutdown_requested.set()
        
        # Give current operations a moment to complete
        time.sleep(2)
        
        # Get the current script path
        script_path = os.path.abspath(sys.argv[0])
        python_path = sys.executable
        
        logger.critical(f"🚀 Restarting: {python_path} {script_path}")
        
        # Use exec to replace current process (cleaner restart)
        os.execv(python_path, [python_path, script_path] + sys.argv[1:])
        
    except Exception as e:
        logger.critical(f"❌ Self-restart failed: {e}")
        logger.critical("💡 Please restart the bot manually or use the wrapper script")
        return False
    
    return True


def emergency_memory_recovery():
    """
    Emergency memory recovery procedure.
    Called when memory is critically high and normal cleanup isn't working.
    """
    global _global_jobbert_model, _global_adaptive_matcher
    import gc
    
    logger.critical("🆘 EMERGENCY MEMORY RECOVERY INITIATED")
    
    try:
        # Step 1: Force unload all models
        logger.info("Step 1: Force unloading models...")
        _global_jobbert_model = None
        _global_adaptive_matcher = None
        
        # Step 2: Clear all caches
        logger.info("Step 2: Clearing caches...")
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        
        # Step 3: Force garbage collection multiple times
        logger.info("Step 3: Aggressive garbage collection...")
        for i in range(5):
            gc.collect()
            time.sleep(0.5)
        
        # Step 4: Check memory after recovery
        try:
            import psutil
            process = psutil.Process(os.getpid())
            memory_after = process.memory_info().rss / 1024 / 1024
            logger.info(f"✅ Memory after emergency recovery: {memory_after:.1f}MB")
            
            if memory_after < MAX_MEMORY_MB * 0.7:
                crash_monitor.reset_memory_warnings()
                logger.info("✅ Emergency recovery successful!")
                return True
            else:
                logger.warning(f"⚠️ Memory still high after recovery: {memory_after:.1f}MB")
                return False
                
        except Exception as e:
            logger.error(f"Could not verify memory after recovery: {e}")
            return False
            
    except Exception as e:
        logger.critical(f"❌ Emergency memory recovery failed: {e}")
        return False


def safe_operation(operation_name: str):
    """
    Decorator for wrapping operations with crash monitoring.
    Records successes and failures, triggering restart if needed.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                crash_monitor.record_success()
                return result
            except MemoryError as e:
                logger.critical(f"💥 MEMORY ERROR in {operation_name}: {e}")
                should_restart = crash_monitor.record_failure(str(e), is_critical=True)
                
                # Try emergency recovery first
                if emergency_memory_recovery():
                    logger.info("✅ Recovered from memory error")
                elif should_restart and AUTO_RESTART_ON_CRITICAL:
                    trigger_self_restart()
                raise
                
            except Exception as e:
                logger.error(f"❌ Error in {operation_name}: {e}")
                is_critical = isinstance(e, (SystemError, RuntimeError, OSError))
                should_restart = crash_monitor.record_failure(str(e), is_critical=is_critical)
                
                if should_restart and AUTO_RESTART_ON_CRITICAL:
                    logger.critical(f"🔄 Too many failures, triggering restart...")
                    trigger_self_restart()
                raise
        return wrapper
    return decorator

# Fully dynamic database connection pool that auto-adjusts during runtime
class DatabasePool:
    # Configuration constants
    MIN_POOL_SIZE = 10
    MAX_POOL_SIZE = 50
    BASE_SIZE = 10
    CONCURRENCY_FACTOR = 0.7
    RESIZE_INTERVAL = 60  # Seconds between resize checks
    
    def __init__(self, initial_max=10):
        self.max_connections = initial_max
        self.pool = []
        self.pool_lock = threading.Lock()
        self.active_connections = 0
        self.checked_out = 0  # Track connections currently in use
        self.last_resize_check = time.time()
        self.resize_lock = threading.Lock()
        logger.info(f"📊 Database pool initialized with max_connections={initial_max}")
        
    def _calculate_optimal_size(self):
        """Calculate optimal pool size based on current active alerts."""
        # Check for environment variable override
        env_pool_size = os.getenv('DB_POOL_SIZE')
        if env_pool_size:
            try:
                pool_size = int(env_pool_size)
                if pool_size > 0:
                    return pool_size
            except ValueError:
                pass
        
        try:
            # Query database for active alerts count using a fresh connection
            temp_conn = psycopg2.connect(
                host=DB_CONFIG["host"],
                port=DB_CONFIG["port"],
                database=DB_CONFIG["database"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                connect_timeout=5
            )
            cursor = temp_conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM alerts WHERE is_active = 1")
            active_alerts = cursor.fetchone()[0]
            temp_conn.close()
            
            # Calculate pool size: base + (alerts * concurrency_factor)
            calculated_size = int(self.BASE_SIZE + (active_alerts * self.CONCURRENCY_FACTOR))
            
            # Apply bounds
            return max(self.MIN_POOL_SIZE, min(calculated_size, self.MAX_POOL_SIZE))
            
        except Exception as e:
            logger.debug(f"Could not calculate pool size: {e}")
            return self.max_connections  # Keep current size on error
    
    def _maybe_resize(self):
        """Check if pool needs resizing (called periodically)."""
        current_time = time.time()
        
        # Only check every RESIZE_INTERVAL seconds
        if current_time - self.last_resize_check < self.RESIZE_INTERVAL:
            return
            
        # Use non-blocking lock to avoid contention
        if not self.resize_lock.acquire(blocking=False):
            return
            
        try:
            self.last_resize_check = current_time
            new_size = self._calculate_optimal_size()
            
            if new_size != self.max_connections:
                old_size = self.max_connections
                self.max_connections = new_size
                logger.info(f"📊 Database pool resized: {old_size} → {new_size} connections")
                
                # If shrinking, close excess idle connections
                if new_size < old_size:
                    with self.pool_lock:
                        while len(self.pool) > new_size:
                            try:
                                conn = self.pool.pop()
                                conn.close()
                                if self.active_connections > 0:
                                    self.active_connections -= 1
                            except Exception:
                                pass
        finally:
            self.resize_lock.release()
        
    def _is_connection_alive(self, conn):
        """Check if a connection is still valid."""
        try:
            if conn.closed:
                return False
            # Quick query to test the connection
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False
    
    def get_connection(self):
        """Get a connection from the pool or create a new one."""
        # Periodically check if resize is needed
        self._maybe_resize()
        
        with self.pool_lock:
            # Try to get from pool first, validating connections
            while self.pool:
                conn = self.pool.pop()
                if self._is_connection_alive(conn):
                    self.checked_out += 1
                    return conn
                else:
                    # Connection is dead, close it and try next
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if self.active_connections > 0:
                        self.active_connections -= 1
                    logger.debug("🔄 Discarded stale connection from pool")
            
            # Create new connection if under limit
            if self.active_connections < self.max_connections:
                self.active_connections += 1
                self.checked_out += 1
                conn = self._create_connection()
                return conn
            
            # Pool exhausted - expand dynamically instead of warning
            # This allows the pool to grow beyond max temporarily under load
            self.active_connections += 1
            self.checked_out += 1
            logger.debug(f"📊 Pool expanded: {self.active_connections} active (max: {self.max_connections})")
            return self._create_connection()
    
    def return_connection(self, conn):
        """Return a connection to the pool."""
        if conn:
            try:
                # Rollback any uncommitted transaction before returning to pool
                try:
                    conn.rollback()
                except Exception:
                    pass
                    
                with self.pool_lock:
                    self.checked_out = max(0, self.checked_out - 1)
                    
                    # Keep connection if pool isn't full
                    if len(self.pool) < self.max_connections:
                        self.pool.append(conn)
                    else:
                        # Close excess connection
                        conn.close()
                        if self.active_connections > 0:
                            self.active_connections -= 1
            except Exception as e:
                logger.warning(f"Error returning connection to pool: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                with self.pool_lock:
                    self.checked_out = max(0, self.checked_out - 1)
                    if self.active_connections > 0:
                        self.active_connections -= 1
    
    def _create_connection(self):
        """Create a new database connection with keepalive settings."""
        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            database=DB_CONFIG["database"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            connect_timeout=10,
            options="-c statement_timeout=30000",  # 30 second query timeout
            # TCP keepalive settings to detect dead connections faster
            keepalives=1,
            keepalives_idle=30,      # Start keepalive probes after 30s idle
            keepalives_interval=10,  # Send keepalive every 10s
            keepalives_count=3       # Close after 3 failed probes
        )
        # Use RealDictCursor for dict-like row access
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn
    
    def get_stats(self):
        """Get current pool statistics."""
        with self.pool_lock:
            return {
                "max_connections": self.max_connections,
                "active_connections": self.active_connections,
                "pooled_connections": len(self.pool),
                "checked_out": self.checked_out,
                "min_connections": getattr(self, 'min_connections', 5)
            }
    
    def force_resize(self):
        """Force an immediate resize check."""
        self.last_resize_check = 0  # Reset timer to force check
        self._maybe_resize()
    
    def close_all(self):
        """Close all connections in the pool."""
        with self.pool_lock:
            while self.pool:
                try:
                    conn = self.pool.pop()
                    conn.close()
                except Exception as e:
                    logger.warning(f"Error closing pooled connection: {e}")
            self.active_connections = 0
            self.checked_out = 0


def calculate_optimal_pool_size():
    """Calculate initial optimal pool size at startup."""
    env_pool_size = os.getenv('DB_POOL_SIZE')
    if env_pool_size:
        try:
            pool_size = int(env_pool_size)
            if pool_size > 0:
                logger.info(f"📊 Using DB_POOL_SIZE from environment: {pool_size}")
                return pool_size
        except ValueError:
            pass
    
    try:
        temp_conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            database=DB_CONFIG["database"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            connect_timeout=5
        )
        cursor = temp_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM alerts WHERE is_active = 1")
        active_alerts = cursor.fetchone()[0]
        temp_conn.close()
        
        calculated_size = int(DatabasePool.BASE_SIZE + (active_alerts * DatabasePool.CONCURRENCY_FACTOR))
        pool_size = max(DatabasePool.MIN_POOL_SIZE, min(calculated_size, DatabasePool.MAX_POOL_SIZE))
        
        logger.info(f"📊 Initial pool size: {active_alerts} active alerts → {pool_size} connections")
        return pool_size
        
    except Exception as e:
        logger.warning(f"⚠️ Could not calculate initial pool size: {e}, using default: {DatabasePool.MIN_POOL_SIZE}")
        return DatabasePool.MIN_POOL_SIZE

# Global database pool - dynamically sized and auto-adjusts during runtime
db_pool = DatabasePool(initial_max=calculate_optimal_pool_size())


# Background CPU tracker for accurate CPU measurements
class CPUTracker:
    """
    Tracks CPU usage in the background for accurate measurements.
    psutil.cpu_percent() needs to be called periodically to get accurate readings.
    """
    def __init__(self, update_interval=2.0):
        self.update_interval = update_interval
        self.process_cpu = 0.0
        self.system_cpu = 0.0
        self.lock = threading.Lock()
        self._running = False
        self._thread = None
        self._process = None
        
    def start(self):
        """Start background CPU tracking."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._track_cpu, daemon=True)
        self._thread.start()
        logger.info("📊 CPU tracker started")
        
    def stop(self):
        """Stop background CPU tracking."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("📊 CPU tracker stopped")
        
    def _track_cpu(self):
        """Background thread that periodically updates CPU measurements."""
        try:
            import psutil
            self._process = psutil.Process(os.getpid())
            
            # Initialize CPU measurement (first call always returns 0)
            self._process.cpu_percent()
            psutil.cpu_percent()
            
            while self._running:
                try:
                    time.sleep(self.update_interval)
                    
                    # Get CPU percentages
                    proc_cpu = self._process.cpu_percent()
                    sys_cpu = psutil.cpu_percent()
                    
                    with self.lock:
                        self.process_cpu = proc_cpu
                        self.system_cpu = sys_cpu
                        
                except Exception as e:
                    logger.debug(f"CPU tracking error: {e}")
                    
        except Exception as e:
            logger.warning(f"Failed to initialize CPU tracker: {e}")
            
    def get_cpu(self):
        """Get current CPU usage."""
        with self.lock:
            return {
                'process': self.process_cpu,
                'system': self.system_cpu
            }

# Global CPU tracker instance
cpu_tracker = CPUTracker(update_interval=2.0)


# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    signal_names = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT"}
    signal_name = signal_names.get(signum, f"Signal {signum}")
    logger.info(f"🛑 Received {signal_name}, initiating graceful shutdown...")
    
    with global_state_lock:
        shutdown_requested.set()
        heartbeat_active.clear()
    
    # Give some time for cleanup before forcing exit
    threading.Timer(30.0, force_exit).start()

def force_exit():
    """Force exit if graceful shutdown takes too long"""
    logger.warning("⚠️ Force exit after 30 seconds")
    os._exit(1)

# Register signal handlers (Windows compatible)
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Optional imports for enhanced features
try:
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    logger.warning(
        "⚠️ scikit-learn not available. Using basic relevance scoring only."
    )
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
        # Very basic fallback - just return random similarities
        if hasattr(a, "shape"):
            return [[0.5] * len(b) for _ in range(len(a))]
        return [[0.5]]

# Advanced semantic matching imports
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
    logger.info(
        "✅ Sentence transformers available for advanced semantic matching"
    )
except ImportError:
    logger.warning(
        "⚠️ sentence-transformers not available. "
        "Install with: pip install sentence-transformers"
    )
    SENTENCE_TRANSFORMERS_AVAILABLE = False

# Import numpy separately for statistical operations
try:
    import numpy as np
except ImportError:
    logger.warning("⚠️ numpy not available for statistical analysis")
    np = None

warnings.filterwarnings(
    action="ignore",
    message=(
        r"If 'per_message=False', 'CallbackQueryHandler' "
        r"will not be tracked for every message."
    ),
    category=UserWarning,
    module="telegram.ext.conversationhandler",
)

# Admin configuration
ADMIN_USER_ID = 7744296624  # Your Telegram user ID


# --- Text and Link Canonicalization Functions ---
def canonical_link(url: str) -> str:
    """Extract numeric job ID for consistent deduplication across LinkedIn, Indeed, Glassdoor."""
    # Lowercase first: LinkedIn %-encodes umlauts with UPPERCASE hex (köln -> k%C3%B6ln),
    # and the slug pattern's char class is lowercase-only. Without this, slug URLs with
    # German accents fail extraction and fall through to the full-URL fallback, breaking
    # dedup. Digits/hex IDs are case-insensitive so this is safe for all existing ids.
    url = (url or "").lower()
    patterns = [
        (r"/jobs/view/(\d+)", "li"),
        (r"/jobs/view/[a-z0-9%\-]+-(\d{7,})", "li"),
        (r"/jobs/(\d+)/", "li"),
        (r"[?&]jk=([a-f0-9]{8,})", "in"),
        (r"[?&]vjk=([a-f0-9]{8,})", "in"),
        (r"[?&]jl=(\d+)", "gd"),
        (r"job[_-](\d+)", "li"),
        (r"jobId[=:](\d+)", "li"),
    ]

    for pattern, prefix in patterns:
        m = re.search(pattern, url)
        if m:
            return f"{prefix}{m.group(1)}" if prefix in ("in", "gd") else m.group(1)

    base_url = url.lower().split("#")[0].rstrip("/")
    return base_url


def canonical_text(txt: str) -> str:
    """Normalize text: lowercase, strip accents, normalize spaces."""
    if not txt:
        return ""
    # Remove accents and non-ASCII characters
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    # Normalize whitespace and convert to lowercase
    return re.sub(r"\s+", " ", txt).strip().lower()


def parse_date_posted_to_datetime(date_str):
    """Convert LinkedIn's 'X days ago' format to actual datetime."""
    date_str = date_str.lower().strip()
    now = datetime.now(pytz.UTC)

    # Relative shortcuts
    if date_str in ("just now", "now"):
        return now
    if date_str == "today":
        return now
    if date_str == "yesterday":
        return now - timedelta(days=1)

    # Handle various formats
    if "minute" in date_str:
        m = re.search(r"\d+", date_str)
        n = int(m.group()) if m else 1
        return now - timedelta(minutes=n)
    if "hour" in date_str:
        hours_match = re.search(r"\d+", date_str)
        hours = int(hours_match.group()) if hours_match else 1
        return now - timedelta(hours=hours)
    if "day" in date_str:
        days_match = re.search(r"\d+", date_str)
        days = int(days_match.group()) if days_match else 1
        return now - timedelta(days=days)
    if "week" in date_str:
        weeks_match = re.search(r"\d+", date_str)
        weeks = int(weeks_match.group()) if weeks_match else 1
        return now - timedelta(weeks=weeks)
    if "month" in date_str:
        months_match = re.search(r"\d+", date_str)
        months = int(months_match.group()) if months_match else 1
        return now - timedelta(days=30 * months)
    if "year" in date_str:
        years_match = re.search(r"\d+", date_str)
        years = int(years_match.group()) if years_match else 1
        return now - timedelta(days=365 * years)
    iso_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if iso_match:
        try:
            y, mo, d = (int(iso_match.group(i)) for i in (1, 2, 3))
            # Use end-of-day so day-granularity dates pass the recency filter
            # (last_checked is an exact timestamp; midnight would be "before" any same-day check)
            return datetime(y, mo, d, 23, 59, 59, tzinfo=pytz.UTC)
        except (ValueError, OverflowError):
            pass
    # Absolute "DD Month YYYY" / "Month DD, YYYY" — the format _format_relative_posted
    # emits for jobs older than 30 days. Without this branch these fall through to the
    # `return now` default and wrongly pass the recency filter as if freshly posted,
    # so deep scrapes would spam months-old listings. strptime month names are
    # case-insensitive, which matches the lowercased date_str above.
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(hour=23, minute=59, second=59, tzinfo=pytz.UTC)
        except ValueError:
            continue
    return now  # Default to now if we can't parse it


# --- Helper Functions ---
def escape_markdown(text):
    """
    Escape Markdown special characters for Telegram MarkdownV1.
    
    Telegram MarkdownV1 only uses: * _ ` [
    We escape these to prevent them from being interpreted as formatting.
    """
    if not text:
        return "N/A"
    text = str(text)
    # For Telegram MarkdownV1, only these characters need escaping:
    # * (bold), _ (italic), ` (code), [ (links)
    # We also escape \ to prevent double-escaping issues
    for char in ['\\', '*', '_', '`', '[']:
        text = text.replace(char, '\\' + char)
    return text


def safe_answer_callback_query(query):
    """Safely answer callback queries with timeout handling."""
    try:
        query.answer()
    except (telegram.error.TimedOut, telegram.error.BadRequest):
        pass  # Ignore timeout and expired callback errors


def safe_edit_message(
    query, text, reply_markup=None,
    parse_mode=None, disable_web_page_preview=None
):
    """Safely edit messages with error handling."""
    try:
        if reply_markup:
            query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
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
    EXPERIENCE_MENU, JOB_TYPE_MENU, DATE_POSTED_MENU, WORKPLACE_MENU, Browse,
    ALERTS_MENU, MY_ALERTS, ADD_ALERT_KEYWORD, ADD_ALERT_LOCATION,
    ALERT_PREFERENCES, EDIT_ALERT_PREFERENCES, SET_TIMEZONE, GET_KEYWORD,
    GET_LOCATION, SAVED_JOBS,
) = range(19)

# Admin panel states (separate ConversationHandler, group=2)
(ADMIN_MENU, ADMIN_USER_ALERTS, ADMIN_ALERT_DETAILS, ADMIN_EDIT_KEYWORDS, ADMIN_EDIT_LOCATION, ADMIN_EDIT_FILTERS) = range(100, 106)
ADMIN_USERS_PER_PAGE = 10

JOBS_PER_PAGE = 5
MAX_SCRAPE_PAGES = 5

DATE_POSTED_OPTIONS = {
    "Past 24 hours": "r86400",
    "Past Week": "r604800",
    "Past Month": "r2592000"
}
EXPERIENCE_LEVELS = {
    "Internship": "1", "Entry level": "2", "Associate": "3",
    "Mid-Senior level": "4", "Director": "5", "Executive": "6"
}
JOB_TYPES = {
    "Full-time": "F", "Part-time": "P", "Contract": "C",
    "Temporary": "T", "Internship": "I"
}
WORKPLACE_TYPES = {"On-site": "1", "Remote": "2", "Hybrid": "3"}


# --- Database Setup ---
def init_db():
    """Initialize the PostgreSQL database and create/update tables."""
    conn = None
    try:
        with db_lock:
            conn = psycopg2.connect(
                host=DB_CONFIG["host"],
                port=DB_CONFIG["port"],
                database=DB_CONFIG["database"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                connect_timeout=10
            )
            conn.autocommit = False
            cursor = conn.cursor()

        # Table for storing user alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                keywords TEXT NOT NULL,
                location TEXT NOT NULL,
                filters TEXT,
                is_active INTEGER DEFAULT 1,
                last_checked TIMESTAMP DEFAULT NOW()
            )
        """)

        # Table for tracking jobs sent, now with robust deduplication
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_jobs (
                alert_id INTEGER,
                chat_id BIGINT NOT NULL,
                job_link TEXT NOT NULL,
                job_id TEXT NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                canonical_title TEXT NOT NULL,
                canonical_company TEXT NOT NULL,
                canonical_location TEXT NOT NULL DEFAULT '',
                sent_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (alert_id, job_link),
                FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
            )
        """)

        # Add new user_settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                chat_id BIGINT PRIMARY KEY,
                timezone TEXT
            )
        """)

        # Table for saved jobs (user bookmarks)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                job_link TEXT NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                date_posted TEXT,
                alert_keywords TEXT,
                alert_location TEXT,
                saved_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(chat_id, job_link)
            )
        """)

        # Table for caching job details (for save button functionality)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_details_cache (
                alert_id INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                job_link TEXT NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                date_posted TEXT,
                cached_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (alert_id, job_id),
                FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
            )
        """)

        # --- Safe Table Migration for PostgreSQL ---
        # Check if new columns exist and add them if they don't
        # for backwards compatibility
        try:
            cursor.execute("SELECT job_title, company FROM sent_jobs LIMIT 1")
        except psycopg2.Error:
            conn.rollback()
            logger.info(
                "Upgrading sent_jobs table: adding job_title and company "
                "columns..."
            )
            try:
                cursor.execute(
                    "ALTER TABLE sent_jobs "
                    "ADD COLUMN IF NOT EXISTS job_title TEXT NOT NULL DEFAULT 'N/A'"
                )
            except psycopg2.Error:
                conn.rollback()
            try:
                cursor.execute(
                    "ALTER TABLE sent_jobs "
                    "ADD COLUMN IF NOT EXISTS company TEXT NOT NULL DEFAULT 'N/A'"
                )
            except psycopg2.Error:
                conn.rollback()

        # Check and add new columns for robust deduplication (PostgreSQL syntax)
        columns_to_add = [
            ("chat_id", "BIGINT NOT NULL DEFAULT 0"),
            ("job_id", "TEXT NOT NULL DEFAULT ''"),
            ("canonical_title", "TEXT NOT NULL DEFAULT ''"),
            ("canonical_company", "TEXT NOT NULL DEFAULT ''"),
            ("canonical_location", "TEXT NOT NULL DEFAULT ''"),
            ("sent_at", "TIMESTAMP"),
        ]

        for col_name, col_def in columns_to_add:
            try:
                cursor.execute(
                    f"ALTER TABLE sent_jobs ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
                )
            except psycopg2.Error as e:
                conn.rollback()
                logger.warning(f"Failed to add {col_name}: {e}")

        conn.commit()  # Persist column additions before backfill

        # Migrate existing data to new format
        try:
            # Update job_id for existing records
            cursor.execute(
                "UPDATE sent_jobs SET job_id = %s "
                "WHERE job_id = '' OR job_id IS NULL", ("",)
            )
            cursor.execute(
                "SELECT ctid, job_link, job_title, company "
                "FROM sent_jobs WHERE job_id = ''"
            )
            rows = cursor.fetchall()
            for row in rows:
                job_id = canonical_link(row[1])
                c_title = canonical_text(row[2])
                c_company = canonical_text(row[3])
                cursor.execute(
                    "UPDATE sent_jobs SET job_id = %s, "
                    "canonical_title = %s, canonical_company = %s WHERE ctid = %s",
                    (job_id, c_title, c_company, row[0]),
                )

            # Update chat_id for existing records by joining with alerts
            cursor.execute("""
                UPDATE sent_jobs
                SET chat_id = alerts.chat_id
                FROM alerts
                WHERE alerts.id = sent_jobs.alert_id
                AND (sent_jobs.chat_id = 0 OR sent_jobs.chat_id IS NULL)
            """)

            logger.info("Migrated existing sent_jobs data to new format")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Migration warning (non-critical): {e}")

        # Backfill: extract numeric IDs from full-URL job_id values
        try:
            cursor.execute(r"""
                WITH candidates AS (
                    SELECT ctid, alert_id, job_id,
                           substring(job_id from '.*-(\d{7,})$') as numeric_id
                    FROM sent_jobs
                    WHERE job_id ~ '.*-\d{7,}$' AND length(job_id) > 20
                ),
                unique_ids AS (
                    SELECT numeric_id, alert_id
                    FROM candidates
                    GROUP BY numeric_id, alert_id
                    HAVING count(*) = 1
                )
                UPDATE sent_jobs s
                SET job_id = substring(s.job_id from '.*-(\d{7,})$')
                FROM unique_ids u
                WHERE s.alert_id = u.alert_id
                AND substring(s.job_id from '.*-(\d{7,})$') = u.numeric_id
                AND s.job_id ~ '.*-\d{7,}$' AND length(s.job_id) > 20
            """)
            backfilled = cursor.rowcount
            if backfilled > 0:
                logger.info(f"Backfilled {backfilled} job_id entries to numeric IDs")
        except Exception as e:
            conn.rollback()
            logger.warning(f"Job ID backfill warning (non-critical): {e}")

        # Create indexes for efficient deduplication
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_jobid "
                "ON sent_jobs(alert_id, job_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_jobid "
                "ON sent_jobs(chat_id, job_id)"
            )
            cursor.execute("DROP INDEX IF EXISTS idx_canonical")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical "
                "ON sent_jobs(chat_id, canonical_title, canonical_company, canonical_location)"
            )
            # Additional performance indexes for PostgreSQL
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_active "
                "ON alerts(is_active) WHERE is_active = 1"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_chat_id "
                "ON alerts(chat_id)"
            )
            logger.info("Created deduplication and performance indexes")
        except Exception as e:
            conn.rollback()
            logger.warning(f"Index creation warning: {e}")

        # Add user identity columns to user_settings for admin panel display
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS first_name TEXT")
            cursor.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS username TEXT")
        except Exception as e:
            conn.rollback()
            logger.warning(f"user_settings column addition warning: {e}")

        conn.commit()
        logger.info("PostgreSQL database initialized and schema updated successfully.")
    except psycopg2.Error as e:
        logger.error(f"Failed to initialize PostgreSQL database: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_db_connection():
    """Get a database connection from the pool with proper error handling."""
    max_retries = 3
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            conn = db_pool.get_connection()
            # Set cursor factory for dict-like access
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            return conn
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"Database connection failed, retrying in {retry_delay}s (attempt {attempt + 1}): {e}")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                logger.error(f"Database connection failed after {attempt + 1} attempts: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected database error: {e}")
            raise


def safe_db_operation(operation_func, *args, **kwargs):
    """Safely execute database operations with automatic connection cleanup and retries"""
    max_retries = 3
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        if shutdown_requested.is_set():
            raise RuntimeError("Shutdown requested, aborting database operation")
            
        conn = None
        try:
            with db_lock:
                conn = get_db_connection()
                result = operation_func(conn, *args, **kwargs)
                db_pool.return_connection(conn)  # Return connection to pool
                conn = None  # Mark as returned to avoid double-return
                return result
        except psycopg2.OperationalError as e:
            if conn:
                try:
                    conn.close()  # Don't return corrupted connections to pool
                except Exception:
                    pass
                conn = None
            if attempt < max_retries - 1:
                logger.warning(f"Database operation failed, retrying in {retry_delay}s... (attempt {attempt + 1}): {e}")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            logger.error(f"Database operation failed after {attempt + 1} attempts: {e}")
            raise
        except Exception as e:
            logger.error(f"Database operation failed: {e}")
            if conn:
                try:
                    conn.close()  # Don't return corrupted connections to pool
                except Exception:
                    pass
            raise
        finally:
            # Return connection to pool if not already done
            if conn:
                try:
                    db_pool.return_connection(conn)
                except Exception as e:
                    logger.warning(f"Failed to return database connection to pool: {e}")
    
    raise psycopg2.OperationalError("Database operation failed after all retries")


# --- Data Persistence Helper ---
def get_user_prefs(context: CallbackContext) -> dict:
    """Safely get user preferences, initializing if not present."""
    if "preferences" not in context.user_data:
        context.user_data["preferences"] = {
            "experience": {},
            "job_types": {},
            "date_posted": {},
            "workplace": {},
        }
    return context.user_data["preferences"]


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
        if user_id in user_operations and \
                operation_type in user_operations[user_id]:
            del user_operations[user_id][operation_type]
            if not user_operations[user_id]:  # Remove user if no operations
                del user_operations[user_id]


def is_user_busy(user_id: int, operation_type: str = None) -> bool:
    """Check if a user is busy with any or a specific operation."""
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
        if "not modified" not in str(e).lower():
            logger.warning(f"Progress message update failed: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error updating progress: {e}")


def run_concurrent_operation(func, *args, **kwargs):
    """Run an operation in a separate thread to avoid blocking other users."""
    try:
        # Check thread pool health before submitting
        if executor._shutdown:
            logger.warning("Thread pool is shutdown, cannot submit new tasks")
            return None
            
        future = executor.submit(func, *args, **kwargs)
        return future
    except Exception as e:
        logger.error(f"Failed to submit concurrent operation: {e}")
        return None


def cleanup_stuck_operations():
    """Clean up stuck user operations and threads"""
    import time
    current_time = time.time()
    stuck_operations = []
    
    with user_operations_lock:
        for user_id, operations in list(user_operations.items()):
            for operation_type, start_time in list(operations.items()):
                # If operation has been running for more than 10 minutes, consider it stuck
                if current_time - start_time > 600:  # 10 minutes
                    stuck_operations.append((user_id, operation_type))
                    logger.warning(f"🚨 Stuck operation detected: user {user_id}, operation {operation_type}")
    
    # Clean up stuck operations
    for user_id, operation_type in stuck_operations:
        unregister_user_operation(user_id, operation_type)
        logger.info(f"🧹 Cleaned up stuck operation: user {user_id}, operation {operation_type}")
    
    if stuck_operations:
        logger.info(f"🧹 Cleaned up {len(stuck_operations)} stuck operations")
        force_memory_cleanup()


# --- UI Generation Functions ---
def make_main_menu(context: CallbackContext) -> (str, InlineKeyboardMarkup):
    text = "👋 Welcome to Job Quest!"
    keyboard = [
        [InlineKeyboardButton("🚀 Start Search", callback_data="start_search")],
        [InlineKeyboardButton("🔔 Set Alert", callback_data="set_alert")],
        [InlineKeyboardButton("💾 Saved Jobs", callback_data="saved_jobs")],
        [InlineKeyboardButton("📋 Preferences", callback_data="prefs")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def make_preferences_menu(
    context: CallbackContext, chat_id: int
) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    experience = ", ".join(prefs["experience"].keys()) or "Not Set"
    job_types = ", ".join(prefs["job_types"].keys()) or "Not Set"
    date_posted = ""
    if prefs["date_posted"]:
        date_posted = list(prefs["date_posted"].keys())[0]
    else:
        date_posted = "Any"
    workplace = ", ".join(prefs["workplace"].keys()) or "Any"

    # Get user timezone
    conn = None
    user_timezone = "Not Set (UTC)"
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timezone FROM user_settings WHERE chat_id = %s", (chat_id,)
        )
        tz_row = cursor.fetchone()
        if tz_row and tz_row["timezone"]:
            user_timezone = tz_row["timezone"]
    finally:
        if conn:
            db_pool.return_connection(conn)

    text = (
        "⚙️ *Preferences*\n\n"
        f"∙ *Timezone:* `{user_timezone}`\n"
        f"∙ *Date Posted:* `{date_posted}`\n"
        f"∙ *Workplace:* `{workplace}`\n"
        f"∙ *Experience:* `{experience}`\n"
        f"∙ *Job Types:* `{job_types}`"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "🗓️ Set Date Posted", callback_data="set_date_posted"
            ),
            InlineKeyboardButton(
                "🏢 Set Workplace", callback_data="set_workplace"
            )
        ],
        [
            InlineKeyboardButton(
                "🎓 Set Experience", callback_data="set_experience"
            ),
            InlineKeyboardButton(
                "📝 Set Job Types", callback_data="set_job_types"
            )
        ],
        [InlineKeyboardButton("🌍 Set Timezone", callback_data="set_timezone")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def make_date_posted_menu(
    context: CallbackContext
) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    selected_value = None
    if prefs["date_posted"]:
        selected_value = list(prefs["date_posted"].values())[0]

    text = "🗓️ Choose Date Posted Filter"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text, callback_data=f"dp_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("Clear Filter", callback_data="dp_clear_None")
    ])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="dp_done")])
    return text, InlineKeyboardMarkup(keyboard)


def make_workplace_menu(
    context: CallbackContext
) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)
    selected_options = prefs["workplace"]

    text = ("🏢 Choose Your Workplace Types\n\n"
           "▫️ Click to select/deselect options\n"
           "▫️ Multiple selections use AND logic\n"
           "▫️ Click 'Done' when finished.")
    
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text, callback_data=f"wt_{option_id}_{option_text}"
            )
        ])

    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="wt_done")])
    return text, InlineKeyboardMarkup(keyboard)


def make_multi_select_menu(
    context: CallbackContext, menu_type: str
) -> (str, InlineKeyboardMarkup):
    prefs = get_user_prefs(context)

    if menu_type == "experience":
        title = "🎓 Choose Your Experience Levels"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs["experience"]
        callback_prefix = "exp"
    else:  # job_type
        title = "📝 Choose Your Job Types"
        options_dict = JOB_TYPES
        selected_options = prefs["job_types"]
        callback_prefix = "jt"

    text = f"{title}\n\n" \
           "▫️ Click to select/deselect options\n" \
           "▫️ Click 'Done' when finished."

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"{callback_prefix}_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "✔️ Done", callback_data=f"{callback_prefix}_done"
        )
    ])
    return text, InlineKeyboardMarkup(keyboard)


# --- User Info Tracking ---
def upsert_user_info(chat_id, first_name, username):
    """Store or update user identity info for admin panel display."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (chat_id, first_name, username)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE
            SET first_name = EXCLUDED.first_name, username = EXCLUDED.username
        """, (chat_id, first_name, username))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to upsert user info: {e}")
    finally:
        if conn:
            db_pool.return_connection(conn)


def backfill_user_info(bot):
    """Fetch Telegram profiles for existing users that don't have names stored yet."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT a.chat_id
            FROM alerts a
            LEFT JOIN user_settings us ON a.chat_id = us.chat_id
            WHERE us.first_name IS NULL
        """)
        rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to query users for backfill: {e}")
        return
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not rows:
        logger.info("User info backfill: all users already have names stored.")
        return

    logger.info(f"Backfilling user info for {len(rows)} user(s)...")
    success = 0
    for row in rows:
        chat_id = row["chat_id"]
        try:
            chat = bot.get_chat(chat_id)
            upsert_user_info(chat_id, chat.first_name, chat.username)
            success += 1
            time.sleep(0.1)  # Respect Telegram rate limits
        except Exception as e:
            logger.warning(f"Could not fetch profile for {chat_id}: {e}")
    logger.info(f"User info backfill complete: {success}/{len(rows)} users updated.")


# --- Start & Main Menu ---
def start(update: Update, context: CallbackContext):
    # Debounce the /start command
    now = time.time()
    last_call = context.user_data.get("last_start_call", 0)
    if now - last_call < 2:
        return None
    context.user_data["last_start_call"] = now

    # Track user identity for admin panel
    user = update.effective_user
    upsert_user_info(user.id, user.first_name, user.username)

    text, keyboard = make_main_menu(context)
    update.message.reply_text(text, reply_markup=keyboard)
    return MAIN_MENU


def main_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_main_menu(context)
    query.edit_message_text(text, reply_markup=keyboard)
    return MAIN_MENU


def start_from_callback(update: Update, context: CallbackContext):
    # Debounce the start button callback
    now = time.time()
    last_call = context.user_data.get("last_start_callback_call", 0)
    if now - last_call < 2:
        query = update.callback_query
        query.answer()
        return None
    context.user_data["last_start_callback_call"] = now
    
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
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
        ),
    )
    return MAIN_MENU


# --- Saved Jobs Functions ---
def saved_jobs_menu(update: Update, context: CallbackContext):
    """Display user's saved jobs with pagination."""
    query = update.callback_query
    safe_answer_callback_query(query)

    chat_id = query.from_user.id
    page = context.user_data.get("saved_jobs_page", 0)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get total count
        cursor.execute(
            "SELECT COUNT(*) FROM saved_jobs WHERE chat_id = %s", (chat_id,)
        )
        total_count = cursor.fetchone()["count"]

        # Get saved jobs for current page
        offset = page * JOBS_PER_PAGE
        cursor.execute(
            """SELECT id, job_title, company, location, date_posted, job_link,
               alert_keywords, alert_location, saved_at
               FROM saved_jobs
               WHERE chat_id = %s
               ORDER BY saved_at DESC
               LIMIT %s OFFSET %s""",
            (chat_id, JOBS_PER_PAGE, offset)
        )
        saved_jobs = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if total_count == 0:
        text = "💾 <b>Saved Jobs</b>\n\nYou haven't saved any jobs yet.\n\n" \
               "Click the 💾 Save button on job alerts to save them here!"
        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]]

        try:
            query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except telegram.error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise e
        return SAVED_JOBS

    # Build message with saved jobs using HTML (more reliable than Markdown)
    total_pages = (total_count + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE
    text = f"💾 <b>Saved Jobs</b> ({total_count} total)\n\n" \
           f"📄 Page {page + 1} of {total_pages}\n\n"

    for idx, job in enumerate(saved_jobs, 1):
        job_num = offset + idx
        # Use html.escape for HTML content
        title = html.escape(str(job["job_title"]) if job["job_title"] else "N/A")
        company = html.escape(str(job["company"]) if job["company"] else "N/A")
        location = html.escape(str(job["location"]) if job["location"] else "N/A")
        date_posted = html.escape(str(job["date_posted"]) if job["date_posted"] else "N/A")
        saved_at = str(job["saved_at"]) if job["saved_at"] else ""

        text += f"{job_num}. <b>{title}</b>\n"
        text += f"   🏢 {company}\n"
        text += f"   📍 {location}\n"
        text += f"   📅 Posted: {date_posted}\n"
        text += f"   💾 Saved: {saved_at[:10] if saved_at else 'N/A'}\n\n"

    # Build keyboard with job links and navigation
    keyboard = []
    for idx, job in enumerate(saved_jobs):
        job_num = offset + idx + 1
        saved_job_id = job["id"]  # Use the database ID
        job_link = job["job_link"]

        keyboard.append([
            InlineKeyboardButton(
                f"View Job {job_num}", url=job_link
            ),
            InlineKeyboardButton(
                f"🗑️ {job_num}", callback_data=f"unsave_job_{saved_job_id}"
            )
        ])

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Previous", callback_data="saved_jobs_prev")
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("➡️ Next", callback_data="saved_jobs_next")
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([
        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")
    ])

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e

    return SAVED_JOBS


def save_job_callback(update: Update, context: CallbackContext):
    """Handle saving a job from an alert."""
    query = update.callback_query
    query.answer("💾 Saving job...")

    chat_id = query.from_user.id
    callback_data = query.data
    
    # DEBUG logging
    logger.info(f"[SAVE_DEBUG] Callback received: {callback_data} from chat_id: {chat_id}")

    # Parse callback data: save_job_<alert_id>_<job_id>
    parts = callback_data.split("_", 3)
    if len(parts) < 4:
        logger.error(f"[SAVE_DEBUG] Invalid callback format: {callback_data}")
        query.answer("❌ Error saving job")
        return

    alert_id = parts[2]
    job_id = parts[3]
    
    logger.info(f"[SAVE_DEBUG] Parsed alert_id={alert_id}, job_id={job_id}")

    conn = None
    try:
        # Get job details from cache
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT job_link, job_title, company, location, date_posted
               FROM job_details_cache
               WHERE alert_id = %s AND job_id = %s""",
            (alert_id, job_id)
        )
        job_data = cursor.fetchone()
        
        logger.info(f"[SAVE_DEBUG] Cache lookup result: {job_data is not None}")

        # Fallback: if not in cache, try sent_jobs table (for older jobs)
        if not job_data:
            logger.info(f"[SAVE_DEBUG] Trying sent_jobs fallback...")
            cursor.execute(
                """SELECT job_link, job_title, company, 
                   NULL as location, NULL as date_posted
                   FROM sent_jobs
                   WHERE alert_id = %s AND job_id = %s AND chat_id = %s""",
                (alert_id, job_id, chat_id)
            )
            job_data = cursor.fetchone()
            
            logger.info(f"[SAVE_DEBUG] sent_jobs lookup result: {job_data is not None}")
            
            if not job_data:
                logger.error(f"[SAVE_DEBUG] Job not found in cache or sent_jobs: alert_id={alert_id}, job_id={job_id}")
                query.answer("❌ Job not found (may have expired)")
                return

        # Get alert details
        cursor.execute(
            """SELECT keywords, location
               FROM alerts
               WHERE id = %s""",
            (alert_id,)
        )
        alert_data = cursor.fetchone()

        # Try to save the job
        try:
            cursor.execute(
                """INSERT INTO saved_jobs
                   (chat_id, job_link, job_title, company, location, date_posted,
                    alert_keywords, alert_location)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (chat_id, job_data["job_link"], job_data["job_title"], job_data["company"],
                 job_data["location"], job_data["date_posted"],
                 alert_data["keywords"] if alert_data else None,
                 alert_data["location"] if alert_data else None)
            )
            conn.commit()
            query.answer("✅ Job saved!")

            # Update button to show "✅ Saved"
            try:
                # Get the job_unique_id from callback data
                job_unique_id = f"{alert_id}_{job_id}"

                # Update the message's inline keyboard
                new_keyboard = [
                    [
                        InlineKeyboardButton("View Job", url=job_data["job_link"]),
                        InlineKeyboardButton(
                            "✅ Saved", callback_data=f"unsave_from_alert_{job_unique_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts"),
                        InlineKeyboardButton("🏠 Start", callback_data="start_command")
                    ]
                ]
                query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
            except Exception as e:
                logger.error(f"Error updating button after save: {e}")

        except psycopg2.IntegrityError:
            conn.rollback()
            query.answer("ℹ️ Job already saved")
        except Exception as e:
            logger.error(f"Error saving job: {e}")
            query.answer("❌ Error saving job")
    finally:
        if conn:
            db_pool.return_connection(conn)


def unsave_from_alert_callback(update: Update, context: CallbackContext):
    """Handle unsaving a job from an alert message."""
    query = update.callback_query
    query.answer("🗑️ Removing from saved jobs...")

    chat_id = query.from_user.id

    # Parse callback data: unsave_from_alert_<alert_id>_<job_id>
    parts = query.data.split("_", 4)
    if len(parts) < 5:
        query.answer("❌ Error removing job")
        return

    alert_id = parts[3]
    job_id = parts[4]

    conn = None
    job_link = None
    try:
        # Get job link from cache
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT job_link FROM job_details_cache
               WHERE alert_id = %s AND job_id = %s""",
            (alert_id, job_id)
        )
        job_data = cursor.fetchone()

        # Fallback: if not in cache, try sent_jobs table (for older jobs)
        if not job_data:
            cursor.execute(
                """SELECT job_link FROM sent_jobs
                   WHERE alert_id = %s AND job_id = %s AND chat_id = %s""",
                (alert_id, job_id, chat_id)
            )
            job_data = cursor.fetchone()
            
            if not job_data:
                query.answer("❌ Job not found (may have expired)")
                return

        job_link = job_data["job_link"]

        # Delete the saved job
        cursor.execute(
            """DELETE FROM saved_jobs WHERE chat_id = %s AND job_link = %s""",
            (chat_id, job_link)
        )
        conn.commit()
        query.answer("✅ Removed from saved jobs")
    finally:
        if conn:
            db_pool.return_connection(conn)

    # Update button to show "💾 Save"
    if job_link:
        try:
            job_unique_id = f"{alert_id}_{job_id}"
            new_keyboard = [
                [
                    InlineKeyboardButton("View Job", url=job_link),
                    InlineKeyboardButton(
                        "💾 Save", callback_data=f"save_job_{job_unique_id}"
                    )
                ],
                [
                    InlineKeyboardButton("📋 My Alerts", callback_data="my_alerts"),
                    InlineKeyboardButton("🏠 Start", callback_data="start_command")
                ]
            ]
            query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
        except Exception as e:
            logger.error(f"Error updating button after unsave: {e}")


def unsave_job_callback(update: Update, context: CallbackContext):
    """Handle removing a saved job from the saved jobs menu."""
    query = update.callback_query
    query.answer("🗑️ Removing job...")

    # Parse callback data: unsave_job_<saved_job_id>
    parts = query.data.split("_", 2)
    if len(parts) < 3:
        query.answer("❌ Error removing job")
        return

    try:
        saved_job_id = int(parts[2])
    except ValueError:
        query.answer("❌ Invalid job ID")
        return

    conn = None
    try:
        # Delete the saved job by ID
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """DELETE FROM saved_jobs WHERE id = %s""",
            (saved_job_id,)
        )
        conn.commit()
        query.answer("✅ Job removed from saved jobs")
    finally:
        if conn:
            db_pool.return_connection(conn)

    # Refresh the saved jobs view
    saved_jobs_menu(update, context)

    return SAVED_JOBS


def saved_jobs_navigation(update: Update, context: CallbackContext):
    """Handle pagination for saved jobs."""
    query = update.callback_query
    query.answer()

    current_page = context.user_data.get("saved_jobs_page", 0)

    if query.data == "saved_jobs_next":
        context.user_data["saved_jobs_page"] = current_page + 1
    elif query.data == "saved_jobs_prev":
        context.user_data["saved_jobs_page"] = max(0, current_page - 1)

    saved_jobs_menu(update, context)
    return SAVED_JOBS


# --- Admin Commands ---
def admin_stats(update: Update, context: CallbackContext):
    """Admin command to view bot statistics"""
    user_id = update.effective_user.id

    # Check if user is admin
    if user_id != ADMIN_USER_ID:
        update.message.reply_text("⛔ This command is only available to the bot administrator.")
        return

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get total unique users
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM alerts")
        total_users = cursor.fetchone()["count"]

        # Get total active alerts
        cursor.execute("SELECT COUNT(*) FROM alerts WHERE is_active = 1")
        active_alerts = cursor.fetchone()["count"]

        # Get total paused alerts
        cursor.execute("SELECT COUNT(*) FROM alerts WHERE is_active = 0")
        paused_alerts = cursor.fetchone()["count"]

        # Get alerts checked in last 24 hours (using last_checked as proxy for activity)
        cursor.execute("""
            SELECT COUNT(*) FROM alerts
            WHERE last_checked > NOW() - INTERVAL '1 day'
        """)
        active_alerts_24h = cursor.fetchone()["count"]

        # Get alerts checked in last 7 days
        cursor.execute("""
            SELECT COUNT(*) FROM alerts
            WHERE last_checked > NOW() - INTERVAL '7 days'
        """)
        active_alerts_7d = cursor.fetchone()["count"]

        # Get total jobs sent
        cursor.execute("SELECT COUNT(*) FROM sent_jobs")
        total_jobs_sent = cursor.fetchone()["count"]

        # Get jobs sent in last 24 hours
        cursor.execute("""
            SELECT COUNT(*) FROM sent_jobs
            WHERE sent_at > NOW() - INTERVAL '1 day'
        """)
        jobs_sent_24h = cursor.fetchone()["count"]

        # Get most popular search keywords (top 5)
        cursor.execute("""
            SELECT keywords, COUNT(*) as count
            FROM alerts
            GROUP BY keywords
            ORDER BY count DESC
            LIMIT 5
        """)
        popular_keywords = cursor.fetchall()

        # Get most popular locations (top 5)
        cursor.execute("""
            SELECT location, COUNT(*) as count
            FROM alerts
            GROUP BY location
            ORDER BY count DESC
            LIMIT 5
        """)
        popular_locations = cursor.fetchall()

        # Format the statistics message
        stats_msg = f"""📊 **Bot Statistics**

👥 **Users & Alerts**
• Total Users: {total_users}
• Active Alerts: {active_alerts}
• Paused Alerts: {paused_alerts}
• Total Alerts: {active_alerts + paused_alerts}

📈 **Activity**
• Active Alerts (24h): {active_alerts_24h}
• Active Alerts (7d): {active_alerts_7d}

💼 **Job Notifications**
• Total Jobs Sent: {total_jobs_sent}
• Jobs Sent (24h): {jobs_sent_24h}

🔍 **Popular Keywords**
"""

        for i, row in enumerate(popular_keywords, 1):
            stats_msg += f"{i}. {row['keywords']} ({row['count']} alerts)\n"

        stats_msg += "\n🌍 **Popular Locations**\n"
        for i, row in enumerate(popular_locations, 1):
            stats_msg += f"{i}. {row['location']} ({row['count']} alerts)\n"

        # Get database pool stats
        pool_stats = db_pool.get_stats()
        
        # Get system resources
        resources = get_system_resources()
        
        stats_msg += f"""
💾 **System Resources**
• Memory: {resources.get('mem_mb', 0):.1f} MB ({resources.get('mem_pct', 0):.1f}%)
• CPU (Process): {resources.get('cpu_pct', 0):.1f}%
• CPU (System): {resources.get('sys_cpu_pct', 0):.1f}%
• Threads: {resources.get('threads', 0)}
• System Memory Available: {resources.get('sys_mem_avail_mb', 0):.1f} MB

🗄️ **Database Pool (PostgreSQL)**
• Max Connections: {pool_stats['max_connections']}
• Checked Out: {pool_stats['checked_out']}

🤖 **Model Status**
• JobBERT Loaded: {'Yes' if _global_jobbert_model else 'No'}
• Model Usage Count: {_model_usage_count}
"""

        update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Admin stats viewed by user {user_id}")

    except Exception as e:
        logger.error(f"Failed to generate admin stats: {e}", exc_info=True)
        update.message.reply_text(f"❌ Error generating statistics: {e}")
    finally:
        if conn:
            db_pool.return_connection(conn)


# --- Admin Panel Functions ---

def _format_user_label(first_name, username, chat_id):
    """Format a user display label for admin panel buttons/headers."""
    if first_name and username:
        return f"👤 {first_name} (@{username})"
    elif first_name:
        return f"👤 {first_name}"
    elif username:
        return f"👤 @{username}"
    else:
        return f"🆔 {chat_id}"


def admin_command(update: Update, context: CallbackContext):
    """Entry point for /admin — show paginated user list."""
    if update.effective_user.id != ADMIN_USER_ID:
        update.message.reply_text("⛔ This command is only available to the bot administrator.")
        return ConversationHandler.END

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM alerts")
        total_users = cursor.fetchone()["count"]

        cursor.execute("""
            SELECT a.chat_id, COUNT(*) AS alert_count,
                   SUM(CASE WHEN a.is_active = 1 THEN 1 ELSE 0 END) AS active_count,
                   us.first_name, us.username
            FROM alerts a
            LEFT JOIN user_settings us ON a.chat_id = us.chat_id
            GROUP BY a.chat_id, us.first_name, us.username
            ORDER BY alert_count DESC
            LIMIT %s OFFSET 0
        """, (ADMIN_USERS_PER_PAGE,))
        users = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    text = f"👑 *Admin Panel*\n\n👥 Users with alerts: {total_users}\n\nSelect a user:"
    keyboard = []
    for u in users:
        cid = u["chat_id"]
        total = u["alert_count"]
        active = u["active_count"] or 0
        label = _format_user_label(u["first_name"], u["username"], cid)
        keyboard.append([InlineKeyboardButton(
            f"{label} — {total} alerts ({active} active)",
            callback_data=f"adm_user_{cid}"
        )])

    nav_row = []
    if total_users > ADMIN_USERS_PER_PAGE:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_upage_{ADMIN_USERS_PER_PAGE}"))
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="adm_cancel")])

    update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_MENU


def admin_user_list(update: Update, context: CallbackContext):
    """Callback handler for adm_users and adm_upage_{offset}."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    offset = 0
    if query.data.startswith("adm_upage_"):
        offset = int(query.data.split("_")[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM alerts")
        total_users = cursor.fetchone()["count"]

        cursor.execute("""
            SELECT a.chat_id, COUNT(*) AS alert_count,
                   SUM(CASE WHEN a.is_active = 1 THEN 1 ELSE 0 END) AS active_count,
                   us.first_name, us.username
            FROM alerts a
            LEFT JOIN user_settings us ON a.chat_id = us.chat_id
            GROUP BY a.chat_id, us.first_name, us.username
            ORDER BY alert_count DESC
            LIMIT %s OFFSET %s
        """, (ADMIN_USERS_PER_PAGE, offset))
        users = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    page_num = offset // ADMIN_USERS_PER_PAGE + 1
    total_pages = (total_users + ADMIN_USERS_PER_PAGE - 1) // ADMIN_USERS_PER_PAGE
    text = f"👑 *Admin Panel*\n\n👥 Users with alerts: {total_users} (page {page_num}/{total_pages})\n\nSelect a user:"

    keyboard = []
    for u in users:
        cid = u["chat_id"]
        total = u["alert_count"]
        active = u["active_count"] or 0
        label = _format_user_label(u["first_name"], u["username"], cid)
        keyboard.append([InlineKeyboardButton(
            f"{label} — {total} alerts ({active} active)",
            callback_data=f"adm_user_{cid}"
        )])

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_upage_{max(0, offset - ADMIN_USERS_PER_PAGE)}"))
    if offset + ADMIN_USERS_PER_PAGE < total_users:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_upage_{offset + ADMIN_USERS_PER_PAGE}"))
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="adm_cancel")])

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_MENU


def admin_view_user_alerts(update: Update, context: CallbackContext):
    """Show all alerts for a specific user."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    # Extract chat_id from adm_user_{chat_id} or adm_back_user_{chat_id}
    parts = query.data.split("_")
    chat_id = int(parts[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT first_name, username FROM user_settings WHERE chat_id = %s",
            (chat_id,)
        )
        user_info = cursor.fetchone()
        cursor.execute(
            "SELECT id, keywords, location, is_active, last_checked FROM alerts WHERE chat_id = %s ORDER BY id",
            (chat_id,)
        )
        alerts = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    user_label = _format_user_label(
        user_info["first_name"] if user_info else None,
        user_info["username"] if user_info else None,
        chat_id
    )
    escaped_label = escape_markdown(user_label)
    if not alerts:
        text = f"{escaped_label}\n\nNo alerts found."
    else:
        text = f"{escaped_label}\n\n📋 {len(alerts)} alert(s):\n"

    keyboard = []
    for a in alerts:
        status_icon = "🟢" if a["is_active"] else "🔴"
        kw = a["keywords"][:30]
        loc = a["location"][:20]
        keyboard.append([InlineKeyboardButton(
            f"{status_icon} #{a['id']} {kw} • {loc}",
            callback_data=f"adm_va_{a['id']}"
        )])

    keyboard.append([InlineKeyboardButton("🗑️ Delete User", callback_data=f"adm_deluserstart_{chat_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Users", callback_data="adm_users")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="adm_cancel")])

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_USER_ALERTS


def admin_view_alert_details(update: Update, context: CallbackContext):
    """Show full details of an alert with action buttons."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = int(query.data.split("_")[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()

        if not alert:
            query.edit_message_text("❌ Alert not found.")
            return ADMIN_USER_ALERTS

        cursor.execute("SELECT COUNT(*) FROM sent_jobs WHERE alert_id = %s", (alert_id,))
        sent_count = cursor.fetchone()["count"]

        chat_id = alert["chat_id"]
        cursor.execute(
            "SELECT first_name, username FROM user_settings WHERE chat_id = %s",
            (chat_id,)
        )
        user_info = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    status_icon = "🟢" if alert["is_active"] else "🔴"
    status_text = "Active" if alert["is_active"] else "Paused"

    last_checked_display = "Never"
    last_checked_val = alert["last_checked"]
    if last_checked_val:
        try:
            if isinstance(last_checked_val, datetime):
                utc_dt = last_checked_val.replace(tzinfo=pytz.utc) if last_checked_val.tzinfo is None else last_checked_val
            else:
                utc_dt = datetime.strptime(str(last_checked_val).split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
            last_checked_display = utc_dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, pytz.UnknownTimeZoneError):
            last_checked_display = str(last_checked_val)[:16]

    filters_text = ""
    if alert["filters"]:
        try:
            f = json.loads(alert["filters"])
            experience = ", ".join(f.get("experience", {}).keys()) or "Any"
            job_types = ", ".join(f.get("job_types", {}).keys()) or "Any"
            date_posted = list(f.get("date_posted", {}).keys())[0] if f.get("date_posted") else "Any"
            workplace = list(f.get("workplace", {}).keys())[0] if f.get("workplace") else "Any"
            filters_text = (
                f"\n<b>Filters:</b>\n"
                f"∙ Date Posted: <code>{html.escape(date_posted)}</code>\n"
                f"∙ Workplace: <code>{html.escape(workplace)}</code>\n"
                f"∙ Experience: <code>{html.escape(experience)}</code>\n"
                f"∙ Job Types: <code>{html.escape(job_types)}</code>\n"
            )
        except (json.JSONDecodeError, KeyError):
            filters_text = "\n<i>Filters: unable to parse</i>\n"

    # Build user display string
    first_name = user_info["first_name"] if user_info else None
    uname = user_info["username"] if user_info else None
    if first_name and uname:
        user_display = f"{html.escape(first_name)} (@{html.escape(uname)}) ({chat_id})"
    elif first_name:
        user_display = f"{html.escape(first_name)} ({chat_id})"
    elif uname:
        user_display = f"@{html.escape(uname)} ({chat_id})"
    else:
        user_display = f"<code>{chat_id}</code>"

    text = (
        f"🔔 <b>Alert #{alert_id}</b>\n\n"
        f"👤 <b>User:</b> {user_display}\n"
        f"📝 <b>Keywords:</b> {html.escape(alert['keywords'])}\n"
        f"📍 <b>Location:</b> {html.escape(alert['location'])}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n"
        f"📬 <b>Jobs Sent:</b> {sent_count}\n"
        f"🕒 <b>Last Checked:</b> {last_checked_display}\n"
        f"{filters_text}"
    )

    action_text = "⏸️ Pause" if alert["is_active"] else "▶️ Resume"
    action_cb = f"adm_pause_{alert_id}" if alert["is_active"] else f"adm_resume_{alert_id}"

    keyboard = [
        [InlineKeyboardButton("✏️ Edit Keywords", callback_data=f"adm_editkw_{alert_id}"),
         InlineKeyboardButton("✏️ Edit Location", callback_data=f"adm_editloc_{alert_id}")],
        [InlineKeyboardButton("🔧 Edit Filters", callback_data=f"adm_editflt_{alert_id}")],
        [InlineKeyboardButton(action_text, callback_data=action_cb)],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"adm_delstart_{alert_id}")],
        [InlineKeyboardButton("⬅️ Back to User Alerts", callback_data=f"adm_back_user_{chat_id}")],
        [InlineKeyboardButton("⬅️ Back to Users", callback_data="adm_users")],
        [InlineKeyboardButton("❌ Close", callback_data="adm_cancel")],
    ]

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_ALERT_DETAILS


def admin_toggle_alert(update: Update, context: CallbackContext):
    """Pause or resume an alert from admin panel."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    # adm_pause_{id} or adm_resume_{id}
    parts = query.data.split("_")
    action = parts[1]  # "pause" or "resume"
    alert_id = int(parts[2])
    new_status = 0 if action == "pause" else 1

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET is_active = %s WHERE id = %s", (new_status, alert_id))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    query.answer(f"Alert {'paused' if new_status == 0 else 'resumed'}.")
    # Re-render alert details
    query.data = f"adm_va_{alert_id}"
    return admin_view_alert_details(update, context)



def admin_edit_keywords_start(update: Update, context: CallbackContext):
    """Prompt admin to enter new keywords for an alert."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = int(query.data.split("_")[-1])
    context.user_data["admin_edit_alert_id"] = alert_id

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT keywords FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not alert:
        query.edit_message_text("\u274c Alert not found.")
        return ADMIN_ALERT_DETAILS

    query.edit_message_text(
        f"\u270f\ufe0f *Edit Keywords for Alert \\#{alert_id}*\n\n"
        f"Current: `{escape_markdown(alert['keywords'])}`\n\n"
        f"Send the new keywords:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Go Back", callback_data=f"adm_va_{alert_id}"),
             InlineKeyboardButton("\u274c Cancel", callback_data="adm_cancel")]
        ])
    )
    return ADMIN_EDIT_KEYWORDS


def admin_edit_keywords_receive(update: Update, context: CallbackContext):
    """Receive new keywords from admin and update the alert."""
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    alert_id = context.user_data.get("admin_edit_alert_id")
    if not alert_id:
        update.message.reply_text("\u274c No alert selected.")
        return ADMIN_ALERT_DETAILS

    new_keywords = update.message.text.strip()
    if not new_keywords:
        update.message.reply_text("\u274c Keywords cannot be empty. Try again or /cancel.")
        return ADMIN_EDIT_KEYWORDS

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET keywords = %s WHERE id = %s", (new_keywords, alert_id))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    update.message.reply_text(f"\u2705 Keywords updated to: *{escape_markdown(new_keywords)}*", parse_mode=ParseMode.MARKDOWN)

    # Show alert details again
    context.user_data.pop("admin_edit_alert_id", None)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if alert:
        _send_admin_alert_details(update.message, alert_id, context)
    return ADMIN_ALERT_DETAILS


def admin_edit_location_start(update: Update, context: CallbackContext):
    """Prompt admin to enter new location for an alert."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = int(query.data.split("_")[-1])
    context.user_data["admin_edit_alert_id"] = alert_id

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT location FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not alert:
        query.edit_message_text("\u274c Alert not found.")
        return ADMIN_ALERT_DETAILS

    query.edit_message_text(
        f"\u270f\ufe0f *Edit Location for Alert \\#{alert_id}*\n\n"
        f"Current: `{escape_markdown(alert['location'])}`\n\n"
        f"Send the new location:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f Go Back", callback_data=f"adm_va_{alert_id}"),
             InlineKeyboardButton("\u274c Cancel", callback_data="adm_cancel")]
        ])
    )
    return ADMIN_EDIT_LOCATION


def admin_edit_location_receive(update: Update, context: CallbackContext):
    """Receive new location from admin and update the alert."""
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    alert_id = context.user_data.get("admin_edit_alert_id")
    if not alert_id:
        update.message.reply_text("\u274c No alert selected.")
        return ADMIN_ALERT_DETAILS

    new_location = update.message.text.strip()
    if not new_location:
        update.message.reply_text("\u274c Location cannot be empty. Try again or /cancel.")
        return ADMIN_EDIT_LOCATION

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET location = %s WHERE id = %s", (new_location, alert_id))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    update.message.reply_text(f"\u2705 Location updated to: *{escape_markdown(new_location)}*", parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop("admin_edit_alert_id", None)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if alert:
        _send_admin_alert_details(update.message, alert_id, context)
    return ADMIN_ALERT_DETAILS



def admin_edit_filters_start(update: Update, context: CallbackContext):
    """Show filter category menu for admin filter editing."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = int(query.data.split("_")[-1])
    context.user_data["admin_edit_alert_id"] = alert_id

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT filters FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not alert:
        query.edit_message_text("❌ Alert not found.")
        return ADMIN_ALERT_DETAILS

    try:
        filters = json.loads(alert["filters"]) if alert["filters"] else {}
    except (json.JSONDecodeError, TypeError):
        filters = {}
    for key in ("experience", "job_types", "date_posted", "workplace"):
        filters.setdefault(key, {})
    context.user_data["admin_edit_filters"] = filters

    return _show_admin_filter_menu(query, alert_id, filters)


def _show_admin_filter_menu(query, alert_id, filters):
    """Render the admin filter category menu."""
    experience = ", ".join(filters.get("experience", {}).keys()) or "Any"
    job_types = ", ".join(filters.get("job_types", {}).keys()) or "Any"
    date_posted = list(filters.get("date_posted", {}).keys())[0] if filters.get("date_posted") else "Any"
    workplace = ", ".join(filters.get("workplace", {}).keys()) or "Any"

    text = (
        f"🔧 <b>Edit Filters for Alert #{alert_id}</b>\n\n"
        f"<b>Current Filters:</b>\n"
        f"∙ Date Posted: <code>{html.escape(date_posted)}</code>\n"
        f"∙ Workplace: <code>{html.escape(workplace)}</code>\n"
        f"∙ Experience: <code>{html.escape(experience)}</code>\n"
        f"∙ Job Types: <code>{html.escape(job_types)}</code>"
    )
    keyboard = [
        [InlineKeyboardButton("🗓️ Date Posted", callback_data="adm_flt_cat_dp"),
         InlineKeyboardButton("🏢 Workplace", callback_data="adm_flt_cat_wp")],
        [InlineKeyboardButton("🎓 Experience", callback_data="adm_flt_cat_exp"),
         InlineKeyboardButton("📝 Job Types", callback_data="adm_flt_cat_jt")],
        [InlineKeyboardButton("⬅️ Go Back", callback_data=f"adm_va_{alert_id}")],
    ]
    try:
        query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_EDIT_FILTERS


def admin_filter_show_date_posted(update: Update, context: CallbackContext):
    """Show Date Posted options (single-select)."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    current = filters.get("date_posted", {})

    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = option_text in current
        display = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display, callback_data=f"adm_flt_dp_{option_id}_{option_text}")])
    keyboard.append([InlineKeyboardButton("🗑️ Clear", callback_data="adm_flt_dp_clear_clear")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="adm_flt_done_dp")])

    query.edit_message_text("🗓️ <b>Date Posted</b>\nSelect one option:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return ADMIN_EDIT_FILTERS


def admin_filter_date_posted_selected(update: Update, context: CallbackContext):
    """Handle Date Posted option selection."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    parts = query.data.split("_", 4)
    option_id = parts[3]
    option_text = parts[4]

    if option_id == "clear":
        filters["date_posted"] = {}
    elif option_text in filters.get("date_posted", {}):
        filters["date_posted"] = {}
    else:
        filters["date_posted"] = {option_text: option_id}

    context.user_data["admin_edit_filters"] = filters
    return admin_filter_show_date_posted(update, context)


def admin_filter_show_workplace(update: Update, context: CallbackContext):
    """Show Workplace options (multi-select)."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    selected = filters.get("workplace", {})

    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = option_text in selected
        display = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display, callback_data=f"adm_flt_wp_{option_id}_{option_text}")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="adm_flt_done_wp")])

    query.edit_message_text("🏢 <b>Workplace</b>\nToggle options:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return ADMIN_EDIT_FILTERS


def admin_filter_workplace_selected(update: Update, context: CallbackContext):
    """Handle Workplace option toggle."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    parts = query.data.split("_", 4)
    option_id = parts[3]
    option_text = parts[4]

    selected = filters.setdefault("workplace", {})
    if option_text in selected:
        del selected[option_text]
    else:
        selected[option_text] = option_id

    context.user_data["admin_edit_filters"] = filters
    return admin_filter_show_workplace(update, context)


def admin_filter_show_experience(update: Update, context: CallbackContext):
    """Show Experience options (multi-select)."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    selected = filters.get("experience", {})

    keyboard = []
    for option_text, option_id in EXPERIENCE_LEVELS.items():
        is_selected = option_text in selected
        display = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display, callback_data=f"adm_flt_exp_{option_id}_{option_text}")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="adm_flt_done_exp")])

    query.edit_message_text("🎓 <b>Experience Level</b>\nToggle options:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return ADMIN_EDIT_FILTERS


def admin_filter_experience_selected(update: Update, context: CallbackContext):
    """Handle Experience option toggle."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    parts = query.data.split("_", 4)
    option_id = parts[3]
    option_text = parts[4]

    selected = filters.setdefault("experience", {})
    if option_text in selected:
        del selected[option_text]
    else:
        selected[option_text] = option_id

    context.user_data["admin_edit_filters"] = filters
    return admin_filter_show_experience(update, context)


def admin_filter_show_job_types(update: Update, context: CallbackContext):
    """Show Job Types options (multi-select)."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    selected = filters.get("job_types", {})

    keyboard = []
    for option_text, option_id in JOB_TYPES.items():
        is_selected = option_text in selected
        display = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([InlineKeyboardButton(display, callback_data=f"adm_flt_jt_{option_id}_{option_text}")])
    keyboard.append([InlineKeyboardButton("✔️ Done", callback_data="adm_flt_done_jt")])

    query.edit_message_text("📝 <b>Job Types</b>\nToggle options:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return ADMIN_EDIT_FILTERS


def admin_filter_job_types_selected(update: Update, context: CallbackContext):
    """Handle Job Types option toggle."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    filters = context.user_data.get("admin_edit_filters", {})
    parts = query.data.split("_", 4)
    option_id = parts[3]
    option_text = parts[4]

    selected = filters.setdefault("job_types", {})
    if option_text in selected:
        del selected[option_text]
    else:
        selected[option_text] = option_id

    context.user_data["admin_edit_filters"] = filters
    return admin_filter_show_job_types(update, context)


def admin_filter_done(update: Update, context: CallbackContext):
    """Save filters to DB and return to filter category menu."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = context.user_data.get("admin_edit_alert_id")
    filters = context.user_data.get("admin_edit_filters", {})

    if not alert_id:
        query.edit_message_text("❌ No alert selected.")
        return ADMIN_ALERT_DETAILS

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET filters = %s WHERE id = %s", (json.dumps(filters), alert_id))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    return _show_admin_filter_menu(query, alert_id, filters)


def _send_admin_alert_details(message, alert_id, context):
    """Send alert details as a new message (used after text input edits)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM alerts WHERE id = %s", (alert_id,))
        alert = cursor.fetchone()
        if not alert:
            message.reply_text("\u274c Alert not found.")
            return

        cursor.execute("SELECT COUNT(*) FROM sent_jobs WHERE alert_id = %s", (alert_id,))
        sent_count = cursor.fetchone()["count"]

        chat_id = alert["chat_id"]
        cursor.execute(
            "SELECT first_name, username FROM user_settings WHERE chat_id = %s",
            (chat_id,)
        )
        user_info = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    status_icon = "\U0001f7e2" if alert["is_active"] else "\U0001f534"
    status_text = "Active" if alert["is_active"] else "Paused"

    last_checked_display = "Never"
    last_checked_val = alert["last_checked"]
    if last_checked_val:
        try:
            if isinstance(last_checked_val, datetime):
                utc_dt = last_checked_val.replace(tzinfo=pytz.utc) if last_checked_val.tzinfo is None else last_checked_val
            else:
                utc_dt = datetime.strptime(str(last_checked_val).split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
            last_checked_display = utc_dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, pytz.UnknownTimeZoneError):
            last_checked_display = str(last_checked_val)[:16]

    filters_text = ""
    if alert["filters"]:
        try:
            f = json.loads(alert["filters"])
            experience = ", ".join(f.get("experience", {}).keys()) or "Any"
            job_types = ", ".join(f.get("job_types", {}).keys()) or "Any"
            date_posted = list(f.get("date_posted", {}).keys())[0] if f.get("date_posted") else "Any"
            workplace = list(f.get("workplace", {}).keys())[0] if f.get("workplace") else "Any"
            filters_text = (
                f"\n<b>Filters:</b>\n"
                f"\u2219 Date Posted: <code>{html.escape(date_posted)}</code>\n"
                f"\u2219 Workplace: <code>{html.escape(workplace)}</code>\n"
                f"\u2219 Experience: <code>{html.escape(experience)}</code>\n"
                f"\u2219 Job Types: <code>{html.escape(job_types)}</code>\n"
            )
        except (json.JSONDecodeError, KeyError):
            filters_text = "\n<i>Filters: unable to parse</i>\n"

    first_name = user_info["first_name"] if user_info else None
    uname = user_info["username"] if user_info else None
    if first_name and uname:
        user_display = f"{html.escape(first_name)} (@{html.escape(uname)}) ({chat_id})"
    elif first_name:
        user_display = f"{html.escape(first_name)} ({chat_id})"
    elif uname:
        user_display = f"@{html.escape(uname)} ({chat_id})"
    else:
        user_display = f"<code>{chat_id}</code>"

    text = (
        f"\U0001f514 <b>Alert #{alert_id}</b>\n\n"
        f"\U0001f464 <b>User:</b> {user_display}\n"
        f"\U0001f4dd <b>Keywords:</b> {html.escape(alert['keywords'])}\n"
        f"\U0001f4cd <b>Location:</b> {html.escape(alert['location'])}\n"
        f"\U0001f4ca <b>Status:</b> {status_icon} {status_text}\n"
        f"\U0001f4ec <b>Jobs Sent:</b> {sent_count}\n"
        f"\U0001f551 <b>Last Checked:</b> {last_checked_display}\n"
        f"{filters_text}"
    )

    action_text = "\u23f8\ufe0f Pause" if alert["is_active"] else "\u25b6\ufe0f Resume"
    action_cb = f"adm_pause_{alert_id}" if alert["is_active"] else f"adm_resume_{alert_id}"

    keyboard = [
        [InlineKeyboardButton("\u270f\ufe0f Edit Keywords", callback_data=f"adm_editkw_{alert_id}"),
         InlineKeyboardButton("\u270f\ufe0f Edit Location", callback_data=f"adm_editloc_{alert_id}")],
        [InlineKeyboardButton("\U0001f527 Edit Filters", callback_data=f"adm_editflt_{alert_id}")],
        [InlineKeyboardButton(action_text, callback_data=action_cb)],
        [InlineKeyboardButton("\U0001f5d1\ufe0f Delete", callback_data=f"adm_delstart_{alert_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back to User Alerts", callback_data=f"adm_back_user_{chat_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back to Users", callback_data="adm_users")],
        [InlineKeyboardButton("\u274c Close", callback_data="adm_cancel")],
    ]

    message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )


def admin_delete_alert_start(update: Update, context: CallbackContext):
    """Show delete confirmation for an alert."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    alert_id = int(query.data.split("_")[-1])

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"adm_delconf_{alert_id}"),
            InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_va_{alert_id}"),
        ],
    ]

    try:
        query.edit_message_text(
            f"⚠️ Are you sure you want to delete alert #{alert_id} and all its sent job records?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_ALERT_DETAILS


def admin_delete_alert_confirm(update: Update, context: CallbackContext):
    """Delete the alert and navigate back to user's alerts."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    alert_id = int(query.data.split("_")[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Get chat_id before deleting
        cursor.execute("SELECT chat_id FROM alerts WHERE id = %s", (alert_id,))
        row = cursor.fetchone()
        if not row:
            query.answer("Alert not found.")
            return ADMIN_MENU
        chat_id = row["chat_id"]

        cursor.execute("DELETE FROM sent_jobs WHERE alert_id = %s", (alert_id,))
        cursor.execute("DELETE FROM alerts WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    query.answer("Alert deleted.")
    # Navigate back to user's alerts
    query.data = f"adm_back_user_{chat_id}"
    return admin_view_user_alerts(update, context)


def admin_delete_user_start(update: Update, context: CallbackContext):
    """Show delete confirmation for a user and all their data."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)

    chat_id = int(query.data.split("_")[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT first_name, username FROM user_settings WHERE chat_id = %s",
            (chat_id,)
        )
        user_info = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) as cnt FROM alerts WHERE chat_id = %s", (chat_id,))
        alert_count = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM saved_jobs WHERE chat_id = %s", (chat_id,))
        saved_count = cursor.fetchone()["cnt"]
    finally:
        if conn:
            db_pool.return_connection(conn)

    user_label = _format_user_label(
        user_info["first_name"] if user_info else None,
        user_info["username"] if user_info else None,
        chat_id
    )

    text = (
        f"⚠️ Are you sure you want to delete user {user_label} and ALL their data?\n\n"
        f"This will remove:\n"
        f"• {alert_count} alert(s) (and all sent job records)\n"
        f"• {saved_count} saved job(s)\n"
        f"• User settings\n\n"
        f"This action cannot be undone."
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"adm_deluserconf_{chat_id}"),
            InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_user_{chat_id}"),
        ],
    ]

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    return ADMIN_USER_ALERTS


def admin_delete_user_confirm(update: Update, context: CallbackContext):
    """Delete user and all their data, navigate back to user list."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END

    chat_id = int(query.data.split("_")[-1])

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # saved_jobs is not cascaded from alerts, delete separately
        cursor.execute("DELETE FROM saved_jobs WHERE chat_id = %s", (chat_id,))
        # alerts CASCADE to sent_jobs and job_details_cache
        cursor.execute("DELETE FROM alerts WHERE chat_id = %s", (chat_id,))
        cursor.execute("DELETE FROM user_settings WHERE chat_id = %s", (chat_id,))
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    query.answer("User deleted.")
    # Navigate back to user list
    query.data = "adm_users"
    return admin_user_list(update, context)


def admin_cancel(update: Update, context: CallbackContext):
    """Close the admin panel via callback."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    safe_answer_callback_query(query)
    try:
        query.edit_message_text("👑 Admin panel closed.")
    except telegram.error.BadRequest:
        pass
    return ConversationHandler.END


def admin_cancel_command(update: Update, context: CallbackContext):
    """Close the admin panel via /cancel command."""
    if update.effective_user.id != ADMIN_USER_ID:
        return ConversationHandler.END
    update.message.reply_text("👑 Admin panel closed.")
    return ConversationHandler.END


# --- Search and Preferences Flow ---
def start_search_flow(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    query.edit_message_text("Please enter the job title or keywords.")
    return GET_SEARCH_KEYWORD


def keyword_received(update: Update, context: CallbackContext):
    context.user_data["search_keywords"] = update.message.text
    update.message.reply_text(
        "Great. Now, what location are you interested in?\n\n"
        "⚠️ Note: Enter only ONE location (e.g., New York, Remote)."
    )
    return GET_SEARCH_LOCATION


def location_received(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    # Check if user is already busy with a search
    if is_user_busy(user_id, "search"):
        update.message.reply_text(
            "⏳ You already have a search in progress. Please wait for it to "
            "complete."
        )
        return GET_SEARCH_LOCATION

    context.user_data["search_location"] = update.message.text
    progress_msg = update.message.reply_text("🚀 Kicking off the search...")

    # Register the search operation
    register_user_operation(user_id, "search")

    # Run search in background thread
    run_concurrent_operation(
        run_scrape_threaded, update, context, progress_msg
    )

    return Browse


# --- Preferences Flow ---
def preferences_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text, keyboard = make_preferences_menu(context, query.from_user.id)
    query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
    )
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

    _, option_id, option_text = update.callback_query.data.split("_", 2)

    if option_id == "clear" or option_id in prefs["date_posted"].values():
        prefs["date_posted"] = {}
    else:
        prefs["date_posted"] = {option_text: option_id}

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

    _, option_id, option_text = update.callback_query.data.split("_", 2)

    selected_dict = prefs["workplace"]

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    # Re-render the menu to show the change
    text, keyboard = make_workplace_menu(context)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return WORKPLACE_MENU


def ask_for_preference(
    update: Update, context: CallbackContext, pref_type: str
):
    query = update.callback_query
    query.answer()

    if pref_type == "keywords":
        query.edit_message_text(
            "Please send your job keywords, separated by a comma "
            "(e.g., AI Engineer, Python Developer)."
        )
        return GET_KEYWORD
    # locations
    query.edit_message_text(
        "Please send your preferred location.\n"
        "(e.g., New York, London, Remote)\n\n"
        "⚠️ Note: Enter only ONE location."
    )
    return GET_LOCATION


def save_text_preference(
    update: Update, context: CallbackContext, pref_type: str
):
    prefs = get_user_prefs(context)
    user_input = [item.strip() for item in update.message.text.split(",")]
    prefs[pref_type] = user_input

    update.message.reply_text(f"✅ Your {pref_type} have been saved!")

    text, keyboard = make_preferences_menu(context, update.message.chat_id)
    update.message.reply_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
    )
    return PREFERENCES_MENU


# --- Multi-Select Menu Flow (Experience & Job Type) ---
def show_multi_select_menu(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()
    text, keyboard = make_multi_select_menu(context, menu_type)
    try:
        query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return EXPERIENCE_MENU if menu_type == "experience" else JOB_TYPE_MENU


def toggle_multi_select_option(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()
    prefs = get_user_prefs(context)

    _, option_id, option_text = query.data.split("_", 2)

    if menu_type == "experience":
        selected_dict = prefs["experience"]
    else:  # job_type
        selected_dict = prefs["job_types"]

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
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
    return EXPERIENCE_MENU if menu_type == "experience" else JOB_TYPE_MENU


# --- Scraping Logic ---
def parse_date_posted(date_str):
    date_str = date_str.lower().strip()
    now = datetime.now()
    if "hour" in date_str:
        return now - timedelta(hours=int(re.search(r"\d+", date_str).group()))
    if "day" in date_str:
        return now - timedelta(days=int(re.search(r"\d+", date_str).group()))
    if "week" in date_str:
        return now - timedelta(weeks=int(re.search(r"\d+", date_str).group()))
    if "month" in date_str:
        return now - timedelta(
            days=30 * int(re.search(r"\d+", date_str).group())
        )
    if "year" in date_str:
        return now - timedelta(
            days=365 * int(re.search(r"\d+", date_str).group())
        )
    return now


def create_paginated_job_message(jobs, page):
    start_index = page * JOBS_PER_PAGE
    end_index = start_index + JOBS_PER_PAGE
    total_pages = (len(jobs) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE

    message_text = f"<b>Displaying page {page + 1} of {total_pages}</b>\n\n"
    for job in jobs[start_index:end_index]:
        # Use HTML formatting and escape special characters
        title = html.escape(job["Title"])
        company = html.escape(job["Company"]) if job["Company"] else ""
        location = html.escape(job["Location"]) if job["Location"] else ""
        date_posted = html.escape(job["Date Posted"]) if job["Date Posted"] and job["Date Posted"] != "N/A" else ""
        # Escape URL for safe use in HTML href attribute
        job_link = html.escape(job["Link"], quote=True)

        if company and location:
            company_line = f"<i>{company}</i> - {location}"
        elif company:
            company_line = f"<i>{company}</i>"
        elif location:
            company_line = location
        else:
            company_line = ""

        message_text += f"<b>{title}</b>\n"
        message_text += f"{company_line}\n"
        if date_posted:
            message_text += f"Posted: {date_posted}\n"
        message_text += f'<a href="{job_link}">View Job</a>\n\n'

    if not jobs[start_index:end_index]:
        return "No jobs to display.", None

    row = []
    if page > 0:
        row.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page - 1}")
        )

    if total_pages > 1:
        row.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}",
                                 callback_data="ignore")
        )

    if end_index < len(jobs):
        row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"page_{page + 1}")
        )

    buttons = [row, [InlineKeyboardButton("❌ Close", callback_data="close")]]
    return message_text, InlineKeyboardMarkup(buttons)


def get_job_description(job_link):
    """Fetch job description from LinkedIn job page with rate limiting."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/96.0.4664.93 Safari/537.36",
        }

        # Add delay to avoid rate limiting
        time.sleep(2)

        response = requests.get(job_link, headers=headers, timeout=15)

        # Handle rate limiting gracefully
        if response.status_code == 429:
            logger.warning(f"Rate limited for {job_link}, using title only")
            return "Description unavailable due to rate limiting"

        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")

        # Try multiple selectors for job description
        description_selectors = [
            ".show-more-less-html__markup",
            ".description__text",
            '[data-automation-id="jobPostingDescription"]',
            ".jobs-description-content__text",
        ]

        for selector in description_selectors:
            desc_elem = soup.select_one(selector)
            if desc_elem:
                return desc_elem.get_text(strip=True)[:1000]

        return "No description available"
    except Exception as e:
        logger.warning(f"Failed to fetch job description for {job_link}: {e}")
        return "No description available"


# --- Enhanced Job Relevance Engine ---
class JobRelevanceEngine:
    def __init__(self):
        # Weightings for each factor (adjust as needed)
        self.weights = {
            "title_relevance": 0.40,      # Job title match (highest priority)
            "company_relevance": 0.15,    # Company relevance
            "recency_boost": 0.15,        # Recent posting bonus
            "location_match": 0.10,       # Location relevance
            "exclusion_penalty": 0.20,     # Penalty for excluded terms
        }
        # You can optionally add exclusion terms here
        self.exclusion_terms = set()
        # self.exclusion_terms.update(['intern', 'sales', 'marketing'])

    def calculate_relevance_score(self, job, search_query):
        """Calculate comprehensive job relevance score (0-1)"""
        total_score = 0.0

        # Title relevance (case-insensitive, word boundary preferred)
        title_score = self._calculate_field_relevance(
            job["Title"], search_query
        )
        total_score += title_score * self.weights["title_relevance"]

        # Company relevance
        company_score = self._calculate_field_relevance(
            job["Company"], search_query
        )
        total_score += company_score * self.weights["company_relevance"]

        # Recency boost (newer jobs score higher)
        recency_score = self._calculate_recency_score(job["Date Posted"])
        total_score += recency_score * self.weights["recency_boost"]

        # Location match (case-insensitive, simple contains)
        location_score = self._calculate_field_relevance(
            job["Location"], search_query
        )
        total_score += location_score * self.weights["location_match"]

        # Exclusion penalty (optional)
        if self.exclusion_terms:
            exclusion_penalty = self._calculate_exclusion_penalty(job)
            total_score -= (
                exclusion_penalty * self.weights["exclusion_penalty"]
            )

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
            if re.search(r"\b" + re.escape(word) + r"\b", field_lower):
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
            if hours_old <= 168:  # 1 week
                return 0.8
            if hours_old <= 720:  # 1 month
                return 0.5
            return 0.1
        except Exception:
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
        """Dynamically identify job type vs domain terms from job data"""
        if self.analyzed:
            return

        import re
        from collections import Counter

        # Extract all terms from job titles
        all_terms = []
        job_titles = [job["Title"] for job in jobs]

        for title in job_titles:
            # Extract meaningful words (2+ characters, alphabetic)
            terms = re.findall(r"\b[a-zA-Z]{2,}\b", title.lower())
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
        logger.info(
            f"Identified {len(self.job_type_indicators)} job type terms, "
            f"{len(self.domain_terms)} domain terms"
        )

    def classify_query_terms(self, query, jobs):
        """Classify query terms as job type vs domain terms"""
        if not self.analyzed:
            self.analyze_job_corpus(jobs)

        query_words = query.lower().split()

        job_type_terms = [
            w for w in query_words if w in self.job_type_indicators
        ]
        domain_terms = [w for w in query_words if w in self.domain_terms]

        return {
            "job_type_terms": job_type_terms,
            "domain_terms": domain_terms,
            "has_domain_specificity": len(domain_terms) > 0,
            "job_type_ratio": len(job_type_terms) / len(query_words)
            if query_words else 0,
        }


class TFIDFTermClassifier:
    def __init__(self):
        if SKLEARN_AVAILABLE:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.vectorizer = TfidfVectorizer(
                ngram_range=(1, 1),
                max_features=1000,
                stop_words="english",
            )
        else:
            self.vectorizer = None
        self.job_type_threshold = 0.1  # Lower TF-IDF = more common/generic
        self.domain_threshold = 0.3    # Higher TF-IDF = more specific

    def analyze_terms(self, jobs):
        """Use TF-IDF to classify terms by importance"""
        if not SKLEARN_AVAILABLE or not self.vectorizer:
            return {"job_type_terms": set(), "domain_terms": set()}

        job_titles = [job["Title"] for job in jobs]

        try:
            # Fit TF-IDF on job titles
            tfidf_matrix = self.vectorizer.fit_transform(job_titles)
            feature_names = self.vectorizer.get_feature_names_out()

            # Calculate average TF-IDF score for each term
            if np is not None:
                mean_scores = np.array(tfidf_matrix.mean(axis=0)).flatten()
            else:
                # Fallback without numpy
                mean_scores = [
                    tfidf_matrix[:, i].mean()
                    for i in range(tfidf_matrix.shape[1])
                ]

            # Classify terms based on TF-IDF scores
            job_type_terms = []
            domain_terms = []

            for i, term in enumerate(feature_names):
                score = mean_scores[i]

                if score <= self.job_type_threshold:
                    # Low TF-IDF = common = job type indicator
                    job_type_terms.append(term)
                elif score >= self.domain_threshold:
                    # High TF-IDF = specific = domain term
                    domain_terms.append(term)

            return {
                "job_type_terms": set(job_type_terms),
                "domain_terms": set(domain_terms),
            }
        except Exception as e:
            logger.warning(f"TF-IDF analysis failed: {e}")
            return {"job_type_terms": set(), "domain_terms": set()}

    def classify_query(self, query, jobs):
        """Classify query terms using TF-IDF analysis"""
        term_classification = self.analyze_terms(jobs)
        query_words = set(query.lower().split())

        job_type_matches = query_words.intersection(
            term_classification["job_type_terms"]
        )
        domain_matches = query_words.intersection(
            term_classification["domain_terms"]
        )

        return {
            "job_type_terms": list(job_type_matches),
            "domain_terms": list(domain_matches),
            "has_domain_specificity": len(domain_matches) > 0,
        }


class CorpusOnlyClassifier:
    """100% Pattern-Free Classifier - learns everything from data"""

    def __init__(self):
        self.learned_morphemes = {"job_type": set(), "domain": set()}
        self.analyzed = False

    def learn_morphemes_from_corpus(self, jobs):
        """Learn morphological patterns directly from job data"""
        if self.analyzed:
            return

        from collections import Counter

        # Extract all words from job titles
        job_titles = [job["Title"] for job in jobs]
        all_words = []
        for title in job_titles:
            words = re.findall(r"\b[a-zA-Z]{3,}\b", title.lower())
            all_words.extend(words)

        word_counter = Counter(all_words)
        total_words = len(all_words)

        # Learn suffix patterns from high-frequency words
        suffix_counter = Counter()
        prefix_counter = Counter()

        for word, count in word_counter.items():
            if count >= 3 and len(word) >= 4:
                # Learn suffixes (last 2-3 characters)
                suffix_counter[word[-2:]] += count
                suffix_counter[word[-3:]] += count

                # Learn prefixes (first 3 characters)
                prefix_counter[word[:3]] += count

        # Identify frequent morphemes (appearing in 1%+ of corpus)
        min_frequency = total_words * 0.01

        frequent_suffixes = {
            suffix for suffix, count in suffix_counter.items()
            if count >= min_frequency and len(suffix) >= 2
        }
        frequent_prefixes = {
            prefix for prefix, count in prefix_counter.items()
            if count >= min_frequency
        }

        self.learned_morphemes["job_type"] = frequent_suffixes.union(
            frequent_prefixes
        )
        self.analyzed = True

        logger.info(
            f"Learned {len(self.learned_morphemes['job_type'])} "
            f"morphological patterns from corpus"
        )

    def classify_terms_by_learned_patterns(self, query_words, jobs):
        """Classify terms using patterns learned from the job corpus"""
        if not self.analyzed:
            self.learn_morphemes_from_corpus(jobs)

        job_type_terms = []
        domain_terms = []

        for word in query_words:
            word_lower = word.lower()

            # Check against learned patterns
            has_job_type_morpheme = any(
                word_lower.endswith(suffix) or word_lower.startswith(prefix)
                for suffix in self.learned_morphemes["job_type"]
                for prefix in self.learned_morphemes["job_type"]
            )

            if has_job_type_morpheme:
                job_type_terms.append(word_lower)
            else:
                # Everything else is potentially domain-specific
                domain_terms.append(word_lower)

        return {
            "job_type_terms": job_type_terms,
            "domain_terms": domain_terms,
            "has_domain_specificity": len(domain_terms) > 0,
        }


class PureMathematicalClassifier:
    """Ultra-Pure Classifier using only mathematical/statistical methods"""

    def __init__(self):
        pass

    def classify_by_mathematical_properties(self, query_words, jobs):
        """Classify using only mathematical properties of words in corpus."""
        from collections import Counter

        # Get all job titles
        job_titles = [job["Title"] for job in jobs]
        all_corpus_words = []
        for title in job_titles:
            words = re.findall(r"\b[a-zA-Z]{2,}\b", title.lower())
            all_corpus_words.extend(words)

        corpus_counter = Counter(all_corpus_words)
        total_corpus_words = len(all_corpus_words)

        job_type_terms = []
        domain_terms = []

        for word in query_words:
            word_lower = word.lower()
            word_frequency = corpus_counter.get(word_lower, 0)
            word_probability = (
                word_frequency / total_corpus_words
                if total_corpus_words > 0 else 0
            )

            # Mathematical classification based on statistical properties only:

            # 1. Frequency-based classification
            if word_probability > 0.02 or len(word) <= 4:
                job_type_terms.append(word_lower)

            # 3. Character distribution analysis
            else:
                # Words with more vowels tend to be more generic/job-type
                vowel_ratio = (
                    sum(1 for char in word_lower if char in "aeiou")
                    / len(word_lower)
                )
                if vowel_ratio > 0.4:
                    job_type_terms.append(word_lower)
                else:
                    domain_terms.append(word_lower)

        return {
            "job_type_terms": job_type_terms,
            "domain_terms": domain_terms,
            "has_domain_specificity": len(domain_terms) > 0,
        }


class UltraPureDynamicClassifier:
    """Completely pattern-free classifier using only mathematical and
    corpus-based methods"""

    def __init__(self):
        self.corpus_classifier = DynamicTermClassifier()
        self.tfidf_classifier = TFIDFTermClassifier()
        self.mathematical_classifier = PureMathematicalClassifier()

    def classify_query_comprehensively(self, query, jobs):
        """Use only mathematical and corpus-based methods"""
        # Method 1: Corpus-based analysis
        corpus_result = self.corpus_classifier.classify_query_terms(
            query, jobs
        )

        # Method 2: TF-IDF based analysis
        tfidf_result = self.tfidf_classifier.classify_query(query, jobs)

        # Method 3: Pure mathematical analysis
        query_words = query.lower().split()
        math_result = self.mathematical_classifier. \
            classify_by_mathematical_properties(
                query_words, jobs
            )

        # Combine results with weighted voting (no linguistic patterns)
        job_type_votes = {}
        domain_votes = {}

        # Count votes from each method
        for term in query_words:
            job_type_votes[term] = 0
            domain_votes[term] = 0

            # Corpus method vote
            if term in corpus_result["job_type_terms"]:
                job_type_votes[term] += 1
            if term in corpus_result["domain_terms"]:
                domain_votes[term] += 1

            # TF-IDF method vote
            if term in tfidf_result["job_type_terms"]:
                job_type_votes[term] += 1
            if term in tfidf_result["domain_terms"]:
                domain_votes[term] += 1

            # Mathematical method vote
            if term in math_result["job_type_terms"]:
                job_type_votes[term] += 1
            if term in math_result["domain_terms"]:
                domain_votes[term] += 1

        # Final classification based on majority vote
        final_job_type_terms = [
            term for term, votes in job_type_votes.items() if votes >= 2
        ]
        final_domain_terms = [
            term for term, votes in domain_votes.items() if votes >= 2
        ]
        job_type_confidence = (
            sum(job_type_votes.values()) / (len(query_words) * 3)
            if query_words else 0
        )
        domain_confidence = (
            sum(domain_votes.values()) / (len(query_words) * 3)
            if query_words else 0
        )

        return {
            "job_type_terms": final_job_type_terms,
            "domain_terms": final_domain_terms,
            "has_domain_specificity": len(final_domain_terms) > 0,
            "confidence_scores": {
                "job_type_confidence": job_type_confidence,
                "domain_confidence": domain_confidence,
            },
            "method": "ultra_pure_mathematical",
        }


# Global model instance to avoid reloading
_global_jobbert_model = None
_model_load_attempted = False
_global_adaptive_matcher = None
_model_last_used = None
_model_usage_count = 0

# Memory management constants
MAX_MEMORY_MB = 2500  # Maximum memory before cleanup
MODEL_IDLE_TIMEOUT = 600  # 10 minutes of inactivity before unloading (optimized for faster memory release)
MAX_MODEL_USES = 200  # Unload model after this many uses to prevent memory fragmentation


def get_memory_usage():
    """Get current memory usage in MB"""
    try:
        import psutil
        import os
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    except Exception:
        return 0

def get_detailed_memory_info():
    """Get detailed memory information for monitoring"""
    try:
        import psutil
        import os
        
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        memory_percent = process.memory_percent()
        
        # System memory info
        system_memory = psutil.virtual_memory()
        
        return {
            'process_rss_mb': memory_info.rss / 1024 / 1024,
            'process_vms_mb': memory_info.vms / 1024 / 1024,
            'process_percent': memory_percent,
            'system_total_mb': system_memory.total / 1024 / 1024,
            'system_available_mb': system_memory.available / 1024 / 1024,
            'system_used_percent': system_memory.percent,
            'num_threads': process.num_threads(),
            'cpu_percent': process.cpu_percent()
        }
    except Exception as e:
        logger.warning(f"Failed to get detailed memory info: {e}")
        return {
            'process_rss_mb': get_memory_usage(),
            'error': str(e)
        }

def get_system_resources():
    """Get comprehensive system resource snapshot for debugging hangs"""
    try:
        import psutil
        import os
        import threading

        process = psutil.Process(os.getpid())

        # Memory info
        mem_info = process.memory_info()
        system_mem = psutil.virtual_memory()

        # CPU info - use background tracker for accurate readings
        cpu_data = cpu_tracker.get_cpu()
        cpu_percent = cpu_data['process']
        system_cpu = cpu_data['system']

        # Thread info
        thread_count = process.num_threads()
        active_threads = threading.active_count()

        # Disk I/O
        try:
            io_counters = process.io_counters()
            disk_read_mb = io_counters.read_bytes / 1024 / 1024
            disk_write_mb = io_counters.write_bytes / 1024 / 1024
        except:
            disk_read_mb = 0
            disk_write_mb = 0

        # Network (if available)
        try:
            net_io = psutil.net_io_counters()
            net_sent_mb = net_io.bytes_sent / 1024 / 1024
            net_recv_mb = net_io.bytes_recv / 1024 / 1024
        except:
            net_sent_mb = 0
            net_recv_mb = 0

        return {
            'mem_mb': mem_info.rss / 1024 / 1024,
            'mem_pct': process.memory_percent(),
            'sys_mem_avail_mb': system_mem.available / 1024 / 1024,
            'sys_mem_pct': system_mem.percent,
            'cpu_pct': cpu_percent,
            'sys_cpu_pct': system_cpu,
            'threads': thread_count,
            'active_threads': active_threads,
            'disk_read_mb': disk_read_mb,
            'disk_write_mb': disk_write_mb,
            'net_sent_mb': net_sent_mb,
            'net_recv_mb': net_recv_mb,
        }
    except Exception as e:
        return {'mem_mb': get_memory_usage(), 'error': str(e)}

def check_memory_health():
    """Check memory health and return status with recommendations"""
    memory_info = get_detailed_memory_info()
    current_memory = memory_info.get('process_rss_mb', 0)

    status = "HEALTHY"
    recommendations = []

    # Check process memory usage
    if current_memory > MAX_MEMORY_MB * 0.9:
        status = "CRITICAL"
        recommendations.append("Immediate model unload required")
        recommendations.append("Force garbage collection")
    elif current_memory > MAX_MEMORY_MB * 0.8:
        status = "WARNING"
        recommendations.append("Consider unloading model")
        recommendations.append("Schedule memory cleanup")
    elif current_memory > MAX_MEMORY_MB * 0.7:
        status = "CAUTION"
        recommendations.append("Monitor closely")
    
    # Check system memory
    system_used = memory_info.get('system_used_percent', 0)
    if system_used > 90:
        status = "CRITICAL" if status != "CRITICAL" else status
        recommendations.append("System memory critically low")
    elif system_used > 80:
        if status == "HEALTHY":
            status = "WARNING"
        recommendations.append("System memory high")
    
    return {
        'status': status,
        'memory_info': memory_info,
        'current_memory_mb': current_memory,
        'memory_usage_percent': (current_memory / MAX_MEMORY_MB) * 100,
        'recommendations': recommendations
    }


def force_memory_cleanup():
    """Aggressive memory cleanup"""
    import gc
    import torch
    
    # Clear GPU cache if available
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("🧹 Cleared GPU cache")
    except Exception:
        pass
    
    # Force garbage collection multiple times
    for _ in range(3):
        gc.collect()
    
    logger.info(f"🧹 Memory cleanup completed. Current usage: {get_memory_usage():.1f} MB")


def unload_jobbert_model():
    """Unload the JobBERT model to free memory with enhanced error handling"""
    global _global_jobbert_model, _model_load_attempted, _model_last_used, _model_usage_count
    
    lock_acquired = False
    try:
        # Try to acquire lock with timeout
        lock_acquired = model_lock.acquire(timeout=30)
        
        if not lock_acquired:
            logger.error("⚠️ Failed to acquire model_lock for unloading - potential deadlock")
            # Try force release if deadlocked
            lock_status = model_lock.get_status()
            if lock_status.get('locked') and lock_status.get('held_duration', 0) > 60:
                logger.warning("🆘 Forcing lock release to unload model")
                model_lock.force_release()
                lock_acquired = model_lock.acquire(timeout=10)
            
            if not lock_acquired:
                logger.error("❌ Cannot unload model - lock unavailable")
                return
        
        if _global_jobbert_model is not None:
            logger.info("🗑️ Unloading JobBERT model to free memory...")
            try:
                del _global_jobbert_model
                _global_jobbert_model = None
                _model_load_attempted = False
                _model_last_used = None
                _model_usage_count = 0
                logger.info("✅ Model deleted from memory")
            except Exception as del_error:
                logger.error(f"❌ Error deleting model: {del_error}")
                _global_jobbert_model = None  # Set to None anyway
            
            try:
                # Don't acquire memory_cleanup_lock here - we already hold model_lock
                # This prevents deadlock with threads holding memory_cleanup_lock waiting for model_lock
                force_memory_cleanup()
                logger.info(f"✅ JobBERT model unloaded. Memory usage: {get_memory_usage():.1f} MB")
            except Exception as cleanup_error:
                logger.error(f"⚠️ Cleanup after unload failed: {cleanup_error}")
                
    except Exception as e:
        logger.error(f"❌ Error in unload_jobbert_model: {e}", exc_info=True)
    finally:
        # Always release the lock if we acquired it
        if lock_acquired:
            try:
                model_lock.release()
            except Exception as release_error:
                logger.error(f"❌ Error releasing lock in unload: {release_error}")


def should_unload_model():
    """Check if model should be unloaded based on memory and usage patterns (optimized for efficiency)"""
    global _model_last_used, _model_usage_count
    
    current_memory = get_memory_usage()
    memory_usage_ratio = current_memory / MAX_MEMORY_MB
    
    # 1. Critical memory situation - immediate unload required
    if current_memory > MAX_MEMORY_MB:
        logger.warning(f"🚨 Critical memory usage: {current_memory:.1f} MB > {MAX_MEMORY_MB} MB")
        return True
    
    # 2. Usage-based unload to prevent memory fragmentation
    if memory_usage_ratio > 0.8:  # Over 80% memory usage
        dynamic_max_uses = max(50, int(MAX_MODEL_USES * (1 - memory_usage_ratio)))
        if _model_usage_count >= dynamic_max_uses:
            logger.info(f"🔄 Dynamic unload: {_model_usage_count} uses at {memory_usage_ratio:.1%} memory")
            return True
    elif _model_usage_count > MAX_MODEL_USES:
        logger.info(f"🔄 Model used {_model_usage_count} times, unloading to prevent fragmentation")
        return True
    
    # 3. Idle timeout - ONLY unload if there's memory pressure (efficient resource management)
    if _model_last_used:
        import time
        idle_time = time.time() - _model_last_used
        if idle_time > MODEL_IDLE_TIMEOUT:
            # Smart logic: Only unload idle model if we need the memory
            memory_pressure_threshold = 0.7  # 70% memory usage indicates pressure
            is_memory_pressure = memory_usage_ratio > memory_pressure_threshold
            
            if is_memory_pressure:
                logger.info(f"💤 Model idle for {idle_time/60:.1f} minutes under memory pressure ({memory_usage_ratio:.1%}), unloading")
                return True
            else:
                # Model is idle but memory is healthy - keep it loaded for performance
                logger.debug(f"⏰ Model idle for {idle_time/60:.1f} minutes but memory healthy ({memory_usage_ratio:.1%}), keeping loaded")
                return False
    
    return False


def get_jobbert_model():
    """Get or load the JobBERT model with intelligent memory management and deadlock recovery"""
    global _global_jobbert_model, _model_load_attempted, _model_last_used, _model_usage_count
    import time
    import threading

    current_thread = threading.current_thread().name

    # FAST PATH: If model is already loaded, return immediately without lock contention
    # This dramatically reduces lock contention when model is already available
    if _global_jobbert_model is not None and not should_unload_model():
        _model_last_used = time.time()
        _model_usage_count += 1
        if _model_usage_count % 10 == 0:
            logger.info(f"📊 Model usage: {_model_usage_count} times, Memory: {get_memory_usage():.1f} MB")
        logger.debug(f"🚀 [FAST PATH] Model already loaded, returning immediately for thread: {current_thread}")
        return _global_jobbert_model

    # SLOW PATH: Need to load/reload the model - acquire lock
    logger.info(f"🔍 [DIAG] get_jobbert_model() slow path called by thread: {current_thread}")
    logger.info(f"🔍 [DIAG] Current model state: _global_jobbert_model={'loaded' if _global_jobbert_model else 'None'}, _model_load_attempted={_model_load_attempted}")

    # Check lock status before attempting to acquire
    lock_status = model_lock.get_status()
    if lock_status.get('locked') and lock_status.get('held_duration', 0) > 300:
        logger.critical(f"🚨 DEADLOCK DETECTED: model_lock held by {lock_status['holder']} for {lock_status['held_duration']:.1f}s")
        logger.critical(f"🆘 Attempting automatic deadlock recovery...")
        try:
            model_lock.force_release()
            logger.info(f"✅ Deadlock recovery successful - lock forcibly released")
        except Exception as e:
            logger.error(f"❌ Deadlock recovery failed: {e}")
            return None

    logger.info(f"🔍 [DIAG] Attempting to acquire model_lock...")
    lock_start_time = time.time()

    # Try to acquire lock with timeout and retry logic to prevent infinite hangs
    max_retries = 2
    retry_count = 0
    lock_acquired = False

    while retry_count < max_retries and not lock_acquired:
        lock_acquired = model_lock.acquire(timeout=60)  # Reduced from 90s to 60s per attempt

        if not lock_acquired:
            retry_count += 1
            elapsed = time.time() - lock_start_time
            logger.warning(f"⚠️ Failed to acquire model_lock (attempt {retry_count}/{max_retries}, elapsed={elapsed:.1f}s)")

            if retry_count < max_retries:
                logger.info(f"🔄 Retrying lock acquisition in 2 seconds...")
                time.sleep(2)  # Reduced from 3s to 2s
            else:
                logger.error(f"🚨 [CRITICAL] Failed to acquire model_lock after {max_retries} attempts ({elapsed:.1f}s total)")
                logger.error(f"🔍 [DIAG] Possible deadlock detected. Attempting force recovery...")
                
                # Last resort: force release the lock
                try:
                    model_lock.force_release()
                    logger.warning(f"🆘 Lock forcibly released. Attempting final acquisition...")
                    lock_acquired = model_lock.acquire(timeout=30)
                    if not lock_acquired:
                        logger.critical(f"❌ Final lock acquisition failed even after force release")
                        return None
                    logger.info(f"✅ Successfully acquired lock after force release")
                except Exception as e:
                    logger.critical(f"❌ Force release failed: {e}")
                    return None

    try:
        lock_acquired_time = time.time()
        logger.info(f"🔍 [DIAG] model_lock acquired in {lock_acquired_time - lock_start_time:.3f} seconds")

        # Double-check: another thread may have loaded the model while we waited for the lock
        if _global_jobbert_model is not None and not should_unload_model():
            logger.info(f"🔍 [DIAG] Model was loaded by another thread while waiting, returning immediately")
            _model_last_used = time.time()
            _model_usage_count += 1
            return _global_jobbert_model

        # Check if we should unload the model first
        if _global_jobbert_model is not None and should_unload_model():
            logger.info(f"🔍 [DIAG] Model needs to be unloaded")
            unload_jobbert_model()

        # Load model if not available
        if _global_jobbert_model is None:
            logger.info(f"🔍 [DIAG] Model is None, resetting _model_load_attempted")
            _model_load_attempted = False  # Reset to allow reloading

        if not _model_load_attempted:
            logger.info(f"🔍 [DIAG] Starting model load attempt...")
            _model_load_attempted = True

            if not SENTENCE_TRANSFORMERS_AVAILABLE:
                logger.warning("❌ Sentence transformers not available")
                logger.error(f"🔍 [DIAG] CRITICAL: sentence-transformers library not available!")
                return None

            try:
                # Pre-cleanup before loading
                current_memory = get_memory_usage()
                logger.info(f"💾 Current memory usage before loading: {current_memory:.1f} MB")
                logger.info(f"🔍 [DIAG] Memory check complete, current_memory={current_memory:.1f} MB")

                if current_memory > 800:  # Cleanup if using more than 800MB
                    logger.info(f"🔍 [DIAG] Memory > 800MB, running cleanup WITHOUT acquiring memory_cleanup_lock (already have model_lock)")
                    cleanup_start = time.time()
                    # Don't acquire memory_cleanup_lock here - we already hold model_lock
                    # This prevents deadlock with threads holding memory_cleanup_lock waiting for model_lock
                    force_memory_cleanup()
                    logger.info(f"🔍 [DIAG] Memory cleanup completed in {time.time() - cleanup_start:.3f} seconds")
                    current_memory = get_memory_usage()  # Re-check after cleanup

                logger.info("🤖 Loading mpnet model (this may take a moment)...")
                resources_before_model = get_system_resources()
                logger.info(f"🔍 [STAGE] MODEL_LOAD_START | model=mpnet")
                logger.info(f"🔍 [RESOURCE] BEFORE_MODEL | mem={resources_before_model['mem_mb']:.1f}MB | cpu={resources_before_model['cpu_pct']:.1f}% | sys_mem_avail={resources_before_model['sys_mem_avail_mb']:.1f}MB | disk_read={resources_before_model['disk_read_mb']:.1f}MB")

                # Check if there's enough available memory to load model (needs ~1GB)
                sys_mem_avail = resources_before_model.get('sys_mem_avail_mb', 1000)
                if sys_mem_avail < 500:  # Less than 500MB available
                    logger.error(f"🚨 [CRITICAL] Insufficient memory to load model! Available: {sys_mem_avail:.1f}MB, Need: ~1000MB")
                    logger.warning("⚠️ System may hang or swap heavily. Attempting load anyway with swap...")
                    # Continue anyway since we now have swap space
                logger.info(f"🔍 [DIAG] About to call SentenceTransformer('TechWolf/JobBERT-v3')...")
                model_load_start = time.time()

                # JobBERT-v3 is the official TalentCLEF 2025 benchmark winner for
                # cross-lingual job title matching. Multilingual (EN/DE/ES/ZH),
                # trained on 21M job-title/skill pairs via contrastive learning.
                # Benchmarked vs 6 alternatives: highest discrimination (gap=0.715)
                # vs mpnet 0.568, bge-m3 0.287, e5-large 0.079. General-purpose
                # models lump all job titles as similar — only domain training works.
                _global_jobbert_model = SentenceTransformer("TechWolf/JobBERT-v3")

                model_load_end = time.time()
                resources_after_model = get_system_resources()
                logger.info(f"🔍 [STAGE] MODEL_LOAD_COMPLETE | model=mpnet | time={model_load_end - model_load_start:.3f}s")
                logger.info(f"🔍 [RESOURCE] AFTER_MODEL | mem={resources_after_model['mem_mb']:.1f}MB (+{resources_after_model['mem_mb']-resources_before_model['mem_mb']:.1f}) | cpu={resources_after_model['cpu_pct']:.1f}% | disk_read={resources_after_model['disk_read_mb']:.1f}MB (+{resources_after_model['disk_read_mb']-resources_before_model['disk_read_mb']:.1f})")
                logger.info(f"✅ mpnet loaded successfully in {model_load_end - model_load_start:.3f} seconds")
                logger.info(f"🔍 [DIAG] Model loaded successfully, type: {type(_global_jobbert_model)}")

            except Exception as e:
                logger.error(f"❌ Failed to load mpnet: {e}")
                logger.error(f"🔍 [DIAG] Exception details:", exc_info=True)
                try:
                    logger.info("🔄 Fallback: Loading all-MiniLM-L6-v2...")
                    logger.info(f"🔍 [DIAG] Attempting fallback model load...")
                    fallback_start = time.time()

                    _global_jobbert_model = SentenceTransformer("all-MiniLM-L6-v2")

                    logger.info(f"✅ Fallback model loaded successfully in {time.time() - fallback_start:.3f} seconds")
                    logger.info(f"🔍 [DIAG] Fallback model loaded successfully")
                except Exception as e2:
                    logger.error(f"❌ Failed to load fallback model: {e2}")
                    logger.error(f"🔍 [DIAG] Fallback model also failed:", exc_info=True)
                    _global_jobbert_model = None
                    return None
        else:
            logger.info(f"🔍 [DIAG] Model already loaded, skipping load attempt")

        # Update usage tracking
        if _global_jobbert_model is not None:
            _model_last_used = time.time()
            _model_usage_count += 1
            logger.info(f"🔍 [DIAG] Model usage updated: count={_model_usage_count}, last_used={_model_last_used}")

            # Log memory usage periodically
            if _model_usage_count % 10 == 0:
                logger.info(f"📊 Model usage: {_model_usage_count} times, Memory: {get_memory_usage():.1f} MB")
        else:
            logger.error(f"🔍 [DIAG] WARNING: Returning None - model failed to load!")

        logger.info(f"🔍 [DIAG] Exiting get_jobbert_model(), returning {'model' if _global_jobbert_model else 'None'}")
        return _global_jobbert_model

    finally:
        # CRITICAL: Always release the lock, even if an exception occurs
        model_lock.release()
        logger.info(f"🔍 [DIAG] model_lock released successfully")


def get_adaptive_matcher():
    """Get or create the AdaptiveJobBERTMatcher singleton"""
    global _global_adaptive_matcher

    if _global_adaptive_matcher is None:
        logger.info("🔧 Creating AdaptiveJobBERTMatcher instance...")
        _global_adaptive_matcher = AdaptiveJobBERTMatcher()
        logger.info("✅ AdaptiveJobBERTMatcher created successfully")

    return _global_adaptive_matcher


class AdaptiveJobBERTMatcher:
    def __init__(self):
        self.dynamic_classifier = UltraPureDynamicClassifier()
        # Initialize the model for domain coherence calculations
        self.model = None
        self._load_model()

    def _load_model(self):
        """Load JobBERT model with proper error handling"""
        try:
            self.model = get_jobbert_model()
            if self.model:
                logger.debug("✅ AdaptiveJobBERTMatcher: Model loaded successfully")
            else:
                logger.warning("⚠️ AdaptiveJobBERTMatcher: Model not available, domain coherence will use fallback")
        except Exception as e:
            logger.warning(f"❌ AdaptiveJobBERTMatcher: Failed to load model: {e}")
            self.model = None

    def _ensure_model_loaded(self):
        """Ensure model is loaded and available"""
        if self.model is None:
            logger.debug("🔄 AdaptiveJobBERTMatcher: Attempting to reload model")
            self._load_model()
        # Also check if the global model changed (memory management might have reloaded it)
        global _global_jobbert_model
        if self.model is not _global_jobbert_model and _global_jobbert_model is not None:
            logger.debug("🔄 AdaptiveJobBERTMatcher: Updating model reference to global instance")
            self.model = _global_jobbert_model
        return self.model is not None

    def calculate_adaptive_relevance(self, jobs, query):
        """Use JobBERT's natural understanding to filter jobs adaptively with memory management"""
        import time
        logger.info(f"🔍 [DIAG] calculate_adaptive_relevance() called with {len(jobs)} jobs, query='{query}'")

        # Get fresh model instance (handles memory management internally)
        logger.info(f"🔍 [DIAG] Calling get_jobbert_model()...")
        model_fetch_start = time.time()
        model = get_jobbert_model()
        logger.info(f"🔍 [DIAG] get_jobbert_model() returned in {time.time() - model_fetch_start:.3f} seconds, model={'loaded' if model else 'None'}")

        if not model:
            logger.error("🚨 [CRITICAL] JobBERT model unavailable - likely due to deadlock!")
            logger.error(f"🔍 [DIAG] Model is None after lock timeout. AI filtering cannot proceed.")
            logger.error(f"💡 ACTION REQUIRED: Restart the bot to clear the deadlock and enable AI filtering.")
            logger.error(f"⚠️ Returning empty results - AI filtering is REQUIRED, not using basic fallback.")
            # Return empty to signal failure - AI filtering is mandatory
            return []

        job_embeddings = None
        query_embedding = None
        similarities = None

        try:
            memory_before = get_memory_usage()
            logger.debug(f"🤖 Processing {len(jobs)} jobs with JobBERT... Memory: {memory_before:.1f} MB")
            logger.info(f"🔍 [DIAG] Memory before encoding: {memory_before:.1f} MB")

            # Step 1: Encode everything with JobBERT
            # JobBERT-v3 was trained on pure job titles (mean 10.56 tokens),
            # NOT on title+company concatenations. Per TechWolf's docs, we
            # encode titles only. Test showed +11.6% discrimination gap and
            # +13% positive-match score vs the old "title + company" format.
            logger.info(f"🔍 [DIAG] Creating job_texts list from {len(jobs)} jobs...")
            job_texts = [job['Title'] for job in jobs]
            logger.info(f"🔍 [DIAG] job_texts created, length={len(job_texts)}")

            # Batch processing with error handling and memory monitoring
            try:
                # Monitor memory during encoding
                if memory_before > MAX_MEMORY_MB * 0.8:  # 80% of max memory
                    logger.warning(f"⚠️ High memory before encoding: {memory_before:.1f} MB")
                    logger.info(f"🔍 [DIAG] High memory detected, running cleanup...")
                    force_memory_cleanup()

                resources_before_encode = get_system_resources()
                logger.info(f"🔍 [STAGE] ENCODING_JOBS_START | jobs={len(job_texts)}")
                logger.info(f"🔍 [RESOURCE] BEFORE_ENCODE | mem={resources_before_encode['mem_mb']:.1f}MB | cpu={resources_before_encode['cpu_pct']:.1f}% | threads={resources_before_encode['threads']}")
                logger.info(f"🔍 [DIAG] About to encode {len(job_texts)} job texts with model.encode()...")
                encode_start = time.time()
                job_embeddings = model.encode(
                    job_texts, show_progress_bar=False, convert_to_tensor=False
                )
                resources_after_encode = get_system_resources()
                logger.info(f"🔍 [STAGE] ENCODING_JOBS_COMPLETE | time={time.time()-encode_start:.3f}s | shape={getattr(job_embeddings, 'shape', 'N/A')}")
                logger.info(f"🔍 [RESOURCE] AFTER_ENCODE | mem={resources_after_encode['mem_mb']:.1f}MB (+{resources_after_encode['mem_mb']-resources_before_encode['mem_mb']:.1f}) | cpu={resources_after_encode['cpu_pct']:.1f}%")
                logger.info(f"🔍 [DIAG] Job embeddings created in {time.time() - encode_start:.3f} seconds, shape={getattr(job_embeddings, 'shape', 'N/A')}")

                logger.info(f"🔍 [STAGE] ENCODING_QUERY_START")
                logger.info(f"🔍 [DIAG] About to encode query with model.encode()...")
                query_encode_start = time.time()
                query_embedding = model.encode(
                    [query], show_progress_bar=False, convert_to_tensor=False
                )
                logger.info(f"🔍 [STAGE] ENCODING_QUERY_COMPLETE | time={time.time()-query_encode_start:.3f}s")
                logger.info(f"🔍 [DIAG] Query embedding created in {time.time() - query_encode_start:.3f} seconds, shape={getattr(query_embedding, 'shape', 'N/A')}")
                
                memory_after_encoding = get_memory_usage()
                logger.debug(f"📊 Memory after encoding: {memory_after_encoding:.1f} MB (+{memory_after_encoding - memory_before:.1f} MB)")
                
            except Exception as e:
                logger.warning(f"❌ JobBERT encoding failed: {e}")
                return self._fallback_basic_filter(jobs, query)

            # Step 2: Check if we have embeddings before calculating similarities
            if len(job_embeddings) == 0 or (hasattr(job_embeddings, 'shape') and job_embeddings.shape[0] == 0):
                logger.info(f"ℹ️ No job embeddings to process (empty array after filtering). Returning 0 jobs.")
                return []
            
            # Step 3: Calculate semantic similarities
            try:
                similarities = cosine_similarity(
                    query_embedding, job_embeddings
                )[0]
            except Exception as e:
                logger.warning(f"❌ Similarity calculation failed: {e}")
                return self._fallback_basic_filter(jobs, query)

            # Step 4: Adaptive threshold based on query specificity
            try:
                threshold = self._calculate_adaptive_threshold(
                    query, similarities
                )
            except Exception as e:
                logger.warning(f"❌ Threshold calculation failed: {e}")
                threshold = 0.3  # Safe fallback threshold

            # Step 5: Multi-factor scoring
            relevant_jobs = []
            for i, job in enumerate(jobs):
                try:
                    semantic_score = float(similarities[i])

                    if semantic_score >= threshold:
                        # Calculate comprehensive relevance
                        final_score = self._calculate_comprehensive_relevance(
                            job, query, semantic_score,
                        )

                        job["semantic_score"] = semantic_score
                        job["final_score"] = final_score
                        job["threshold_used"] = threshold
                        relevant_jobs.append(job)
                except Exception as e:
                    logger.warning(
                        "❌ Job scoring failed for "
                        f"'{job.get('Title', 'Unknown')}': {e}"
                    )
                    continue  # Skip this job and continue with others

            logger.info(
                f"✅ JobBERT processing complete: "
                f"{len(relevant_jobs)} relevant jobs"
            )
            
            result = sorted(
                relevant_jobs, key=lambda x: x.get("final_score", 0),
                reverse=True
            )
            
            return result

        except Exception as e:
            logger.error(f"❌ JobBERT processing failed completely: {e}")
            logger.info("🔄 Falling back to basic keyword filtering")
            return self._fallback_basic_filter(jobs, query)
        
        finally:
            # Critical: Clean up embeddings to prevent memory leaks
            try:
                if job_embeddings is not None:
                    del job_embeddings
                if query_embedding is not None:
                    del query_embedding
                if similarities is not None:
                    del similarities
                
                # Force cleanup after processing
                import gc
                gc.collect()
                
                memory_final = get_memory_usage()
                logger.debug(f"🧹 Memory after cleanup: {memory_final:.1f} MB")
                
                # Check if we should unload the model after this operation
                if should_unload_model():
                    unload_jobbert_model()
                    
            except Exception as cleanup_error:
                logger.warning(f"⚠️ Cleanup error (non-critical): {cleanup_error}")

    def _calculate_adaptive_threshold(self, query, similarities):
        """Improved adaptive threshold calculation"""
        if np is not None:
            similarities = np.array(similarities)
        else:
            return self._fallback_threshold_calculation(query, similarities)

        query_words = query.lower().split()

        potential_domain_indicators = 0
        for word in query_words:
            if (len(word) <= 4 and word.isupper()) or len(word) > 8 or \
                    any(char.isdigit() for char in word):
                potential_domain_indicators += 1

        has_likely_domain_terms = potential_domain_indicators > 0
        domain_ratio = (
            potential_domain_indicators / len(query_words)
            if query_words else 0
        )

        word_count = len(query_words)
        avg_word_length = sum(len(word) for word in query_words) / max(
            word_count, 1
        )

        complexity_score = min(
            (word_count / 6.0) * 0.6 + (avg_word_length / 12.0) * 0.4, 1.0
        )

        sim_mean = float(similarities.mean())
        sim_std = float(similarities.std())
        sim_median = float(np.median(similarities))
        sim_75th = float(np.percentile(similarities, 75))

        if sim_std > 0.15:
            base_threshold = min(sim_75th * 0.8, 0.45)
        elif sim_std > 0.08:
            base_threshold = min(sim_median + 0.1, 0.4)
        elif sim_mean > 0.6:
            base_threshold = min(sim_mean * 0.9, 0.5)
        else:
            base_threshold = max(sim_mean * 0.7, 0.25)

        if has_likely_domain_terms:
            domain_adjustment = 0.12 + (domain_ratio * 0.18)
            logger.debug(
                f"Domain-specific query detected: applying "
                f"+{domain_adjustment:.3f} threshold boost"
            )
        else:
            domain_adjustment = 0.0

        complexity_adjustment = complexity_score * 0.10

        sample_size = len(similarities)
        if sample_size > 200:
            size_adjustment = 0.03
        elif sample_size < 50:
            size_adjustment = -0.03
        else:
            size_adjustment = 0.0

        final_threshold = (
            base_threshold + domain_adjustment +
            complexity_adjustment + size_adjustment
        )

        if has_likely_domain_terms:
            min_threshold = 0.20
            max_threshold = 0.65
        else:
            min_threshold = 0.15
            max_threshold = 0.55

        final_threshold = max(
            min_threshold, min(max_threshold, final_threshold)
        )

        logger.debug(
            f"Threshold calculation: base={base_threshold:.3f}, "
            f"domain_adj={domain_adjustment:.3f}, "
            f"complexity_adj={complexity_adjustment:.3f}, "
            f"size_adj={size_adjustment:.3f}, "
            f"final={final_threshold:.3f}, "
            f"domain_detected={has_likely_domain_terms}"
        )

        return final_threshold

    def _fallback_threshold_calculation(self, query, similarities):
        """Fallback threshold calculation with domain awareness when numpy is
        not available"""
        sim_values = list(similarities)
        sim_mean = sum(sim_values) / len(sim_values) if sim_values else 0.3

        query_words = query.lower().split()
        complexity_score = min(len(query_words) / 6.0, 1.0)

        potential_domain_indicators = 0
        for word in query_words:
            if (len(word) <= 4 and word.isupper()) or len(word) > 8 or \
                    any(char.isdigit() for char in word):
                potential_domain_indicators += 1

        has_likely_domain_terms = potential_domain_indicators > 0
        domain_ratio = (
            potential_domain_indicators / len(query_words)
            if query_words else 0
        )

        base_threshold = max(sim_mean * 0.8, 0.25)
        complexity_adjustment = complexity_score * 0.1

        if has_likely_domain_terms:
            domain_adjustment = 0.12 + (domain_ratio * 0.18)
            max_threshold = 0.70
            min_threshold = 0.25
        else:
            domain_adjustment = 0.0
            max_threshold = 0.55
            min_threshold = 0.15

        final_threshold = base_threshold + complexity_adjustment \
            + domain_adjustment

        return max(min_threshold, min(max_threshold, final_threshold))

    def _calculate_comprehensive_relevance(
        self, job, query, semantic_score
    ):
        """Enhanced relevance calculation with adaptive weighting based on
        domain specificity"""
        try:
            classification = self.dynamic_classifier. \
                classify_query_comprehensively(
                    query, [job]
                )
            domain_terms = classification["domain_terms"]
            has_domain_specificity = len(domain_terms) > 0
        except Exception as e:
            logger.warning(f"❌ Domain classification failed: {e}")
            has_domain_specificity = False
            domain_terms = []

        if has_domain_specificity:
            semantic_weight = 0.25
            keyword_weight = 0.20
            recency_weight = 0.10
            quality_weight = 0.05
            domain_weight = 0.40

            logger.debug(
                f"Domain-specific query detected for '{query}': "
                f"using domain-focused weighting"
            )
        else:
            semantic_weight = 0.45
            keyword_weight = 0.20
            recency_weight = 0.15
            quality_weight = 0.05
            domain_weight = 0.15

            logger.debug(
                f"Generic query detected for '{query}': "
                f"using semantic-focused weighting"
            )

        try:
            query_words = set(query.lower().split())

            if has_domain_specificity:
                job_words = set(job["Title"].lower().split())
                logger.debug(
                    "Domain-specific query: using title-only "
                    f"keyword matching for '{job['Title']}'"
                )
            else:
                job_words = set(
                    f"{job['Title']} {job['Company']}".lower().split()
                )
                logger.debug(
                    "General query: using title+company keyword matching"
                )

            exact_matches = len(query_words.intersection(job_words))
            keyword_coverage = exact_matches / len(query_words) \
                if query_words else 0

            partial_matches = 0
            for q_word in query_words:
                for j_word in job_words:
                    if len(q_word) > 3 and q_word in j_word:
                        partial_matches += 0.5

            enhanced_keyword_score = min(
                (keyword_coverage + partial_matches * 0.3), 1.0
            )
        except Exception as e:
            logger.warning(f"❌ Keyword scoring failed: {e}")
            enhanced_keyword_score = 0.0

        try:
            recency_score = self._calculate_recency_score(job["Date Posted"])
        except Exception as e:
            logger.warning(f"❌ Recency scoring failed: {e}")
            recency_score = 0.3

        try:
            quality_score = self._calculate_enhanced_job_quality(job)
        except Exception as e:
            logger.warning(f"❌ Quality scoring failed: {e}")
            quality_score = 0.5

        try:
            domain_coherence_score = self._calculate_domain_coherence(
                job, query, [job]
            )
        except Exception as e:
            logger.warning(f"❌ Domain coherence calculation failed: {e}")
            domain_coherence_score = 1.0 if not has_domain_specificity else 0.3

        if has_domain_specificity and domain_coherence_score < 0.4:
            logger.debug(
                f"Domain-specific job '{job['Title']}' failed "
                f"domain coherence check: "
                f"{domain_coherence_score:.3f} < 0.4"
            )
            return 0.05

        if has_domain_specificity and domain_coherence_score < 0.6:
            domain_penalty = (0.6 - domain_coherence_score) * 0.3
            logger.debug(
                f"Applying domain penalty of {domain_penalty:.3f} "
                f"to '{job['Title']}'"
            )
        else:
            domain_penalty = 0.0

        try:
            final_score = (
                semantic_score * semantic_weight +
                enhanced_keyword_score * keyword_weight +
                recency_score * recency_weight +
                quality_score * quality_weight +
                domain_coherence_score * domain_weight
            ) - domain_penalty

            final_score = max(final_score, 0.0)
        except Exception as e:
            logger.warning(f"❌ Final score calculation failed: {e}")
            final_score = semantic_score * 0.5

        if has_domain_specificity:
            logger.debug(
                f"Adaptive scoring for '{job['Title']}': "
                f"semantic={semantic_score:.3f}*{semantic_weight} + "
                f"keyword={enhanced_keyword_score:.3f}*{keyword_weight} + "
                f"recency={recency_score:.3f}*{recency_weight} + "
                f"quality={quality_score:.3f}*{quality_weight} + "
                f"domain={domain_coherence_score:.3f}*{domain_weight} = "
                f"{final_score:.3f}"
            )

        return final_score

    def _calculate_recency_score(self, date_posted):
        """Calculate recency score"""
        try:
            posted_date = parse_date_posted_to_datetime(date_posted)
            now = datetime.now(pytz.UTC)
            hours_old = (now - posted_date).total_seconds() / 3600

            if hours_old <= 24:
                return 1.0
            if hours_old <= 168:
                return 0.8
            if hours_old <= 720:
                return 0.5
            return 0.1
        except Exception:
            return 0.3

    def _calculate_enhanced_job_quality(self, job):
        """Enhanced job quality calculation"""
        quality_score = 0.5

        title = job["Title"]
        company = job["Company"]
        location = job["Location"]

        if 10 <= len(title) <= 100:
            quality_score += 0.2

        if 3 <= len(company) <= 50:
            quality_score += 0.15

        location_lower = location.lower()
        if any(
            indicator in location_lower
            for indicator in ["remote", "hybrid", "city", "street", "avenue"]
        ):
            quality_score += 0.1

        title_lower = title.lower()
        if any(
            positive in title_lower
            for positive in [
                "senior", "junior", "lead", "principal", "manager"
            ]
        ):
            quality_score += 0.05

        generic_titles = [
            "assistant", "associate", "specialist",
            "representative", "coordinator"
        ]
        if any(generic in title_lower for generic in generic_titles):
            quality_score -= 0.1

        if title.count("!") > 3 or title.count("$") > 0:
            quality_score -= 0.2

        word_count = len(title.split())
        if 2 <= word_count <= 8:
            quality_score += 0.1

        return min(max(quality_score, 0.1), 1.0)

    def _calculate_domain_coherence(self, job, query, jobs):
        """Calculate domain coherence using ultra-pure dynamic term
        classification with enhanced strictness"""
        classification = self.dynamic_classifier. \
            classify_query_comprehensively(
                query, jobs
            )

        domain_terms = classification["domain_terms"]
        job_type_terms = classification["job_type_terms"]

        query_lower = query.lower()
        fallback_domain_terms = []

        domain_indicators = {
            "ai": [
                "ai", "artificial intelligence", "machine learning", "ml",
                "deep learning"
            ],
            "web": [
                "web", "frontend", "backend", "javascript", "react", "vue",
                "angular"
            ],
            "data": ["data science", "data analyst", "analytics", "sql"],
            "mobile": ["mobile", "ios", "android", "swift", "kotlin"],
            "cloud": ["cloud", "aws", "azure", "gcp", "devops"],
            "security": ["security", "cybersecurity", "infosec"],
        }

        for domain, keywords in domain_indicators.items():
            if any(keyword in query_lower for keyword in keywords):
                fallback_domain_terms.extend(
                    [kw for kw in keywords if kw in query_lower]
                )

        all_domain_terms = list(set(domain_terms + fallback_domain_terms))

        if all_domain_terms or job_type_terms:
            logger.debug(
                f"DOMAIN COHERENCE for '{query}': "
                f"Dynamic={domain_terms}, Fallback={fallback_domain_terms}, "
                f"Combined={all_domain_terms}, "
                f"JobType={job_type_terms}, "
                f"Method={classification.get('method', 'unknown')}"
            )

        if not all_domain_terms:
            return 1.0

        job_title = job["Title"].lower()
        job_company = job["Company"].lower()

        title_exact_matches = 0
        for term in all_domain_terms:
            if term in job_title:
                title_exact_matches += 1
                logger.debug(
                    f"   ✅ TITLE MATCH: '{term}' found in '{job['Title']}'"
                )

        company_exact_matches = 0
        for term in all_domain_terms:
            if term in job_company:
                company_exact_matches += 1
                logger.debug(
                    f"   🔸 COMPANY MATCH: '{term}' found in '{job['Company']}'"
                )

        title_domain_score = (
            title_exact_matches / len(all_domain_terms)
            if all_domain_terms else 0
        )
        company_domain_score = (
            company_exact_matches / len(all_domain_terms)
            if all_domain_terms else 0
        )

        combined_domain_score = (
            (title_domain_score * 0.9) + (company_domain_score * 0.1)
        )

        partial_matches = 0
        for domain_term in all_domain_terms:
            for job_word in job_title.split():
                if len(domain_term) >= 3 and len(job_word) >= 3:
                    if domain_term in job_word and \
                            len(domain_term) >= len(job_word) * 0.6:
                        partial_matches += 0.3
                        logger.debug(
                            f"   🔸 PARTIAL TITLE MATCH: '{domain_term}' "
                            f"~ '{job_word}'"
                        )
                    break

        partial_domain_score = (
            min(partial_matches / len(all_domain_terms), 1.0)
            if all_domain_terms else 0
        )

        final_combined_score = (
            combined_domain_score + (partial_domain_score * 0.05)
        )

        if self._ensure_model_loaded():
            try:
                domain_query = " ".join(all_domain_terms)
                domain_embedding = self.model.encode([domain_query])
                title_embedding = self.model.encode([job_title])

                semantic_score = cosine_similarity(
                    domain_embedding, title_embedding
                )[0][0]

                if final_combined_score >= 0.5:
                    final_score = (
                        (final_combined_score * 0.8) +
                        (float(semantic_score) * 0.2)
                    )
                elif final_combined_score >= 0.2:
                    final_score = (
                        (final_combined_score * 0.6) +
                        (float(semantic_score) * 0.4)
                    )
                    if semantic_score < 0.6:
                        final_score *= 0.7
                else:
                    final_score = float(semantic_score) * 0.4
                    if semantic_score < 0.8:
                        final_score *= 0.5

                logger.debug(
                    f"DOMAIN COHERENCE for '{job['Title']}': "
                    f"title_exact={title_domain_score:.3f}, "
                    f"company_exact={company_domain_score:.3f}, "
                    f"partial={partial_domain_score:.3f}, "
                    f"combined={final_combined_score:.3f}, "
                    f"semantic={semantic_score:.3f}, final={final_score:.3f}"
                )

                if len(all_domain_terms) > 0 and final_score < 0.2:
                    logger.debug(
                        f"   ❌ HARD MINIMUM FAIL: {final_score:.3f} < 0.2 "
                        f"for domain query"
                    )
                    return 0.05

                return final_score

            except Exception as e:
                logger.warning(f"❌ JobBERT semantic matching failed in domain coherence: {e}")
                # Clear the model to force reload on next attempt
                self.model = None
                return final_combined_score if final_combined_score >= 0.4 else 0.1
        
        # Model not available, using lexical matching only
        logger.debug(f"🔄 Model not available for domain coherence, using lexical score: {final_combined_score:.3f}")
        
        if final_combined_score >= 0.4:
            return final_combined_score
        logger.debug(
            f"   ❌ LEXICAL FAIL: {final_combined_score:.3f} < 0.4 threshold"
        )
        return 0.1

    def _fallback_basic_filter(self, jobs, query):
        """Fallback when JobBERT is not available - basic keyword matching"""
        query_words = set(query.lower().split())
        filtered_jobs = []

        for job in jobs:
            job_text = f"{job['Title']} {job['Company']}".lower()
            job_words = set(job_text.split())

            coverage = len(
                query_words.intersection(job_words)
            ) / len(query_words)

            if coverage >= 0.5:
                job["semantic_score"] = coverage
                job["final_score"] = coverage
                job["threshold_used"] = 0.5
                filtered_jobs.append(job)

        return sorted(
            filtered_jobs, key=lambda x: x["final_score"], reverse=True
        )


def scrape_linkedin(keyword, location, filters_dict, max_pages=None):
    """Reusable and DYNAMIC scraping function.
    Set max_pages to a number to limit page scraping, or None to scrape all.
    """
    all_jobs_data = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/96.0.4664.93 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8,"
                  "application/signed-exchange;v=b3;q=0.9",
        "Connection": "keep-alive",
    }

    filter_params = "".join(
        [f"&{key}={quote_plus(value)}"
         for key, value in filters_dict.items() if value]
    )

    page_number = 0
    while True:
        if max_pages and page_number >= max_pages:
            logger.info(
                f"Reached max_pages limit of {max_pages}. Stopping scrape."
            )
            break

        start_index = page_number * 25
        url = (f"https://www.linkedin.com/jobs-guest/jobs/api/"
               f"seeMoreJobPostings/search?keywords={quote_plus(keyword)}"
               f"&location={quote_plus(location)}&start={start_index}"
               f"{filter_params}")

        try:
            time.sleep(1.5)  # Be respectful to LinkedIn's servers
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "lxml")
            job_cards = soup.find_all("div", class_="base-card")

            if not job_cards:
                logger.info(
                    f"No more job cards found on page {page_number}. "
                    f"Stopping scrape."
                )
                break

            for job in job_cards:
                try:
                    raw_link = job.find(
                        "a", class_="base-card__full-link"
                    )["href"]
                    clean_link = raw_link.split("?")[0]
                    date_posted_element = (
                        job.find("time", class_="job-search-card__listdate") or
                        job.find("time")
                    )
                    all_jobs_data.append({
                        "Title": job.find(
                            "h3", class_="base-search-card__title"
                        ).text.strip(),
                        "Company": job.find(
                            "h4", class_="base-search-card__subtitle"
                        ).text.strip(),
                        "Location": job.find(
                            "span", class_="job-search-card__location"
                        ).text.strip(),
                        "Date Posted": date_posted_element.text.strip(),
                        "Link": clean_link,
                    })
                except (AttributeError, TypeError):
                    continue

            page_number += 1

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400:
                logger.info(
                    f"LinkedIn pagination limit reached at start={start_index}"
                    f". Stopping scrape."
                )
                break
            logger.error(f"HTTP error for url {url}: {e}")
            break
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for url {url}: {e}")
            break

    return sorted(
        all_jobs_data,
        key=lambda job: parse_date_posted(job["Date Posted"]),
        reverse=True
    )


JOBQUEST_DATE_MAP = {"r86400": 24, "r604800": 168, "r2592000": 720}
JOBQUEST_JOBTYPE_MAP = {"F": "fulltime", "P": "parttime", "C": "contract", "I": "internship"}


def _bot_filters_to_jobquest_kwargs(filters_dict):
    """Translate bot's LinkedIn-URL-param filters → JobQuest kwargs.

    Returns (base_kwargs, job_types_list) where job_types_list is for fan-out.
    """
    kw = {}
    if filters_dict.get("f_TPR"):
        kw["hours_old"] = JOBQUEST_DATE_MAP.get(filters_dict["f_TPR"])
    if filters_dict.get("f_WT"):
        codes = set(filters_dict["f_WT"].split(","))
        if codes == {"2"}:
            kw["is_remote"] = True
    job_types = []
    if filters_dict.get("f_JT"):
        for c in filters_dict["f_JT"].split(","):
            if c in JOBQUEST_JOBTYPE_MAP:
                job_types.append(JOBQUEST_JOBTYPE_MAP[c])
    return kw, job_types


def _format_relative_posted(dp):
    """Format a date/datetime as a human-readable relative time like LinkedIn.

    LinkedIn provides hour-level precision (minutes/hours), Indeed and Glassdoor
    are day-level only. Returns:
        "just now" | "X minutes ago" | "X hours ago" | "today" | "yesterday"
        | "X days ago" | "X weeks ago" | "YYYY-MM-DD" (>= 30 days)
    """
    if dp is None or (hasattr(dp, "isoformat") and str(dp) == "NaT") or (isinstance(dp, float) and str(dp) == "nan"):
        return "N/A"
    now = datetime.now()
    if hasattr(dp, "hour") and hasattr(dp, "minute"):
        # Datetime (LinkedIn) or pandas Timestamp — may have hour precision
        if hasattr(dp, "to_pydatetime"):
            dp = dp.to_pydatetime()
        if dp.tzinfo is not None:
            dp = dp.replace(tzinfo=None)
        # Indeed and Glassdoor return midnight (00:00:00) — they don't have
        # real hour info. Treat midnight timestamps as date-only.
        if dp.hour == 0 and dp.minute == 0 and dp.second == 0:
            days = (now.date() - dp.date()).days
        else:
            diff_s = (now - dp).total_seconds()
            if diff_s < 60:
                return "Just now"
            if diff_s < 3600:
                mins = int(diff_s // 60)
                return f"{mins} minute{'s' if mins != 1 else ''} ago"
            if diff_s < 86400:
                hours = int(diff_s // 3600)
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            days = int(diff_s // 86400)
    else:
        # date-only (raw datetime.date object)
        days = (now.date() - dp).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    # Older than a month: format as "20 May 2026" instead of "2026-05-20"
    if hasattr(dp, "strftime"):
        try:
            return dp.strftime("%-d %B %Y")
        except (ValueError, AttributeError):
            return dp.strftime("%d %B %Y").lstrip("0")
    return str(dp)


def _jobquest_df_to_bot_jobs(df):
    """Translate JobQuest DataFrame rows → bot's dict-of-strings shape."""
    if df is None or len(df) == 0:
        return []
    jobs = []
    for _, row in df.iterrows():
        link = str(row.get("job_url") or "")
        title = str(row.get("title") or "")
        raw_company = row.get("company")
        company = str(raw_company) if raw_company is not None and str(raw_company) != "nan" else ""
        loc = row.get("location")
        loc_str = str(loc) if loc and str(loc) != "nan" else ""
        dp = row.get("date_posted")
        dp_str = _format_relative_posted(dp)
        if not link or not title:
            continue
        jobs.append({
            "Title": title, "Company": company,
            "Location": loc_str, "Date Posted": dp_str, "Link": link,
        })
    return jobs


# Per-alert adaptive scrape state — a feedback loop with NO hardcoded depth caps.
# Bounds are derived from real signals, not magic numbers:
#   floor   = one scrape page (a real API page size, the smallest useful request)
#   ceiling = how many jobs fit in this alert's time budget, computed from the
#             scheduler interval ÷ active-alert count ÷ measured per-job latency
#   start   = floor (begin minimal, grow only as a query proves it needs depth)
# State per keyword+location (in-memory, re-learns within a few cycles after restart):
#   depth        — results/site to pull next time
#   ema_dur      — EMA of measured scrape wall-time (drives the timeout)
#   sec_per_job  — EMA of measured per-result latency (drives the depth ceiling)
_alert_scrape_state = {}
_SCRAPE_PAGE = 25                  # LinkedIn guest API returns 25/page; the natural unit
_SCHEDULER_INTERVAL_S = 30 * 60    # alert-check interval (see scheduler add_job)
_active_alert_cache = {"n": 1, "ts": 0}


def _active_alert_count():
    """Cached count of active alerts (refreshed every 5 min) — drives time budget."""
    now = time.time()
    if now - _active_alert_cache["ts"] > 300:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM alerts WHERE is_active = 1")
            row = cur.fetchone()
            # get_db_connection uses RealDictCursor → row is a dict
            count = row["c"] if isinstance(row, dict) else row[0]
            _active_alert_cache["n"] = max(1, int(count))
            _active_alert_cache["ts"] = now
        except Exception as e:
            logger.warning(f"active_alert_count query failed: {e}")
        finally:
            if conn:
                db_pool.return_connection(conn)
    return _active_alert_cache["n"]


def _per_alert_budget_s():
    """Each alert's fair share of the cycle, 70% of interval split across alerts."""
    return (_SCHEDULER_INTERVAL_S * 0.7) / _active_alert_count()


def _scrape_key(keyword, location):
    return f"{(keyword or '').strip().lower()}|{(location or '').strip().lower()}"


def _scrape_plan(keyword, location):
    """Return (depth, timeout) for this alert from learned state."""
    st = _alert_scrape_state.setdefault(
        _scrape_key(keyword, location),
        {"depth": _SCRAPE_PAGE, "ema_dur": None, "sec_per_job": None},
    )
    depth = st["depth"]
    # Timeout = measured wall-time × 2.5 margin, clamped [60,180]s. A single
    # scrape never needs longer (the board exhausts well before that); the time
    # budget governs depth, not timeout. 120s default before first measurement.
    timeout = int(max(60, min(180, st["ema_dur"] * 2.5))) if st["ema_dur"] else 120
    return depth, timeout


def _record_scrape_duration(keyword, location, duration, per_site):
    st = _alert_scrape_state.setdefault(
        _scrape_key(keyword, location),
        {"depth": _SCRAPE_PAGE, "ema_dur": None, "sec_per_job": None},
    )
    st["ema_dur"] = duration if st["ema_dur"] is None else 0.6 * st["ema_dur"] + 0.4 * duration
    spj = duration / max(1, per_site)
    st["sec_per_job"] = spj if st["sec_per_job"] is None else 0.6 * st["sec_per_job"] + 0.4 * spj


def adapt_scrape_depth(keyword, location, scraped, new_count):
    """Feedback controller. Each alert scrapes as deep as its per-alert time
    budget allows, climbing one page per cycle toward that budget ceiling and
    holding there. The budget (_per_alert_budget_s ÷ measured per-job latency)
    already guarantees the whole cycle fits the scheduler interval, so it is the
    only bound — no hardcoded caps.

    Why ramp to the ceiling instead of growing only when new jobs appear: fresh
    jobs are often buried *below* the already-sent results at the top of a board
    (a heavy sent-history makes the top-N all dupes). A controller that retreats
    on dupe-saturation never digs deep enough to reach them, so the alert goes
    silent even while new postings exist. Climbing to the budget ceiling surfaces
    those buried-deep jobs; `scraped`/`new_count` are kept for logging only."""
    st = _alert_scrape_state.setdefault(
        _scrape_key(keyword, location),
        {"depth": _SCRAPE_PAGE, "ema_dur": None, "sec_per_job": None},
    )
    d = st["depth"]
    floor = _SCRAPE_PAGE
    # Dynamic ceiling: how many results we can afford within the time budget.
    if st["sec_per_job"]:
        ceiling = max(floor, int(_per_alert_budget_s() / st["sec_per_job"]))
    else:
        ceiling = d + _SCRAPE_PAGE  # unknown latency yet — allow one page of growth
    if d < ceiling:
        st["depth"] = min(d + _SCRAPE_PAGE, ceiling)   # climb toward budget ceiling
    else:
        st["depth"] = ceiling                          # budget shrank → ease down
    st["depth"] = max(floor, min(st["depth"], ceiling))
    if st["depth"] != d:
        logger.info(f"🔍 [SCRAPE_ADAPT] '{keyword}'@'{location}': depth {d}→{st['depth']} "
                    f"(scraped={scraped}, new={new_count}, ceiling={ceiling})")
    return st["depth"]


def _jobquest_scrape_multi_board(keyword, location, filters_dict, results_wanted=None):
    """Multi-board scrape: LinkedIn + Indeed + Glassdoor → bot-shape dicts.

    LinkedIn uses the fast guest-API scraper (`scrape_linkedin`) — ~3x faster than
    JobQuest's LinkedIn path (no per-job overhead/stealth delays), so it reaches the
    depth needed to surface recent jobs buried in LinkedIn's relevance ordering. The
    guest endpoint is public/unauthenticated (no Cloudflare), and tolerates this. If
    it ever yields nothing (throttle/HTML change), we fall back to JobQuest LinkedIn,
    so there's no total-outage risk. Indeed + Glassdoor stay on JobQuest. Both run
    concurrently. Depth/timeout remain dynamic via the _scrape_plan feedback loop;
    LinkedIn gets a deeper page floor since it's fast and the user's priority.
    """
    from jobquest import scrape_jobs
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
    import pandas as _pd
    import time as _time

    fdict = filters_dict or {}
    base_kw, job_types = _bot_filters_to_jobquest_kwargs(fdict)
    depth, TIMEOUT = _scrape_plan(keyword, location)
    per_site = min(results_wanted, depth) if results_wanted else depth
    # No hardcoded caps — scrape as much as is available, bounded only by the
    # adaptive per-alert time budget (per_site already = budget ceiling). LinkedIn
    # paginates by 25; floor at 8 pages so the priority board always goes deep, then
    # follow the adaptive depth. Indeed/Glassdoor scrape to the same budget depth.
    li_pages = max(8, -(-per_site // 25))
    ig_per_site = per_site

    # Reuse the alert's LinkedIn-param filters; inject a time-posted-range from
    # hours_old so the deep guest-API scrape stays on recent postings.
    li_filters = dict(fdict)
    if "f_TPR" not in li_filters and base_kw.get("hours_old"):
        _tpr = {24: "r86400", 168: "r604800", 720: "r2592000"}.get(base_kw["hours_old"])
        if _tpr:
            li_filters["f_TPR"] = _tpr

    ig_common = dict(search_term=keyword, location=location, results_wanted=ig_per_site,
                     country_indeed="germany", verbose=0, **base_kw)
    logger.info(f"🔍 [SCRAPE_PARAMS] li_pages={li_pages} ig_per_site={ig_per_site} timeout={TIMEOUT}s "
                f"hours_old={base_kw.get('hours_old')} job_types={len(job_types)}")
    _scrape_t0 = _time.time()

    def _scrape_linkedin_fast():
        try:
            return scrape_linkedin(keyword, location, li_filters, max_pages=li_pages)
        except Exception as e:
            logger.warning(f"Fast LinkedIn scrape error: {e}")
            return None

    def _scrape_indeed_glassdoor():
        sites = ["indeed", "glassdoor"]
        if not job_types or len(job_types) >= 4:
            return scrape_jobs(site_name=sites, **ig_common)
        if len(job_types) == 1:
            return scrape_jobs(site_name=sites, job_type=job_types[0], **ig_common)
        dfs = []
        for jt in job_types:
            try:
                dfs.append(scrape_jobs(site_name=sites, job_type=jt, **ig_common))
            except Exception:
                pass
        return _pd.concat(dfs).drop_duplicates(subset=["job_url"]).reset_index(drop=True) if dfs else _pd.DataFrame()

    li_jobs, ig_df = None, _pd.DataFrame()
    try:
        with ThreadPoolExecutor(max_workers=2) as exe:
            f_li = exe.submit(_scrape_linkedin_fast)
            f_ig = exe.submit(_scrape_indeed_glassdoor)
            try:
                li_jobs = f_li.result(timeout=TIMEOUT)
            except _Timeout:
                logger.warning(f"Fast LinkedIn scrape timed out after {TIMEOUT}s")
            try:
                ig_df = f_ig.result(timeout=TIMEOUT)
            except _Timeout:
                logger.warning(f"Indeed/Glassdoor scrape timed out after {TIMEOUT}s")
    except Exception as e:
        logger.error(f"Multi-board scrape failed: {e}", exc_info=True)

    # Fallback: fast LinkedIn yielded nothing → let JobQuest cover LinkedIn too.
    if not li_jobs:
        logger.warning("Fast LinkedIn empty — falling back to JobQuest LinkedIn")
        try:
            li_jobs = _jobquest_df_to_bot_jobs(scrape_jobs(site_name=["linkedin"], **ig_common))
        except Exception as e:
            logger.error(f"JobQuest LinkedIn fallback failed: {e}")
            li_jobs = []

    _record_scrape_duration(keyword, location, _time.time() - _scrape_t0, per_site)
    return (li_jobs or []) + _jobquest_df_to_bot_jobs(ig_df)


def scrape_linkedin_with_adaptive_jobbert(
    keyword, location, filters_dict,
    max_pages=None, progress_msg=None, user_id=None
):
    """Adaptive JobBERT filtering with memory management (thread-safe).

    Now uses JobQuest under the hood: LinkedIn + Indeed + Glassdoor with
    Chrome TLS stealth and Cloudflare bypass. JobBERT pipeline unchanged.
    """
    import gc
    import time

    function_start = time.time()
    resources_start = get_system_resources()
    logger.info(f"🔍 [STAGE] SCRAPE_START | user={user_id} | keyword='{keyword}' | location='{location}'")
    logger.info(f"🔍 [RESOURCE] mem={resources_start['mem_mb']:.1f}MB | cpu={resources_start['cpu_pct']:.1f}% | threads={resources_start['threads']} | sys_mem_avail={resources_start['sys_mem_avail_mb']:.1f}MB")

    if progress_msg:
        safe_progress_update(
            progress_msg,
            "🔍 **Searching jobs across LinkedIn, Indeed & Glassdoor** ⠋\n\n"
            "⏳ _Stealth mode active..._",
            ParseMode.MARKDOWN
        )

    # results_wanted only set as a ceiling when caller forces max_pages;
    # otherwise depth is computed dynamically inside the scrape function.
    results_wanted = (max_pages * 25) if max_pages else None
    scrape_start = time.time()
    all_scraped_jobs = _jobquest_scrape_multi_board(
        keyword, location, filters_dict or {}, results_wanted=results_wanted
    )
    scrape_duration = time.time() - scrape_start

    # Cross-board in-scrape dedup. Collapse the same job appearing on several
    # boards, and when it does, keep the LinkedIn listing so the user gets the
    # LinkedIn link (not Indeed/Glassdoor). Only treat title+company as the same
    # job when BOTH are non-empty — otherwise distinct jobs with a missing
    # company would wrongly collapse and silently drop real postings.
    def _is_linkedin(link):
        return "linkedin.com" in (link or "").lower()

    seen_job_ids = set()
    seen_pairs = {}            # (c_title, c_company) -> index in `deduped`
    deduped = []
    for job_data in all_scraped_jobs:
        job_id = canonical_link(job_data["Link"])
        if job_id in seen_job_ids:
            continue
        c_title = canonical_text(job_data["Title"])
        c_company = canonical_text(job_data["Company"])
        pair = (c_title, c_company) if (c_title and c_company) else None
        if pair is not None and pair in seen_pairs:
            # same job already kept from another board — upgrade to the LinkedIn
            # copy if this one is LinkedIn and the kept one is not
            idx = seen_pairs[pair]
            if _is_linkedin(job_data["Link"]) and not _is_linkedin(deduped[idx]["Link"]):
                seen_job_ids.discard(canonical_link(deduped[idx]["Link"]))
                seen_job_ids.add(job_id)
                deduped[idx] = job_data
            continue
        seen_job_ids.add(job_id)
        if pair is not None:
            seen_pairs[pair] = len(deduped)
        deduped.append(job_data)
    all_scraped_jobs = deduped

    logger.info(f"🔍 [STAGE] JOBQUEST_SCRAPE_COMPLETE | jobs={len(all_scraped_jobs)} | scrape_time={scrape_duration:.2f}s | elapsed={time.time()-function_start:.2f}s")

    if not all_scraped_jobs:
        logger.info(f"🔍 [STAGE] SCRAPE_END_NO_JOBS | elapsed={time.time()-function_start:.2f}s")
        return []

    if progress_msg:
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[0]
        safe_progress_update(
            progress_msg,
            f"🤖 **AI Filtering** {loading_char}\n\n⚡ _Analyzing relevance..._",
            ParseMode.MARKDOWN
        )

    logger.info(f"🔍 [STAGE] AI_FILTERING_START | elapsed={time.time()-function_start:.2f}s")
    logger.info(f"🔍 [DIAG] About to call get_adaptive_matcher()...")
    matcher_start = time.time()
    adaptive_matcher = get_adaptive_matcher()
    logger.info(f"🔍 [STAGE] MATCHER_LOADED | time={time.time()-matcher_start:.3f}s | elapsed={time.time()-function_start:.2f}s")
    logger.info(f"🔍 [DIAG] get_adaptive_matcher() returned in {time.time() - matcher_start:.3f} seconds, matcher={'valid' if adaptive_matcher else 'None'}")

    logger.info(f"🔍 FILTER DEBUG: Starting analysis of query '{keyword}'")

    keyword_lower = keyword.lower()
    ai_terms = [
        "ai", "artificial intelligence", "machine learning", "ml",
        "deep learning", "neural", "data science"
    ]
    has_ai_terms = any(term in keyword_lower for term in ai_terms)

    if has_ai_terms:
        logger.info(
            f"🎯 DOMAIN-SPECIFIC QUERY DETECTED: '{keyword}' contains AI terms"
        )
        pre_filtered_jobs = []
        ai_keywords = [
            "ai", "artificial intelligence", "machine learning", "ml",
            "deep learning", "neural", "data science", "computer vision",
            "nlp", "robotics", "algorithm", "tensorflow", "pytorch", "python"
        ]

        for job in all_scraped_jobs:
            job_title_text = job["Title"].lower()
            ai_match_score = 0
            for ai_term in ai_keywords:
                if ai_term in job_title_text:
                    ai_match_score += 1

            if ai_match_score > 0:
                pre_filtered_jobs.append(job)
                logger.debug(
                    f"✅ PRE-FILTER PASS: '{job['Title']}' "
                    f"(AI score: {ai_match_score})"
                )
            else:
                logger.debug(
                    f"❌ PRE-FILTER FAIL: '{job['Title']}' "
                    f"(No AI keywords found in title)"
                )

        logger.info(
            f"🎯 PRE-FILTER RESULTS: {len(all_scraped_jobs)} → "
            f"{len(pre_filtered_jobs)} jobs after AI keyword filtering"
        )
        jobs_to_analyze = pre_filtered_jobs
    else:
        logger.info(
            f"📝 GENERIC QUERY: '{keyword}' - applying standard filtering"
        )
        jobs_to_analyze = all_scraped_jobs

    if progress_msg:
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[2]
        progress_text = (
            f"🤖 **AI Filtering** {loading_char}\n\n"
            "🧠 _Applying semantic analysis..._"
        )
        safe_progress_update(progress_msg, progress_text, ParseMode.MARKDOWN)

    logger.info(f"🔍 [DIAG] About to call adaptive_matcher.calculate_adaptive_relevance() with {len(jobs_to_analyze)} jobs")
    ai_filter_start = time.time()

    try:
        final_jobs = adaptive_matcher.calculate_adaptive_relevance(
            jobs_to_analyze, keyword
        )
        logger.info(f"🔍 [DIAG] calculate_adaptive_relevance() completed in {time.time() - ai_filter_start:.3f} seconds, returned {len(final_jobs)} jobs")
    except MemoryError as e:
        logger.error(f"🚨 Memory error in JobBERT processing: {e}")
        logger.error(f"🔍 [DIAG] MemoryError in calculate_adaptive_relevance after {time.time() - ai_filter_start:.3f} seconds", exc_info=True)
        logger.warning("⚠️ Running garbage collection and falling back to basic filtering...")
        gc.collect()
        final_jobs = adaptive_matcher._fallback_basic_filter(
            jobs_to_analyze, keyword
        )
    except Exception as e:
        logger.error(f"🚨 AdaptiveJobBERTMatcher failed: {e}", exc_info=True)
        logger.error(f"🔍 [DIAG] Exception in calculate_adaptive_relevance after {time.time() - ai_filter_start:.3f} seconds", exc_info=True)
        logger.warning("⚠️ Falling back to basic filtering due to error.")
        final_jobs = adaptive_matcher._fallback_basic_filter(
            jobs_to_analyze, keyword
        )

    logger.info("🎯 FINAL FILTERING RESULTS:")
    logger.info(f"   📊 Original scraped: {len(all_scraped_jobs)}")
    logger.info(f"   🎯 After pre-filter: {len(jobs_to_analyze)}")
    logger.info(f"   ✅ Final relevant: {len(final_jobs)}")
    filter_efficiency = 0
    if all_scraped_jobs:
        filter_efficiency = (
            (len(all_scraped_jobs) - len(final_jobs)) /
            len(all_scraped_jobs) * 100
        )
    logger.info(
        f"   📈 Filter efficiency: {filter_efficiency:.1f}% filtered out"
    )

    if final_jobs:
        logger.info("🎉 TOP MATCHES:")
        for i, job in enumerate(final_jobs[:3]):
            score = job.get("final_score", 0)
            logger.info(
                f"   {i+1}. '{job['Title']}' at {job['Company']} "
                f"(score: {score:.3f})"
            )

    if progress_msg:
        loading_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        loading_char = loading_chars[5]
        completion_text = (
            f"✅ **AI Filtering Complete** {loading_char}\n\n"
            "🚀 _Preparing your results..._"
        )
        safe_progress_update(progress_msg, completion_text, ParseMode.MARKDOWN)

    logger.info(f"🔍 [STAGE] SORTING_RESULTS | jobs={len(final_jobs)} | elapsed={time.time()-function_start:.2f}s")
    final_jobs = sorted(
        final_jobs,
        key=lambda x: (
            parse_date_posted_to_datetime(x["Date Posted"]),
            x.get("final_score", 0)
        ),
        reverse=True
    )

    # Clean up memory after processing
    gc.collect()
    logger.info(f"🔍 [STAGE] SCRAPE_END_SUCCESS | total_jobs={len(final_jobs)} | total_time={time.time()-function_start:.2f}s")
    return final_jobs


def run_scrape_threaded(
    update: Update, context: CallbackContext, progress_msg
):
    """Thread-safe version of run_scrape that notifies the user if the AI is busy."""
    import time
    import threading

    user_id = update.effective_user.id
    current_thread = threading.current_thread().name
    logger.info(f"🔍 [DIAG] run_scrape_threaded() called for user {user_id} by thread {current_thread}")

    try:
        search_keyword = context.user_data.get("search_keywords")
        search_location = context.user_data.get("search_location")
        logger.info(f"🔍 [DIAG] Search params: keyword='{search_keyword}', location='{search_location}'")

        prefs = get_user_prefs(context)

        filters = {
            "f_E": ",".join(prefs["experience"].values()),
            "f_JT": ",".join(prefs["job_types"].values()),
            "f_TPR": list(prefs["date_posted"].values())[0] if prefs["date_posted"] else None,
            "f_WT": ",".join(prefs["workplace"].values()) if prefs["workplace"] else None,
        }
        logger.info(f"🔍 [DIAG] Filters: {filters}")

        logger.info(f"User {user_id} is attempting to start a live search.")
        logger.info(f"🔍 [DIAG] Attempting to acquire search_ai_lock (non-blocking)...")
        lock_attempt_start = time.time()

        # Try to acquire the search AI lock without blocking
        if not search_ai_lock.acquire(blocking=False):
            # The lock is busy. Notify the user and then wait.
            logger.info(f"AI lock is busy. Notifying user {user_id} that they are queued.")
            logger.info(f"🔍 [DIAG] search_ai_lock is busy, user queued. Now attempting blocking acquire...")
            safe_progress_update(
                progress_msg,
                "⏳ **Please Wait...**\n\nThe bot is processing a background task. "
                "Your search has been queued and will begin shortly!",
                ParseMode.MARKDOWN
            )
            # Now, block and wait until the lock is released.
            blocking_start = time.time()
            search_ai_lock.acquire()
            logger.info(f"Search AI lock acquired for user {user_id} after waiting.")
            logger.info(f"🔍 [DIAG] search_ai_lock acquired after {time.time() - blocking_start:.3f} seconds of blocking")
        else:
            logger.info(f"🔍 [DIAG] search_ai_lock acquired immediately (non-blocking) in {time.time() - lock_attempt_start:.3f} seconds")

        # At this point, we are GUARANTEED to have the lock.
        logger.info(f"🔍 [DIAG] search_ai_lock is held, proceeding with search...")
        try:
            # Notify the user that their search is now actively running.
            logger.info(f"🔍 [DIAG] Updating progress message to 'search is running'...")
            safe_progress_update(
                progress_msg,
                "🚀 **Your search is now running...**\n\n"
                "🔍 _Connecting to LinkedIn..._",
                ParseMode.MARKDOWN
            )

            logger.info(f"🔍 [DIAG] About to call scrape_linkedin_with_adaptive_jobbert()...")
            scrape_start = time.time()
            sorted_jobs = scrape_linkedin_with_adaptive_jobbert(
                search_keyword, search_location, filters,
                progress_msg=progress_msg, user_id=user_id,
            )
            logger.info(f"🔍 [DIAG] scrape_linkedin_with_adaptive_jobbert() completed in {time.time() - scrape_start:.3f} seconds, returned {len(sorted_jobs) if sorted_jobs else 0} jobs")

            if not sorted_jobs:
                safe_progress_update(progress_msg, "Search complete. No jobs found with these criteria.")
                time.sleep(2)
                text, kbd = make_main_menu(context)
                safe_progress_update(progress_msg, text)
                if progress_msg:
                    try:
                        progress_msg.edit_reply_markup(reply_markup=kbd)
                    except Exception:
                        pass
                return

            results_text = "🎉 **Search Complete!**\n\n📋 _Loading your job listings..._"
            safe_progress_update(progress_msg, results_text, ParseMode.MARKDOWN)
            time.sleep(1)

            context.user_data["jobs"] = sorted_jobs
            context.user_data["page"] = 0
            message_text, reply_markup = create_paginated_job_message(sorted_jobs, 0)

            if progress_msg:
                progress_msg.edit_text(
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

        finally:
            # CRUCIAL: Release the lock so other processes can run.
            logger.info(f"Releasing search AI lock for user {user_id}'s search.")
            search_ai_lock.release()

    except Exception as e:
        logger.error(f"Search failed for user {user_id}: {e}", exc_info=True)
        safe_progress_update(progress_msg, f"❌ Search failed: {e!s}")
    finally:
        unregister_user_operation(user_id, "search")


def run_scrape(update: Update, context: CallbackContext, progress_msg):
    """Legacy function for backwards compatibility."""
    return run_scrape_threaded(update, context, progress_msg)


# --- Browse and End Handlers ---
def page_navigation(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = update.effective_user.id
    
    try:
        safe_answer_callback_query(query)
        
        # Parse page number from callback data
        callback_data = query.data
        logger.debug(f"📄 Page navigation: user={user_id}, callback_data={callback_data}")
        
        page = int(callback_data.split("_")[1])
        
        # Check if jobs exist in user_data
        jobs = context.user_data.get("jobs")
        if not jobs:
            logger.warning(f"⚠️ Page navigation: No jobs in user_data for user {user_id}")
            query.edit_message_text("❌ Search results expired. Please start a new search.")
            return MAIN_MENU
        
        context.user_data["page"] = page
        message_text, reply_markup = create_paginated_job_message(jobs, page)
        
        if not message_text or not reply_markup:
            logger.warning(f"⚠️ Page navigation: Empty message for page {page}, user {user_id}")
            return Browse
        
        # Edit message with new page
        try:
            query.edit_message_text(
                text=message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.debug(f"✅ Page {page + 1} displayed for user {user_id}")
        except telegram.error.BadRequest as e:
            if "message is not modified" in str(e).lower():
                logger.debug(f"Page content unchanged for user {user_id}")
            else:
                logger.error(f"❌ Failed to edit page message: {e}")
        except telegram.error.TimedOut:
            logger.warning(f"⏰ Timeout editing page message for user {user_id}")
        
        return Browse
        
    except Exception as e:
        logger.error(f"❌ Page navigation error for user {user_id}: {e}", exc_info=True)
        return Browse


def ignore_callback(update: Update, context: CallbackContext):
    """An empty callback function to handle unclickable buttons."""
    safe_answer_callback_query(update.callback_query)


def close_Browse(update: Update, context: CallbackContext):
    query = update.callback_query
    safe_answer_callback_query(query)
    # Clear search results to free up memory
    context.user_data.pop("jobs", None)
    context.user_data.pop("page", None)
    return main_menu(update, context)


def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("Operation canceled.")
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
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")],
    ]
    text = "🔔 *Alerts Menu*\n\nManage your job alerts here."
    query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return ALERTS_MENU


def add_alert_start(update: Update, context: CallbackContext):
    """Start the process of adding a new alert."""
    query = update.callback_query
    query.answer()
    query.edit_message_text(
        "First, what job keywords should I look for? (e.g. AI Engineer)"
    )
    return ADD_ALERT_KEYWORD


def add_alert_keyword_received(update: Update, context: CallbackContext):
    """Receive the keywords for the new alert."""
    context.user_data["alert_keywords"] = update.message.text
    update.message.reply_text(
        "Got it. Now, what location are you interested in?\n\n"
        "⚠️ Note: Enter only ONE location (e.g., New York, Remote)."
    )
    return ADD_ALERT_LOCATION


def add_alert_location_received(update: Update, context: CallbackContext):
    """Receive the location and ask about preferences for this alert."""
    context.user_data["alert_location"] = update.message.text
    keywords = context.user_data.get("alert_keywords")
    location = update.message.text

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Skip Filters (Any)", callback_data="alert_skip_filters"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Set Filters", callback_data="alert_set_filters"
            )
        ],
    ]

    text = (f"Alert Setup:\n📝 Keywords: '{keywords}'\n"
            f"📍 Location: '{location}'\n\nWould you like to set specific "
            f"filters for this alert?")
    update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ALERT_PREFERENCES


def alert_skip_filters(update: Update, context: CallbackContext):
    """Save alert without specific filters."""
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    keywords = context.user_data.get("alert_keywords")
    location = context.user_data.get("alert_location")

    if is_user_busy(user_id, "alert_setup"):
        query.edit_message_text(
            "⏳ Alert setup already in progress. Please wait..."
        )
        return ALERT_PREFERENCES

    register_user_operation(user_id, "alert_setup")

    query.edit_message_text(
        "📡 Setting up your alert and checking for existing jobs..."
    )

    run_concurrent_operation(
        setup_alert_threaded, query, context, keywords, location, {}
    )

    return MAIN_MENU


def setup_alert_threaded(query, context, keywords, location, prefs):
    """Thread-safe alert setup that doesn't block other users."""
    user_id = query.from_user.id
    chat_id = query.from_user.id

    conn = None
    try:
        if not prefs:
            prefs = {
                "experience": {},
                "job_types": {},
                "date_posted": {},
                "workplace": {},
            }
        filters_json = json.dumps(prefs)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (chat_id, keywords, location, filters) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (chat_id, keywords, location, filters_json),
        )
        alert_id = cursor.fetchone()["id"]
        conn.commit()

        date_posted_value = None
        if prefs["date_posted"]:
            date_posted_value = list(prefs["date_posted"].values())[0]

        workplace_value = None
        if prefs["workplace"]:
            workplace_value = ",".join(prefs["workplace"].values())

        filter_dict = {
            "f_E": ",".join(prefs["experience"].values()),
            "f_JT": ",".join(prefs["job_types"].values()),
            "f_TPR": date_posted_value,
            "f_WT": workplace_value,
        }

        baseline_jobs = scrape_linkedin_with_adaptive_jobbert(
            keywords, location, filter_dict,
            progress_msg=None, user_id=user_id
        )

        for job in baseline_jobs:
            job_id = canonical_link(job["Link"])
            canonical_title = canonical_text(job["Title"])
            canonical_company = canonical_text(job["Company"])
            canonical_location = canonical_text(job.get("Location", ""))
            cursor.execute("""
                INSERT INTO sent_jobs
                (alert_id, chat_id, job_link, job_id, job_title, company,
                 canonical_title, canonical_company, canonical_location, sent_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (alert_id, job_id) DO NOTHING
            """, (
                alert_id, chat_id, job["Link"], job_id, job["Title"],
                job["Company"], canonical_title, canonical_company,
                canonical_location
            ))
        conn.commit()

        logger.info(
            f"Populated {len(baseline_jobs)} baseline jobs for new alert ID "
            f"{alert_id}"
        )

        try:
            message = (
                f"✅ Alert for '{keywords}' in '{location}' has been set and "
                f"is now active. I've recorded {len(baseline_jobs)} existing "
                "jobs so you'll only get notified about truly new "
                "opportunities!"
            )
            query.edit_message_text(message)

            time.sleep(3)
            text, keyboard = make_main_menu(context)
            query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Failed to update alert success message: {e}")

    except Exception as e:
        logger.error(f"Failed to setup alert for user {user_id}: {e}")
        try:
            query.edit_message_text(f"❌ Failed to setup alert: {e!s}")
        except Exception:
            pass
    finally:
        if conn:
            db_pool.return_connection(conn)
        unregister_user_operation(user_id, "alert_setup")


def alert_set_filters(update: Update, context: CallbackContext):
    """Start setting filters specifically for this alert."""
    query = update.callback_query
    query.answer()

    context.user_data["alert_preferences"] = {
        "experience": {},
        "job_types": {},
        "date_posted": {},
        "workplace": {},
    }

    text, keyboard = make_alert_preferences_menu(context)
    query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return ALERT_PREFERENCES


def get_alert_prefs(context: CallbackContext) -> dict:
    """Get alert-specific preferences."""
    if "alert_preferences" not in context.user_data:
        context.user_data["alert_preferences"] = {
            "experience": {},
            "job_types": {},
            "date_posted": {},
            "workplace": {},
        }
    return context.user_data["alert_preferences"]


def make_alert_preferences_menu(
    context: CallbackContext
) -> (str, InlineKeyboardMarkup):
    """Create the alert preferences menu (uses HTML formatting)."""
    prefs = get_alert_prefs(context)
    keywords = context.user_data.get("alert_keywords", "N/A")
    location = context.user_data.get("alert_location", "N/A")

    experience = ", ".join(prefs["experience"].keys()) or "Any"
    job_types = ", ".join(prefs["job_types"].keys()) or "Any"
    date_posted = ""
    if prefs["date_posted"]:
        date_posted = list(prefs["date_posted"].keys())[0]
    else:
        date_posted = "Any"

    workplace = ""
    if prefs["workplace"]:
        workplace = list(prefs["workplace"].keys())[0]
    else:
        workplace = "Any"

    # Escape user-entered data for HTML
    keywords_escaped = html.escape(str(keywords))
    location_escaped = html.escape(str(location))

    text = (
        f"⚙️ <b>Alert Filters</b>\n\n"
        f"📝 <b>Keywords:</b> {keywords_escaped}\n"
        f"📍 <b>Location:</b> {location_escaped}\n\n"
        f"<b>Current Filters:</b>\n"
        f"∙ <b>Date Posted:</b> <code>{html.escape(date_posted)}</code>\n"
        f"∙ <b>Workplace:</b> <code>{html.escape(workplace)}</code>\n"
        f"∙ <b>Experience:</b> <code>{html.escape(experience)}</code>\n"
        f"∙ <b>Job Types:</b> <code>{html.escape(job_types)}</code>"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "🗓️ Date Posted", callback_data="alert_set_date_posted"
            ),
            InlineKeyboardButton(
                "🏢 Workplace", callback_data="alert_set_workplace"
            )
        ],
        [
            InlineKeyboardButton(
                "🎓 Experience", callback_data="alert_set_experience"
            ),
            InlineKeyboardButton(
                "📝 Job Types", callback_data="alert_set_job_types"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Save Alert", callback_data="alert_save_final"
            )
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def alert_save_final(update: Update, context: CallbackContext):
    """Save the alert with the configured preferences and populate baseline."""
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    keywords = context.user_data.get("alert_keywords")
    location = context.user_data.get("alert_location")
    prefs = get_alert_prefs(context)

    if is_user_busy(user_id, "alert_setup"):
        query.edit_message_text(
            "⏳ Alert setup already in progress. Please wait..."
        )
        return ALERT_PREFERENCES

    register_user_operation(user_id, "alert_setup")

    query.edit_message_text(
        "📡 Setting up your alert and checking for existing jobs..."
    )

    run_concurrent_operation(
        setup_alert_threaded, query, context, keywords, location, prefs
    )

    context.user_data.pop("alert_keywords", None)
    context.user_data.pop("alert_location", None)
    context.user_data.pop("alert_preferences", None)

    return MAIN_MENU


def show_alert_date_posted_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = None
    if prefs["date_posted"]:
        selected_value = list(prefs["date_posted"].values())[0]

    text = "🗓️ Choose Date Posted Filter for This Alert"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"alert_dp_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "Clear Filter", callback_data="alert_dp_clear_None"
        )
    ])
    keyboard.append([
        InlineKeyboardButton("✔️ Done", callback_data="alert_dp_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES


def alert_date_posted_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, option_id, option_text = update.callback_query.data.split("_", 3)

    if option_id == "clear" or option_id in prefs["date_posted"].values():
        prefs["date_posted"] = {}
    else:
        prefs["date_posted"] = {option_text: option_id}

    return show_alert_date_posted_menu(update, context)


def alert_preferences_done(update: Update, context: CallbackContext):
    """Return to alert preferences menu from a sub-menu."""
    query = update.callback_query
    query.answer()
    text, keyboard = make_alert_preferences_menu(context)
    query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return ALERT_PREFERENCES


def show_alert_workplace_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_options = prefs["workplace"]

    text = ("🏢 Choose Workplace Types for This Alert\n\n"
           "▫️ Click to select/deselect options\n"
           "▫️ Multiple selections use AND logic\n"
           "▫️ Click 'Done' when finished.")
    
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"alert_wt_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("✔️ Done", callback_data="alert_wt_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES


def alert_workplace_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    _, _, option_id, option_text = update.callback_query.data.split("_", 3)

    selected_dict = prefs["workplace"]

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    return show_alert_workplace_menu(update, context)


def show_alert_multi_select_menu(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)

    if menu_type == "experience":
        title = "🎓 Choose Experience Levels for This Alert"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs["experience"]
        callback_prefix = "alert_exp"
    else:  # job_type
        title = "📝 Choose Job Types for This Alert"
        options_dict = JOB_TYPES
        selected_options = prefs["job_types"]
        callback_prefix = "alert_jt"

    text = (
        f"{title}\n\n"
        "▫️ Click to select/deselect options\n"
        "▫️ Click 'Done' when finished."
    )

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"{callback_prefix}_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("✔️ Done",
                             callback_data=f"{callback_prefix}_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ALERT_PREFERENCES


def alert_toggle_multi_select_option(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    if menu_type == "experience":
        _, _, option_id, option_text = query.data.split("_", 3)
        selected_dict = prefs["experience"]
    else:  # job_type
        _, _, option_id, option_text = query.data.split("_", 3)
        selected_dict = prefs["job_types"]

    if option_id in selected_dict.values():
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        selected_dict[option_text] = option_id

    return show_alert_multi_select_menu(update, context, menu_type)


def my_alerts(update: Update, context: CallbackContext):
    """Display a list of user's alerts with manage options."""
    query = update.callback_query
    safe_answer_callback_query(query)

    chat_id = query.from_user.id

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM alerts WHERE chat_id = %s", (chat_id,)
        )
        alerts = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not alerts:
        text = "📋 *Your Alerts*\n\nYou have no alerts set up yet."
        keyboard = [
            [
                InlineKeyboardButton(
                    "➕ Add New Alert", callback_data="add_alert"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Alerts Menu", callback_data="alerts_menu"
                )
            ],
        ]
        if query:
            try:
                query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            except telegram.error.BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise e
        else:
            update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        return MY_ALERTS

    text = f"📋 *Your Alerts* ({len(alerts)} active)\n\n" \
           "Click on any alert to manage it:"
    keyboard = []

    for alert in alerts:
        status_icon = "🟢" if alert["is_active"] else "🔴"
        alert_line = f"{status_icon} {alert['keywords']} • {alert['location']}"
        keyboard.append([
            InlineKeyboardButton(
                alert_line, callback_data=f"view_alert_{alert['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("➕ Add New Alert", callback_data="add_alert")
    ])
    keyboard.append([
        InlineKeyboardButton("🔙 Back to Alerts Menu",
                             callback_data="alerts_menu")
    ])

    if query:
        try:
            query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except telegram.error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise e
    else:
        update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    return MY_ALERTS


def view_alert_details(update: Update, context: CallbackContext):
    """Show detailed view of a specific alert with management options."""
    query = update.callback_query
    query.answer()

    _, _, alert_id = query.data.split("_")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM alerts WHERE id = %s", (alert_id,)
        )
        alert = cursor.fetchone()

        if not alert:
            query.edit_message_text("❌ Alert not found.")
            return MY_ALERTS

        filters = json.loads(alert["filters"])
        experience = ", ".join(filters["experience"].keys()) or "Any"
        job_types = ", ".join(filters["job_types"].keys()) or "Any"
        date_posted = "Any"
        if filters["date_posted"]:
            date_posted = list(filters["date_posted"].keys())[0]
        workplace = "Any"
        if filters["workplace"]:
            workplace = list(filters["workplace"].keys())[0]

        cursor.execute(
            "SELECT COUNT(*) FROM sent_jobs WHERE alert_id = %s", (alert_id,)
        )
        sent_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT timezone FROM user_settings WHERE chat_id = %s",
            (query.from_user.id,)
        )
        tz_row = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    user_timezone_str = tz_row["timezone"] if tz_row and tz_row["timezone"] \
        else "UTC"

    status_icon = "🟢" if alert["is_active"] else "🔴"
    status_text = "Active" if alert["is_active"] else "Paused"

    last_checked_val = alert["last_checked"]
    last_checked_display = "Never"
    if last_checked_val:
        try:
            # Handle both datetime objects (PostgreSQL) and strings (SQLite)
            if isinstance(last_checked_val, datetime):
                utc_dt = last_checked_val.replace(tzinfo=pytz.utc) if last_checked_val.tzinfo is None else last_checked_val
            else:
                utc_dt = datetime.strptime(
                    str(last_checked_val).split(".")[0], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=pytz.utc)

            user_tz = pytz.timezone(user_timezone_str)
            local_dt = utc_dt.astimezone(user_tz)

            last_checked_display = local_dt.strftime("%Y-%m-%d %H:%M")
            if user_timezone_str != "UTC":
                tz_name = user_timezone_str.split('/')[-1].replace('_', ' ')
                last_checked_display += f" ({tz_name})"
        except (ValueError, pytz.UnknownTimeZoneError):
            last_checked_display = last_checked_utc_str[:16] + " (UTC)"

    # Use HTML to safely display user-entered keywords and location
    keywords_escaped = html.escape(alert['keywords'])
    location_escaped = html.escape(alert['location'])
    experience_escaped = html.escape(experience)
    job_types_escaped = html.escape(job_types)
    date_posted_escaped = html.escape(date_posted)
    workplace_escaped = html.escape(workplace)

    text = (
        f"🔔 <b>Alert Details</b>\n\n"
        f"📝 <b>Keywords:</b> {keywords_escaped}\n"
        f"📍 <b>Location:</b> {location_escaped}\n"
        f"📊 <b>Status:</b> {status_icon} {status_text}\n"
        f"📬 <b>Jobs Sent:</b> {sent_count}\n\n"
        f"<b>Current Filters:</b>\n"
        f"∙ <b>Date Posted:</b> <code>{date_posted_escaped}</code>\n"
        f"∙ <b>Workplace:</b> <code>{workplace_escaped}</code>\n"
        f"∙ <b>Experience:</b> <code>{experience_escaped}</code>\n"
        f"∙ <b>Job Types:</b> <code>{job_types_escaped}</code>\n\n"
        f"🕒 Last checked: {last_checked_display}"
    )

    action_text = "⏸️ Pause Alert" if alert["is_active"] else "▶️ Resume Alert"
    action_cb = f"pause_alert_{alert_id}" if alert["is_active"] \
        else f"resume_alert_{alert_id}"

    keyboard = [
        [InlineKeyboardButton(action_text, callback_data=action_cb)],
        [
            InlineKeyboardButton(
                "⚙️ Edit Preferences", callback_data=f"edit_alert_{alert_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑️ Delete Alert",
                callback_data=f"delete_alert_start_{alert_id}"
            )
        ],
        [InlineKeyboardButton("⬅️ Back to Alerts", callback_data="my_alerts")],
    ]

    try:
        query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e

    return MY_ALERTS


def toggle_alert_status(update: Update, context: CallbackContext):
    """Pause or resume an alert."""
    query = update.callback_query
    action, _, alert_id = query.data.split("_")
    new_status = 0 if action == "pause" else 1

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE alerts SET is_active = %s WHERE id = %s", (new_status, alert_id)
        )
        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    query.answer(f"Alert {'paused' if new_status == 0 else 'resumed'}.")
    return my_alerts(update, context)


def delete_alert_start(update: Update, context: CallbackContext):
    """Ask for confirmation before deleting an alert."""
    query = update.callback_query
    _, _, _, alert_id = query.data.split("_")

    text = "Are you sure you want to permanently delete this alert?"
    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, Delete", callback_data=f"delete_alert_confirm_{alert_id}"
            ),
            InlineKeyboardButton("No, Cancel", callback_data="my_alerts"),
        ],
    ]
    query.answer()
    try:
        query.edit_message_text(text,
                                reply_markup=InlineKeyboardMarkup(keyboard))
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e
    return MY_ALERTS


def delete_alert_confirm(update: Update, context: CallbackContext):
    """Delete the alert and all associated sent jobs from the database."""
    query = update.callback_query
    _, _, _, alert_id = query.data.split("_")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM sent_jobs WHERE alert_id = %s", (alert_id,))
        cursor.execute("DELETE FROM alerts WHERE id = %s", (alert_id,))

        conn.commit()
    finally:
        if conn:
            db_pool.return_connection(conn)

    query.answer("Alert and all associated job records deleted.")
    return my_alerts(update, context)


def edit_alert_start(update: Update, context: CallbackContext):
    """Start editing an existing alert's preferences."""
    query = update.callback_query
    query.answer()

    _, _, alert_id = query.data.split("_")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM alerts WHERE id = %s", (alert_id,)
        )
        alert = cursor.fetchone()
    finally:
        if conn:
            db_pool.return_connection(conn)

    if not alert:
        query.edit_message_text("❌ Alert not found.")
        return MY_ALERTS

    context.user_data["editing_alert_id"] = alert_id
    context.user_data["alert_keywords"] = alert["keywords"]
    context.user_data["alert_location"] = alert["location"]

    existing_prefs = json.loads(alert["filters"])
    context.user_data["alert_preferences"] = existing_prefs

    text, keyboard = make_edit_alert_preferences_menu(context)
    query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return EDIT_ALERT_PREFERENCES


def make_edit_alert_preferences_menu(
    context: CallbackContext
) -> (str, InlineKeyboardMarkup):
    """Create the edit alert preferences menu (uses HTML formatting)."""
    prefs = get_alert_prefs(context)
    keywords = context.user_data.get("alert_keywords", "N/A")
    location = context.user_data.get("alert_location", "N/A")

    experience = ", ".join(prefs["experience"].keys()) or "Any"
    job_types = ", ".join(prefs["job_types"].keys()) or "Any"
    date_posted = "Any"
    if prefs["date_posted"]:
        date_posted = list(prefs["date_posted"].keys())[0]
    workplace = "Any"
    if prefs["workplace"]:
        workplace = list(prefs["workplace"].keys())[0]

    # Escape user-entered data for HTML
    keywords_escaped = html.escape(str(keywords))
    location_escaped = html.escape(str(location))

    text = (
        f"⚙️ <b>Edit Alert Preferences</b>\n\n"
        f"📝 <b>Keywords:</b> {keywords_escaped}\n"
        f"📍 <b>Location:</b> {location_escaped}\n\n"
        f"<b>Current Filters:</b>\n"
        f"∙ <b>Date Posted:</b> <code>{html.escape(date_posted)}</code>\n"
        f"∙ <b>Workplace:</b> <code>{html.escape(workplace)}</code>\n"
        f"∙ <b>Experience:</b> <code>{html.escape(experience)}</code>\n"
        f"∙ <b>Job Types:</b> <code>{html.escape(job_types)}</code>"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "🗓️ Date Posted", callback_data="edit_alert_set_date_posted"
            ),
            InlineKeyboardButton(
                "🏢 Workplace", callback_data="edit_alert_set_workplace"
            )
        ],
        [
            InlineKeyboardButton(
                "🎓 Experience", callback_data="edit_alert_set_experience"
            ),
            InlineKeyboardButton(
                "📝 Job Types", callback_data="edit_alert_set_job_types"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Save Changes", callback_data="edit_alert_save_final"
            )
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="my_alerts")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def edit_alert_save_final(update: Update, context: CallbackContext):
    """Save the updated alert preferences and refresh the baseline."""
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    alert_id = context.user_data.get("editing_alert_id")
    keywords = context.user_data.get("alert_keywords")
    location = context.user_data.get("alert_location")
    prefs = get_alert_prefs(context)

    if is_user_busy(user_id, "alert_update"):
        query.edit_message_text(
            "⏳ Alert update already in progress. Please wait..."
        )
        return EDIT_ALERT_PREFERENCES

    register_user_operation(user_id, "alert_update")

    query.edit_message_text(
        "📡 Updating your alert and re-scanning for existing jobs with "
        "the new filters..."
    )

    run_concurrent_operation(
        update_alert_baseline_threaded, query, context, alert_id,
        keywords, location, prefs
    )

    context.user_data.pop("editing_alert_id", None)
    context.user_data.pop("alert_keywords", None)
    context.user_data.pop("alert_location", None)
    context.user_data.pop("alert_preferences", None)

    return EDIT_ALERT_PREFERENCES


def update_alert_baseline_threaded(
    query, context, alert_id, keywords, location, prefs
):
    """Thread-safe alert update that refreshes the baseline jobs."""
    user_id = query.from_user.id
    chat_id = query.from_user.id
    conn = None

    try:
        # 1. Update the filters in the database
        filters_json = json.dumps(prefs)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE alerts SET filters = %s WHERE id = %s",
            (filters_json, alert_id),
        )
        conn.commit()

        # 2. Scrape for jobs with new filters
        date_posted_value = None
        if prefs.get("date_posted"):
            date_posted_value = list(prefs["date_posted"].values())[0]

        workplace_value = None
        if prefs.get("workplace"):
            workplace_value = ",".join(prefs["workplace"].values())

        filter_dict = {
            "f_E": ",".join(prefs.get("experience", {}).values()),
            "f_JT": ",".join(prefs.get("job_types", {}).values()),
            "f_TPR": date_posted_value,
            "f_WT": workplace_value,
        }

        baseline_jobs = scrape_linkedin_with_adaptive_jobbert(
            keywords, location, filter_dict, progress_msg=None,
            user_id=user_id
        )

        cursor.execute(
            "SELECT job_id FROM sent_jobs WHERE chat_id = %s",
            (chat_id,)
        )
        sent_jobs = cursor.fetchall()
        sent_job_ids = {row["job_id"] for row in sent_jobs}

        new_jobs_to_insert = []
        for job in baseline_jobs:
            job_id = canonical_link(job["Link"])
            canonical_title = canonical_text(job["Title"])
            canonical_company = canonical_text(job["Company"])

            canonical_location = canonical_text(job.get("Location", ""))

            is_duplicate = job_id in sent_job_ids

            if not is_duplicate:
                new_jobs_to_insert.append(
                    (
                        alert_id, chat_id, job["Link"], job_id,
                        job["Title"], job["Company"], canonical_title,
                        canonical_company, canonical_location
                    )
                )

        if new_jobs_to_insert:
            for job_data in new_jobs_to_insert:
                cursor.execute("""
                    INSERT INTO sent_jobs
                    (alert_id, chat_id, job_link, job_id, job_title,
                     company, canonical_title, canonical_company,
                     canonical_location, sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (alert_id, job_id) DO NOTHING
                """, job_data)
            conn.commit()

        logger.info(
            "Populated %s new baseline jobs for updated alert ID %s",
            len(new_jobs_to_insert), alert_id
        )

        try:
            message = (
                f"✅ Alert for '{keywords}' in '{location}' has been "
                f"updated. I've recorded {len(new_jobs_to_insert)} new "
                "existing jobs based on your new preferences. You'll only get "
                "notified about truly new opportunities!"
            )
            keyboard = [[InlineKeyboardButton(
                "⬅️ Back to Alerts", callback_data="my_alerts"
            )]]
            query.edit_message_text(
                message, reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except Exception as e:
            logger.error(f"Failed to update alert success message: {e}")

    except Exception as e:
        logger.error(f"Failed to update alert for user {user_id}: {e}")
        try:
            query.edit_message_text(f"❌ Failed to update alert: {e!s}")
        except Exception:
            pass
    finally:
        if conn:
            db_pool.return_connection(conn)
        unregister_user_operation(user_id, "alert_update")


def edit_alert_preferences_done(update: Update, context: CallbackContext):
    """Return to edit alert preferences menu from a sub-menu."""
    query = update.callback_query
    query.answer()
    text, keyboard = make_edit_alert_preferences_menu(context)
    query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    return EDIT_ALERT_PREFERENCES


def show_edit_alert_date_posted_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_value = None
    if prefs["date_posted"]:
        selected_value = list(prefs["date_posted"].values())[0]

    text = "🗓️ Choose Date Posted Filter for This Alert"
    keyboard = []
    for option_text, option_id in DATE_POSTED_OPTIONS.items():
        is_selected = selected_value == option_id
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"edit_alert_dp_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "Clear Filter", callback_data="edit_alert_dp_clear_None"
        )
    ])
    keyboard.append([
        InlineKeyboardButton("✔️ Done", callback_data="edit_alert_dp_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES


def edit_alert_date_posted_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    data_parts = update.callback_query.data.split("_", 4)
    option_id = data_parts[3]
    option_text = data_parts[4]

    if option_id == "clear" or option_id in prefs["date_posted"].values():
        prefs["date_posted"] = {}
    else:
        prefs["date_posted"] = {option_text: option_id}

    return show_edit_alert_date_posted_menu(update, context)


def show_edit_alert_workplace_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)
    selected_options = prefs["workplace"]

    text = ("🏢 Choose Workplace Types for This Alert\n\n"
           "▫️ Click to select/deselect options\n"
           "▫️ Multiple selections use AND logic\n"
           "▫️ Click 'Done' when finished.")
    
    keyboard = []
    for option_text, option_id in WORKPLACE_TYPES.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"edit_alert_wt_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("✔️ Done", callback_data="edit_alert_wt_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES


def edit_alert_workplace_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    data_parts = update.callback_query.data.split("_", 4)
    option_id = data_parts[3]
    option_text = data_parts[4]

    selected_dict = prefs["workplace"]

    if option_id in selected_dict.values():
        # Deselect: find key by value and delete
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        # Select
        selected_dict[option_text] = option_id

    return show_edit_alert_workplace_menu(update, context)


def show_edit_alert_multi_select_menu(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()

    prefs = get_alert_prefs(context)

    if menu_type == "experience":
        title = "🎓 Choose Experience Levels for This Alert"
        options_dict = EXPERIENCE_LEVELS
        selected_options = prefs["experience"]
        callback_prefix = "edit_alert_exp"
    else:  # job_type
        title = "📝 Choose Job Types for This Alert"
        options_dict = JOB_TYPES
        selected_options = prefs["job_types"]
        callback_prefix = "edit_alert_jt"

    text = (f"{title}\n\n"
            "▫️ Click to select/deselect options\n"
            "▫️ Click 'Done' when finished.")

    keyboard = []
    for option_text, option_id in options_dict.items():
        is_selected = option_id in selected_options.values()
        display_text = f"✅ {option_text}" if is_selected else option_text
        keyboard.append([
            InlineKeyboardButton(
                display_text,
                callback_data=f"{callback_prefix}_{option_id}_{option_text}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("✔️ Done",
                             callback_data=f"{callback_prefix}_done")
    ])
    query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_ALERT_PREFERENCES


def edit_alert_toggle_multi_select_option(
    update: Update, context: CallbackContext, menu_type: str
):
    query = update.callback_query
    query.answer()
    prefs = get_alert_prefs(context)

    if menu_type == "experience":
        _, _, _, option_id, option_text = query.data.split("_", 4)
        selected_dict = prefs["experience"]
    else:  # job_type
        _, _, _, option_id, option_text = query.data.split("_", 4)
        selected_dict = prefs["job_types"]

    if option_id in selected_dict.values():
        key_to_del = next(
            (k for k, v in selected_dict.items() if v == option_id), None
        )
        if key_to_del:
            del selected_dict[key_to_del]
    else:
        selected_dict[option_text] = option_id

    return show_edit_alert_multi_select_menu(update, context, menu_type)


def check_all_alerts(bot: Bot):
    """Scheduled job to check all active alerts with robust deduplication."""
    logger.info("Scheduler running: Checking all active alerts sequentially...")

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM alerts WHERE is_active = 1"
        )
        active_alerts = cursor.fetchall()
    finally:
        if conn:
            db_pool.return_connection(conn)

    # Process alerts one by one instead of in parallel to save memory
    for alert in active_alerts:
        try:
            check_single_alert(alert, bot)
        except Exception:
            # Log the error for the specific alert that failed
            logger.exception(f"Alert check failed for alert ID: {alert['id']}")

    logger.info("Scheduler finished checking alerts.")


def check_single_alert(alert, bot: Bot):
    """Check a single alert with enhanced error handling and memory management."""
    conn = None
    import gc
    start_time = time.time()
    start_memory = get_memory_usage()
    
    try:
        logger.info(
            "Checking alert ID %s for chat ID %s...",
            alert["id"], alert["chat_id"]
        )
        filters = json.loads(alert["filters"])

        date_posted_value = None
        if filters["date_posted"]:
            date_posted_value = next(iter(filters["date_posted"].values()))

        workplace_value = None
        if filters["workplace"]:
            workplace_value = ",".join(filters["workplace"].values())

        filter_dict = {
            "f_E": ",".join(filters["experience"].values()),
            "f_JT": ",".join(filters["job_types"].values()),
            "f_TPR": date_posted_value,
            "f_WT": workplace_value,
        }

        scheduler_user_id = f"scheduler_{alert['id']}"
        
        # Acquire the alert AI lock with timeout to prevent deadlocks
        logger.info(f"Background alert {alert['id']} waiting for alert AI lock...")
        lock_acquired = alert_ai_lock.acquire(timeout=300)  # 5 minute timeout
        if not lock_acquired:
            logger.error(f"Failed to acquire alert AI lock for alert {alert['id']} after 5 minutes")
            return
            
        try:
            logger.info(f"Alert AI lock acquired for background alert {alert['id']}.")
            found_jobs = scrape_linkedin_with_adaptive_jobbert(
                alert["keywords"], alert["location"], filter_dict,
                progress_msg=None, user_id=scheduler_user_id,
            )
        finally:
            alert_ai_lock.release()
            logger.info(f"Alert AI lock released for background alert {alert['id']}.")
            gc.collect()  # Clean up memory after processing

        conn = get_db_connection()
        cursor = conn.cursor()

        last_checked = None
        if alert["last_checked"]:
            try:
                # Handle both datetime objects (PostgreSQL) and strings (SQLite)
                if isinstance(alert["last_checked"], datetime):
                    last_checked = alert["last_checked"].replace(tzinfo=pytz.utc) if alert["last_checked"].tzinfo is None else alert["last_checked"]
                else:
                    last_checked = datetime.strptime(
                        str(alert["last_checked"]).split(".")[0], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=pytz.utc)
            except ValueError:
                logger.warning(
                    "Could not parse last_checked timestamp for alert %s",
                    alert["id"]
                )

        jobs_to_send = []
        duped = 0
        recency_skipped = 0
        for job in found_jobs:
            job_id = canonical_link(job["Link"])
            canonical_title = canonical_text(job["Title"])
            canonical_company = canonical_text(job["Company"])
            canonical_location = canonical_text(job.get("Location", ""))

            # Dedup paths within 14 days:
            #   1) same job_id (most precise)
            #   2) same title + exact canonical_company (catches short names like
            #      KLA, SAP, BMW, IBM that the first-word path filters out)
            #   3) same title + first word of canonical_company (>= 4 chars) —
            #      catches "Scalable Capital" / "Scalable GmbH" / "Scalable Press"
            cursor.execute(
                """SELECT 1 FROM sent_jobs WHERE
                   chat_id = %s AND (
                       job_id = %s OR
                       (canonical_title = %s
                        AND canonical_title != ''
                        AND canonical_company != ''
                        AND sent_at > NOW() - INTERVAL '14 days'
                        AND (
                            canonical_company = %s
                            OR (length(split_part(canonical_company, ' ', 1)) >= 4
                                AND split_part(canonical_company, ' ', 1) = split_part(%s, ' ', 1))
                        ))
                   )
                   LIMIT 1""",
                (alert["chat_id"], job_id, canonical_title, canonical_company, canonical_company)
            )
            is_duplicate = cursor.fetchone() is not None

            if is_duplicate:
                duped += 1
                continue

            if last_checked:
                try:
                    job_posted_time = parse_date_posted_to_datetime(
                        job["Date Posted"]
                    )
                    # 48h buffer: day-granularity dates from JobQuest need a wide
                    # window; the sent_jobs DB check above is the real dedup guard
                    if job_posted_time < last_checked - timedelta(hours=48):
                        recency_skipped += 1
                        continue
                except Exception as e:
                    logger.warning(
                        f"Could not parse job date '{job['Date Posted']}': {e}"
                    )

            job_with_meta = {
                **job,
                "_job_id": job_id,
                "_canonical_title": canonical_title,
                "_canonical_company": canonical_company,
                "_canonical_location": canonical_location
            }
            jobs_to_send.append(job_with_meta)

        logger.info(
            f"Alert {alert['id']}: {len(found_jobs)} scraped, "
            f"{duped} duped, {recency_skipped} old, "
            f"{len(jobs_to_send)} to send"
        )

        # Feedback to the adaptive scrape loop: if a full scrape still yields many
        # new jobs, deepen next cycle; if saturated by dupes, ease off.
        try:
            adapt_scrape_depth(
                alert["keywords"], alert["location"],
                len(found_jobs), len(jobs_to_send)
            )
        except Exception:
            pass

        new_jobs_to_insert_db = []
        for job in jobs_to_send:
            title = html.escape(job["Title"])
            company = html.escape(job["Company"]) if job["Company"] else ""
            location = html.escape(job["Location"]) if job["Location"] else ""
            date_posted = html.escape(job["Date Posted"]) if job["Date Posted"] and job["Date Posted"] != "N/A" else ""
            keywords = html.escape(alert["keywords"])
            alert_location = html.escape(alert["location"])

            if company and location:
                company_line = f"<i>{company}</i> - {location}"
            elif company:
                company_line = f"<i>{company}</i>"
            elif location:
                company_line = location
            else:
                company_line = ""

            date_line = f"Posted: {date_posted}\n\n" if date_posted else "\n"

            message = (
                "🔔 <b>New Job Alert!</b>\n\n"
                f"<b>{title}</b>\n"
                f"{company_line}\n"
                f"{date_line}"
                f"From your alert for: <b>{keywords}</b> in "
                f"<b>{alert_location}</b>"
            )
            # Create a unique job identifier for this alert
            # Use hash if job_id is too long for Telegram's 64-byte callback_data limit
            job_id_for_callback = job['_job_id']
            if len(job_id_for_callback) > 40:  # Leave room for "save_job_" prefix and alert_id
                job_id_for_callback = hashlib.md5(job_id_for_callback.encode()).hexdigest()[:16]

            job_unique_id = f"{alert['id']}_{job_id_for_callback}"

            # Store the mapping from hashed ID to original job_id for callback lookup
            job["_job_id_for_callback"] = job_id_for_callback

            # Check if job is already saved
            cursor.execute(
                "SELECT 1 FROM saved_jobs WHERE chat_id = %s AND job_link = %s",
                (alert["chat_id"], job["Link"])
            )
            is_saved = cursor.fetchone() is not None

            # Show "✅ Saved" if already saved, otherwise "💾 Save"
            save_button_text = "✅ Saved" if is_saved else "💾 Save"
            save_callback_data = f"unsave_from_alert_{job_unique_id}" if is_saved else f"save_job_{job_unique_id}"

            keyboard = [
                [
                    InlineKeyboardButton("View Job", url=job["Link"]),
                    InlineKeyboardButton(
                        save_button_text, callback_data=save_callback_data
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📋 My Alerts", callback_data="my_alerts"
                    ),
                    InlineKeyboardButton("🏠 Start", callback_data="start_command")
                ]
            ]

            try:
                bot.send_message(
                    chat_id=alert["chat_id"],
                    text=message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML,
                )

                # Use pre-computed canonical data
                new_jobs_to_insert_db.append(
                    (
                        alert["id"], alert["chat_id"], job["Link"],
                        job["_job_id"], job["Title"], job["Company"],
                        job["_canonical_title"], job["_canonical_company"],
                        job["_canonical_location"]
                    )
                )

                time.sleep(1.2)
            except telegram.error.BadRequest:
                logger.exception(
                    "Failed to send alert to %s", alert["chat_id"]
                )
            except Exception:
                logger.exception(
                    "An unexpected error occurred sending to %s",
                    alert["chat_id"]
                )

        if new_jobs_to_insert_db:
            for job_data in new_jobs_to_insert_db:
                cursor.execute("""
                    INSERT INTO sent_jobs
                    (alert_id, chat_id, job_link, job_id, job_title,
                     company, canonical_title, canonical_company,
                     canonical_location, sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (alert_id, job_id) DO NOTHING
                """, job_data)
            logger.info(
                "Sent and recorded %s new job(s) for alert ID %s.",
                len(new_jobs_to_insert_db), alert["id"]
            )

        # Cache job details for saving later (using callback-safe job_id)
        # MOVED BEFORE sending messages to fix race condition
        job_cache_data = []
        for job in jobs_to_send:
            job_cache_data.append((
                alert["id"], job["_job_id_for_callback"], job["Link"], job["Title"],
                job["Company"], job["Location"], job["Date Posted"]
            ))

        if job_cache_data:
            for cache_data in job_cache_data:
                cursor.execute("""
                    INSERT INTO job_details_cache
                    (alert_id, job_id, job_link, job_title, company, location,
                     date_posted, cached_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (alert_id, job_id) DO UPDATE SET
                        job_link = EXCLUDED.job_link,
                        job_title = EXCLUDED.job_title,
                        company = EXCLUDED.company,
                        location = EXCLUDED.location,
                        date_posted = EXCLUDED.date_posted,
                        cached_at = NOW()
                """, cache_data)
        
        # Commit cache BEFORE sending messages to ensure it's available for save button
        conn.commit()

        cursor.execute(
            "UPDATE alerts SET last_checked = (NOW() AT TIME ZONE 'UTC') WHERE id = %s",
            (alert["id"],)
        )
        conn.commit()

        time.sleep(2)

    except MemoryError as e:
        logger.critical(f"Memory error checking alert {alert['id']}: {e}")
        gc.collect()
    except Exception as e:
        logger.exception(f"Failed to check alert {alert['id']}: {e}")
    finally:
        if conn:
            try:
                db_pool.return_connection(conn)
            except Exception as e:
                logger.error(f"Error returning database connection to pool: {e}")
        gc.collect()  # Always clean up memory after alert check
        
        # Log alert completion with key metrics
        end_time = time.time()
        end_memory = get_memory_usage()
        processing_time = end_time - start_time
        memory_delta = end_memory - start_memory
        
        alert_logger.info(
            f"Alert {alert['id']} | "
            f"Time: {processing_time:.1f}s | "
            f"Memory: {end_memory:.1f}MB ({memory_delta:+.1f}) | "
            f"Status: {'SUCCESS' if 'found_jobs' in locals() else 'ERROR'}"
        )


# --- New Timezone Functions ---
def set_timezone_start(update: Update, context: CallbackContext):
    """Start the timezone setting process."""
    query = update.callback_query
    query.answer()
    text = (
        "Please send me your timezone identifier.\n\n"
        "You can find your identifier on this list: "
        "[List of tz database time zones]"
        "(https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)\n\n"
        "For example: `America/New_York`, `Europe/London`, `Asia/Kolkata`"
    )
    query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
    )
    return SET_TIMEZONE


def timezone_received(update: Update, context: CallbackContext):
    """Validate and save the user's timezone."""
    user_timezone = update.message.text.strip()
    try:
        pytz.timezone(user_timezone)

        chat_id = update.message.chat_id
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO user_settings (chat_id, timezone) 
                VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET timezone = EXCLUDED.timezone""",
                (chat_id, user_timezone),
            )
            conn.commit()
        finally:
            if conn:
                db_pool.return_connection(conn)

        update.message.reply_text(
            f"✅ Timezone set to `{user_timezone}`.",
            parse_mode=ParseMode.MARKDOWN
        )

        text, keyboard = make_preferences_menu(context, chat_id)
        update.message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )
        return PREFERENCES_MENU

    except pytz.UnknownTimeZoneError:
        update.message.reply_text(
            "❌ Invalid timezone identifier. Please check the list and try again.\n"
            "Example: Europe/Berlin"
        )
        return SET_TIMEZONE


def main():
    import argparse
    import gc

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        
        # Shutdown scheduler
        if 'scheduler' in locals():
            try:
                scheduler.shutdown(wait=False)
                logger.info("✅ Scheduler shutdown complete")
            except Exception as e:
                logger.error(f"Error shutting down scheduler: {e}")
        
        # Shutdown thread pool
        if 'executor' in globals():
            try:
                executor.shutdown(wait=True, timeout=30)
                logger.info("✅ Thread pool shutdown complete")
            except Exception as e:
                logger.error(f"Error shutting down thread pool: {e}")
        
        # Cleanup models
        try:
            unload_jobbert_model()
            logger.info("✅ Model cleanup complete")
        except Exception as e:
            logger.error(f"Error during model cleanup: {e}")
        
        # Final memory cleanup
        try:
            force_memory_cleanup()
            logger.info("✅ Final memory cleanup complete")
        except Exception as e:
            logger.error(f"Error during final cleanup: {e}")
        
        logger.info("🏁 Graceful shutdown complete")
        sys.exit(0)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="JobQuestTG Bot")
    parser.add_argument("--migrate-only", action="store_true",
                        help="Run DB migrations then exit")
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("FATAL: TELEGRAM_BOT_TOKEN not found.")
        return

    try:
        init_db()
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        return

    if args.migrate_only:
        print("✅ Migration completed - exiting as requested.")
        print("🚀 You can now start the bot normally with: python bot.py")
        sys.exit(0)
        
    # Force garbage collection to free memory
    gc.collect()

    scheduler = BackgroundScheduler(timezone="UTC")

    def memory_aware_check_alerts(bot_instance):
        """Memory-aware alert checking with adaptive intervals and watchdog tracking"""
        if shutdown_requested.is_set():
            logger.info("🛑 Shutdown requested, skipping alert check")
            return
        
        start_time = time.time()
        
        try:
            with scheduler_lock:
                current_memory = get_memory_usage()
                logger.info(f"🔍 Starting alert check cycle. Memory: {current_memory:.1f} MB")
                
                # Update watchdog - alert check started
                with scheduler_watchdog_lock:
                    last_scheduler_run['alert_check'] = time.time()
                
                # Skip if memory is too high
                if current_memory > MAX_MEMORY_MB:
                    logger.warning(f"⚠️ Skipping alert check due to high memory: {current_memory:.1f} MB")
                    # Don't acquire memory_cleanup_lock here - we hold scheduler_lock
                    # This prevents potential deadlock
                    force_memory_cleanup()
                    return
                
                # Run the actual alert check
                try:
                    check_all_alerts(bot_instance)
                    logger.info(f"✅ Alert check completed in {time.time() - start_time:.1f}s")
                    crash_monitor.record_success()  # Record successful alert check
                except Exception as check_error:
                    logger.error(f"❌ Alert check failed: {check_error}", exc_info=True)
                    
                    # Record failure in crash monitor
                    should_restart = crash_monitor.record_failure(
                        f"Alert check failed: {check_error}", 
                        is_critical=isinstance(check_error, (MemoryError, SystemError))
                    )
                    
                    # Try to recover from potential deadlocks
                    lock_status = model_lock.get_status()
                    if lock_status.get('locked') and lock_status.get('held_duration', 0) > 300:
                        logger.critical(f"🚨 Detected stuck lock during alert check - forcing recovery")
                        model_lock.force_release()
                    
                    # Check if we should restart
                    if should_restart and AUTO_RESTART_ON_CRITICAL:
                        logger.critical("🔄 Too many alert check failures - triggering restart!")
                        trigger_self_restart()
                        return
                
                # Cleanup after alert check
                # Don't acquire memory_cleanup_lock here - we hold scheduler_lock
                force_memory_cleanup()

                # Unload model if needed
                if should_unload_model():
                    unload_jobbert_model()
                    
        except Exception as e:
            logger.error(f"❌ Memory-aware alert check failed: {e}", exc_info=True)
            logger.error(f"🔍 Alert check failed after {time.time() - start_time:.1f}s")
            try:
                # Don't acquire memory_cleanup_lock - just run cleanup directly
                force_memory_cleanup()
                # Attempt deadlock recovery
                lock_status = model_lock.get_status()
                if lock_status.get('locked'):
                    logger.warning(f"⚠️ Lock still held after failure - attempting recovery")
                    model_lock.force_release()
            except Exception as cleanup_e:
                logger.error(f"❌ Failed to cleanup after error: {cleanup_e}")

    def periodic_memory_cleanup():
        """Periodic memory cleanup job with watchdog tracking and crash detection"""
        if shutdown_requested.is_set():
            logger.info("🛑 Shutdown requested, skipping memory cleanup")
            return
        
        try:
            # Update watchdog
            with scheduler_watchdog_lock:
                last_scheduler_run['memory_cleanup'] = time.time()
            
            with memory_cleanup_lock:
                current_memory = get_memory_usage()
                logger.info(f"🧹 Periodic cleanup. Memory: {current_memory:.1f} MB")
                
                # Check for critical memory levels
                if current_memory > MAX_MEMORY_MB * 0.95:
                    logger.critical(f"🚨 CRITICAL MEMORY: {current_memory:.1f}MB - initiating emergency recovery!")
                    if not emergency_memory_recovery():
                        need_restart = crash_monitor.record_memory_warning(current_memory)
                        if need_restart and AUTO_RESTART_ON_CRITICAL:
                            logger.critical("🔄 Emergency recovery failed - triggering restart!")
                            trigger_self_restart()
                            return
                elif current_memory > MAX_MEMORY_MB * 0.7:  # 70% of max
                    logger.info("Running periodic memory cleanup...")
                    force_memory_cleanup()
                    
                    # Unload model if memory is still high
                    if get_memory_usage() > MAX_MEMORY_MB * 0.8:
                        unload_jobbert_model()
                
                # Reset memory warnings if we're in good shape
                if get_memory_usage() < MAX_MEMORY_MB * 0.6:
                    crash_monitor.reset_memory_warnings()
                        
        except Exception as e:
            logger.error(f"❌ Periodic cleanup failed: {e}", exc_info=True)
            crash_monitor.record_failure(f"Periodic cleanup failed: {e}", is_critical=False)
            # Don't retry cleanup here to avoid infinite loops
    
    def scheduler_watchdog():
        """Monitor scheduler health and detect if jobs stop running"""
        if shutdown_requested.is_set():
            return
        
        try:
            current_time = time.time()
            
            with scheduler_watchdog_lock:
                alert_last_run = last_scheduler_run.get('alert_check')
                cleanup_last_run = last_scheduler_run.get('memory_cleanup')
            
            time_since_alert = 0
            time_since_cleanup = 0
            
            # Check alert checker (should run every 30 minutes)
            if alert_last_run:
                time_since_alert = current_time - alert_last_run
                if time_since_alert > 3600:  # 1 hour without running
                    logger.critical(f"🚨 SCHEDULER ALERT: Alert checker hasn't run in {time_since_alert/60:.1f} minutes!")
                    logger.critical(f"🔍 Last successful alert check: {time_since_alert/60:.1f} minutes ago")
                    logger.critical(f"💡 This may indicate scheduler failure or deadlock")
                    
                    # Record failure in crash monitor
                    should_restart = crash_monitor.record_failure(
                        f"Alert checker stuck for {time_since_alert/60:.1f} minutes", 
                        is_critical=True
                    )
                    
                    # Try to diagnose the issue
                    lock_status = model_lock.get_status()
                    if lock_status.get('locked'):
                        logger.critical(f"🚨 Model lock is stuck! Held by: {lock_status['holder']} for {lock_status['held_duration']:.1f}s")
                        logger.critical(f"🆘 Attempting automatic recovery...")
                        try:
                            model_lock.force_release()
                            logger.info(f"✅ Lock forcibly released - scheduler should resume")
                        except Exception as e:
                            logger.critical(f"❌ Failed to release lock: {e}")
                    
                    # If scheduler is stuck for over 1 hour, trigger restart (reduced from 2 hours)
                    if time_since_alert > 3600 and AUTO_RESTART_ON_CRITICAL:
                        logger.critical("🔄 Scheduler stuck for over 1 hour - triggering restart!")
                        trigger_self_restart()
                        return
                        
                elif time_since_alert > 2400:  # 40 minutes warning
                    logger.warning(f"⚠️ Alert checker delayed: {time_since_alert/60:.1f} minutes since last run")
            
            # Check memory cleanup (should run every 15 minutes)
            if cleanup_last_run:
                time_since_cleanup = current_time - cleanup_last_run
                if time_since_cleanup > 1800:  # 30 minutes without running
                    logger.warning(f"⚠️ Memory cleanup hasn't run in {time_since_cleanup/60:.1f} minutes")
            
            # Log scheduler health status with crash monitor status
            health_status = crash_monitor.get_health_status()
            logger.info(f"🕐 Scheduler Watchdog: Alert check {time_since_alert/60:.1f}m ago, Cleanup {time_since_cleanup/60:.1f}m ago | Health: {health_status['status']}" if alert_last_run and cleanup_last_run else "🕐 Scheduler Watchdog: Waiting for first run")
            
            # Check if crash monitor recommends restart
            if crash_monitor.should_restart() and AUTO_RESTART_ON_CRITICAL:
                logger.critical("🔄 Crash monitor recommends restart - triggering!")
                trigger_self_restart()
            
        except Exception as e:
            logger.error(f"❌ Scheduler watchdog failed: {e}", exc_info=True)
            crash_monitor.record_failure(f"Watchdog failed: {e}", is_critical=False)

    def heartbeat_check():
        """Enhanced heartbeat mechanism with detailed health monitoring and crash detection"""
        if shutdown_requested.is_set():
            logger.info("🛑 Shutdown requested, skipping heartbeat")
            return
            
        try:
            with heartbeat_lock:
                current_time = datetime.now().strftime("%H:%M:%S")
                health_check = check_memory_health()
                status = health_check['status']
                current_memory = health_check['current_memory_mb']
                usage_percent = health_check['memory_usage_percent']
                
                # Check memory against crash monitor thresholds
                if current_memory > MAX_MEMORY_MB * 0.9:
                    need_restart = crash_monitor.record_memory_warning(current_memory)
                    if need_restart and AUTO_RESTART_ON_CRITICAL:
                        logger.critical(f"🚨 Memory critical ({current_memory:.1f}MB) - attempting emergency recovery first...")
                        if not emergency_memory_recovery():
                            logger.critical("🔄 Emergency recovery failed - triggering restart!")
                            trigger_self_restart()
                            return
                else:
                    # Memory is OK, record success
                    crash_monitor.record_success()
                
                # Determine heartbeat emoji based on status
                status_emoji = {
                    'HEALTHY': '💚',
                    'CAUTION': '💛', 
                    'WARNING': '🧡',
                    'CRITICAL': '🔴'
                }.get(status, '💓')
                
                # Check if heartbeat is still active
                if heartbeat_active.is_set():
                    crash_health = crash_monitor.get_health_status()
                    logger.info(f"{status_emoji} Heartbeat {current_time} - Memory: {current_memory:.1f}MB ({usage_percent:.1f}%) - Status: {status} | Failures: {crash_health['consecutive_failures']}")
                    
                    # Log detailed info for non-healthy states
                    if status != 'HEALTHY':
                        memory_info = health_check['memory_info']
                        logger.warning(f"📊 System Memory: {memory_info.get('system_used_percent', 0):.1f}% used, "
                                     f"Threads: {memory_info.get('num_threads', 0)}, "
                                     f"CPU: {memory_info.get('cpu_percent', 0):.1f}%")
                        
                        # Log recommendations
                        for rec in health_check['recommendations']:
                            logger.info(f"💡 Recommendation: {rec}")
                            
                        # Auto-execute critical recommendations
                        if status == 'CRITICAL':
                            logger.critical("🆘 CRITICAL STATUS - EXECUTING EMERGENCY MEASURES")
                            try:
                                if _global_jobbert_model is not None:
                                    unload_jobbert_model()
                                    logger.info("✅ Emergency model unload completed")

                                # Don't acquire memory_cleanup_lock - run cleanup directly to avoid deadlock
                                force_memory_cleanup()
                                logger.info("✅ Emergency cleanup completed")
                            except Exception as emergency_e:
                                logger.critical(f"❌ Emergency measures failed: {emergency_e}")
                else:
                    logger.warning(f"⚠️ Heartbeat {current_time} - Status: SHUTTING DOWN")
                    
        except Exception as e:
            logger.error(f"💔 Heartbeat failed: {e}")
            # This is critical - if heartbeat fails, something is very wrong
            try:
                logger.critical("🆘 SYSTEM HEALTH CHECK FAILED - POTENTIAL CRASH IMMINENT")
                # Don't acquire memory_cleanup_lock - run cleanup directly to avoid deadlock
                force_memory_cleanup()
            except Exception as critical_e:
                logger.critical(f"🆘 CRITICAL CLEANUP FAILED: {critical_e}")
                # Last resort - request shutdown
                shutdown_requested.set()

    try:
        bot_instance = Bot(token=token)
        
        # Add memory-aware alert checking (UTC-aware since scheduler uses UTC tz)
        scheduler.add_job(
            memory_aware_check_alerts,
            "interval",
            minutes=30,
            args=[bot_instance],
            max_instances=1,
            id="alert_checker",
            next_run_time=datetime.now(pytz.UTC) + timedelta(seconds=20),
        )
        
        # Add periodic memory cleanup every 15 minutes
        scheduler.add_job(
            periodic_memory_cleanup,
            "interval",
            minutes=15,
            max_instances=1,
            id="memory_cleanup"
        )
        
        # Add stuck operation cleanup every 5 minutes
        scheduler.add_job(
            cleanup_stuck_operations,
            "interval",
            minutes=5,
            max_instances=1,
            id="operation_cleanup"
        )
        
        # Add heartbeat mechanism every 2 minutes
        scheduler.add_job(
            heartbeat_check,
            "interval",
            minutes=2,
            max_instances=1,
            id="heartbeat_check"
        )
        
        # Add scheduler watchdog every 10 minutes
        scheduler.add_job(
            scheduler_watchdog,
            "interval",
            minutes=10,
            max_instances=1,
            id="scheduler_watchdog"
        )
        
        scheduler.start()
        logger.info("🚀 Background scheduler started:")
        logger.info("   - Alert checks every 30 minutes (memory-aware)")
        logger.info("   - Memory cleanup every 15 minutes")
        logger.info("   - Heartbeat monitoring every 2 minutes")
        logger.info("   - Stuck operation cleanup every 5 minutes")
        logger.info("   - Scheduler watchdog every 10 minutes")
        
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")
        return

    # Persist conversation states across restarts
    persistence = PicklePersistence(filename="/root/Job-Search-TG/conversation_states.pickle")
    updater = Updater(token, use_context=True, persistence=persistence)
    dispatcher = updater.dispatcher

    def error_handler(update, context):
        """Enhanced error handler to prevent crashes."""
        try:
            if isinstance(context.error, telegram.error.TimedOut):
                logger.warning("Telegram timeout error occurred")
            elif isinstance(context.error, telegram.error.BadRequest):
                logger.warning("Telegram BadRequest error: %s", context.error)
            elif isinstance(context.error, telegram.error.NetworkError):
                logger.error("Network error: %s", context.error)
            elif isinstance(context.error, MemoryError):
                logger.critical("Memory error! Attempting to free resources...")
                gc.collect()
                # Try to clear global models if memory is low
                global _global_jobbert_model, _global_adaptive_matcher
                if _global_jobbert_model:
                    _global_jobbert_model = None
                if _global_adaptive_matcher:
                    _global_adaptive_matcher = None
                gc.collect()
            else:
                logger.error("Unexpected error: %s", context.error, exc_info=True)
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

    dispatcher.add_error_handler(error_handler)

    # Backfill user names for existing users (runs once on startup)
    try:
        backfill_user_info(updater.bot)
    except Exception as e:
        logger.warning(f"User info backfill failed (non-critical): {e}")

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        name="main_conversation",
        persistent=True,
        states={
            MAIN_MENU: [
                CallbackQueryHandler(preferences_menu, pattern="^prefs$"),
                CallbackQueryHandler(start_search_flow,
                                     pattern="^start_search$"),
                CallbackQueryHandler(alerts_menu, pattern="^set_alert$"),
                CallbackQueryHandler(my_alerts, pattern="^my_alerts$"),
                CallbackQueryHandler(saved_jobs_menu, pattern="^saved_jobs$"),
                CallbackQueryHandler(start_from_callback, pattern="^start_command$"),
            ],
            PREFERENCES_MENU: [
                CallbackQueryHandler(show_date_posted_menu,
                                     pattern="^set_date_posted$"),
                CallbackQueryHandler(show_workplace_menu,
                                     pattern="^set_workplace$"),
                CallbackQueryHandler(lambda u, c: show_multi_select_menu(
                    u, c, "experience"), pattern="^set_experience$"),
                CallbackQueryHandler(lambda u, c: show_multi_select_menu(
                    u, c, "job_type"), pattern="^set_job_types$"),
                CallbackQueryHandler(set_timezone_start,
                                     pattern="^set_timezone$"),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
                CallbackQueryHandler(start_from_callback, pattern="^start_command$"),
            ],
            ALERTS_MENU: [
                CallbackQueryHandler(add_alert_start, pattern="^add_alert$"),
                CallbackQueryHandler(my_alerts, pattern="^my_alerts$"),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
                CallbackQueryHandler(start_from_callback, pattern="^start_command$"),
            ],
            MY_ALERTS: [
                CallbackQueryHandler(add_alert_start, pattern="^add_alert$"),
                CallbackQueryHandler(alerts_menu, pattern="^alerts_menu$"),
                CallbackQueryHandler(view_alert_details,
                                     pattern="^view_alert_"),
                CallbackQueryHandler(toggle_alert_status,
                                     pattern="^(pause|resume)_alert_"),
                CallbackQueryHandler(edit_alert_start, pattern="^edit_alert_"),
                CallbackQueryHandler(delete_alert_start,
                                     pattern="^delete_alert_start_"),
                CallbackQueryHandler(delete_alert_confirm,
                                     pattern="^delete_alert_confirm_"),
                CallbackQueryHandler(my_alerts,
                                     pattern="^my_alerts$"),
                CallbackQueryHandler(start_from_callback, pattern="^start_command$"),
            ],
            ADD_ALERT_KEYWORD: [
                MessageHandler(TEXT_FILTER & ~COMMAND_FILTER,
                               add_alert_keyword_received)
            ],
            ADD_ALERT_LOCATION: [
                MessageHandler(TEXT_FILTER & ~COMMAND_FILTER,
                               add_alert_location_received)
            ],
            SET_TIMEZONE: [
                MessageHandler(
                    TEXT_FILTER & ~COMMAND_FILTER, timezone_received
                )
            ],
            GET_SEARCH_KEYWORD: [
                MessageHandler(TEXT_FILTER & ~COMMAND_FILTER, keyword_received)
            ],
            GET_SEARCH_LOCATION: [
                MessageHandler(
                    TEXT_FILTER & ~COMMAND_FILTER, location_received
                )
            ],
            DATE_POSTED_MENU: [
                CallbackQueryHandler(preferences_menu, pattern="^dp_done$"),
                CallbackQueryHandler(date_posted_selected, pattern="^dp_"),
            ],
            WORKPLACE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern="^wt_done$"),
                CallbackQueryHandler(workplace_selected, pattern="^wt_"),
            ],
            EXPERIENCE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern="^exp_done$"),
                CallbackQueryHandler(
                    lambda u, c: toggle_multi_select_option(
                        u, c, "experience"
                    ),
                    pattern="^exp_"
                ),
            ],
            JOB_TYPE_MENU: [
                CallbackQueryHandler(preferences_menu, pattern="^jt_done$"),
                CallbackQueryHandler(
                    lambda u, c: toggle_multi_select_option(
                        u, c, "job_type"
                    ),
                    pattern="^jt_"
                ),
            ],
            Browse: [
                CallbackQueryHandler(page_navigation, pattern="^page_"),
                CallbackQueryHandler(ignore_callback, pattern="^ignore$"),
                CallbackQueryHandler(close_Browse, pattern="^close$"),
                CallbackQueryHandler(my_alerts, pattern="^my_alerts$"),
            ],
            ALERT_PREFERENCES: [
                CallbackQueryHandler(alert_skip_filters,
                                     pattern="^alert_skip_filters$"),
                CallbackQueryHandler(alert_set_filters,
                                     pattern="^alert_set_filters$"),
                CallbackQueryHandler(show_alert_date_posted_menu,
                                     pattern="^alert_set_date_posted$"),
                CallbackQueryHandler(show_alert_workplace_menu,
                                     pattern="^alert_set_workplace$"),
                CallbackQueryHandler(lambda u, c: show_alert_multi_select_menu(
                    u, c, "experience"), pattern="^alert_set_experience$"),
                CallbackQueryHandler(lambda u, c: show_alert_multi_select_menu(
                    u, c, "job_type"), pattern="^alert_set_job_types$"),
                CallbackQueryHandler(alert_preferences_done,
                                     pattern="^alert_dp_done$"),
                CallbackQueryHandler(alert_preferences_done,
                                     pattern="^alert_wt_done$"),
                CallbackQueryHandler(alert_preferences_done,
                                     pattern="^alert_exp_done$"),
                CallbackQueryHandler(alert_preferences_done,
                                     pattern="^alert_jt_done$"),
                CallbackQueryHandler(alert_date_posted_selected,
                                     pattern="^alert_dp_"),
                CallbackQueryHandler(alert_workplace_selected,
                                     pattern="^alert_wt_"),
                CallbackQueryHandler(
                    lambda u, c: alert_toggle_multi_select_option(
                        u, c, "experience"
                    ),
                    pattern="^alert_exp_"
                ),
                CallbackQueryHandler(
                    lambda u, c: alert_toggle_multi_select_option(
                        u, c, "job_type"
                    ),
                    pattern="^alert_jt_"
                ),
                CallbackQueryHandler(alert_save_final,
                                     pattern="^alert_save_final$"),
            ],
            EDIT_ALERT_PREFERENCES: [
                CallbackQueryHandler(show_edit_alert_date_posted_menu,
                                     pattern="^edit_alert_set_date_posted$"),
                CallbackQueryHandler(show_edit_alert_workplace_menu,
                                     pattern="^edit_alert_set_workplace$"),
                CallbackQueryHandler(
                    lambda u, c: show_edit_alert_multi_select_menu(
                        u, c, "experience"
                    ),
                    pattern="^edit_alert_set_experience$"
                ),
                CallbackQueryHandler(
                    lambda u, c: show_edit_alert_multi_select_menu(
                        u, c, "job_type"
                    ),
                    pattern="^edit_alert_set_job_types$"
                ),
                CallbackQueryHandler(edit_alert_preferences_done,
                                     pattern="^edit_alert_dp_done$"),
                CallbackQueryHandler(edit_alert_preferences_done,
                                     pattern="^edit_alert_wt_done$"),
                CallbackQueryHandler(edit_alert_preferences_done,
                                     pattern="^edit_alert_exp_done$"),
                CallbackQueryHandler(edit_alert_preferences_done,
                                     pattern="^edit_alert_jt_done$"),
                CallbackQueryHandler(edit_alert_date_posted_selected,
                                     pattern="^edit_alert_dp_"),
                CallbackQueryHandler(edit_alert_workplace_selected,
                                     pattern="^edit_alert_wt_"),
                CallbackQueryHandler(
                    lambda u, c: edit_alert_toggle_multi_select_option(
                        u, c, "experience"
                    ),
                    pattern="^edit_alert_exp_"
                ),
                CallbackQueryHandler(
                    lambda u, c: edit_alert_toggle_multi_select_option(
                        u, c, "job_type"
                    ),
                    pattern="^edit_alert_jt_"
                ),
                CallbackQueryHandler(edit_alert_save_final,
                                     pattern="^edit_alert_save_final$"),
                CallbackQueryHandler(my_alerts, pattern="^my_alerts$"),
            ],
            SAVED_JOBS: [
                CallbackQueryHandler(saved_jobs_navigation, pattern="^saved_jobs_(next|prev)$"),
                CallbackQueryHandler(unsave_job_callback, pattern="^unsave_job_"),
                CallbackQueryHandler(save_job_callback, pattern="^save_job_"),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
                CallbackQueryHandler(start_from_callback, pattern="^start_command$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel),
                   CommandHandler("start", start),
                   CallbackQueryHandler(save_job_callback, pattern="^save_job_"),
                   CallbackQueryHandler(unsave_from_alert_callback, pattern="^unsave_from_alert_")],
        allow_reentry=True,
    )
    dispatcher.add_handler(conv_handler)

    # Add standalone save/unsave handlers outside ConversationHandler
    # This fixes the issue where callbacks are dropped when user has no active
    # conversation state (e.g., after bot restart without persistence)
    dispatcher.add_handler(CallbackQueryHandler(save_job_callback, pattern="^save_job_"), group=1)
    dispatcher.add_handler(CallbackQueryHandler(unsave_from_alert_callback, pattern="^unsave_from_alert_"), group=1)

    # Add admin command handlers
    dispatcher.add_handler(CommandHandler("stats", admin_stats))

    # Admin panel ConversationHandler (group=2 to avoid interfering with main flow)
    admin_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_command)],
        name="admin_conversation",
        persistent=False,
        states={
            ADMIN_MENU: [
                CallbackQueryHandler(admin_view_user_alerts, pattern=r"^adm_user_\d+$"),
                CallbackQueryHandler(admin_user_list, pattern=r"^adm_users$"),
                CallbackQueryHandler(admin_user_list, pattern=r"^adm_upage_\d+$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
            ADMIN_USER_ALERTS: [
                CallbackQueryHandler(admin_view_alert_details, pattern=r"^adm_va_\d+$"),
                CallbackQueryHandler(admin_delete_user_start, pattern=r"^adm_deluserstart_\d+$"),
                CallbackQueryHandler(admin_delete_user_confirm, pattern=r"^adm_deluserconf_\d+$"),
                CallbackQueryHandler(admin_view_user_alerts, pattern=r"^adm_user_\d+$"),
                CallbackQueryHandler(admin_user_list, pattern=r"^adm_users$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
            ADMIN_ALERT_DETAILS: [
                CallbackQueryHandler(admin_edit_keywords_start, pattern=r"^adm_editkw_\d+$"),
                CallbackQueryHandler(admin_edit_location_start, pattern=r"^adm_editloc_\d+$"),
                CallbackQueryHandler(admin_edit_filters_start, pattern=r"^adm_editflt_\d+$"),
                CallbackQueryHandler(admin_toggle_alert, pattern=r"^adm_(pause|resume)_\d+$"),
                CallbackQueryHandler(admin_delete_alert_start, pattern=r"^adm_delstart_\d+$"),
                CallbackQueryHandler(admin_delete_alert_confirm, pattern=r"^adm_delconf_\d+$"),
                CallbackQueryHandler(admin_view_alert_details, pattern=r"^adm_va_\d+$"),
                CallbackQueryHandler(admin_view_user_alerts, pattern=r"^adm_back_user_\d+$"),
                CallbackQueryHandler(admin_user_list, pattern=r"^adm_users$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
            ADMIN_EDIT_KEYWORDS: [
                MessageHandler(Filters.text & ~Filters.command, admin_edit_keywords_receive),
                CallbackQueryHandler(admin_view_alert_details, pattern=r"^adm_va_\d+$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
            ADMIN_EDIT_LOCATION: [
                MessageHandler(Filters.text & ~Filters.command, admin_edit_location_receive),
                CallbackQueryHandler(admin_view_alert_details, pattern=r"^adm_va_\d+$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
            ADMIN_EDIT_FILTERS: [
                CallbackQueryHandler(admin_filter_show_date_posted, pattern=r"^adm_flt_cat_dp$"),
                CallbackQueryHandler(admin_filter_show_workplace, pattern=r"^adm_flt_cat_wp$"),
                CallbackQueryHandler(admin_filter_show_experience, pattern=r"^adm_flt_cat_exp$"),
                CallbackQueryHandler(admin_filter_show_job_types, pattern=r"^adm_flt_cat_jt$"),
                CallbackQueryHandler(admin_filter_done, pattern=r"^adm_flt_done_(dp|wp|exp|jt)$"),
                CallbackQueryHandler(admin_filter_date_posted_selected, pattern=r"^adm_flt_dp_"),
                CallbackQueryHandler(admin_filter_workplace_selected, pattern=r"^adm_flt_wp_"),
                CallbackQueryHandler(admin_filter_experience_selected, pattern=r"^adm_flt_exp_"),
                CallbackQueryHandler(admin_filter_job_types_selected, pattern=r"^adm_flt_jt_"),
                CallbackQueryHandler(admin_view_alert_details, pattern=r"^adm_va_\d+$"),
                CallbackQueryHandler(admin_view_user_alerts, pattern=r"^adm_back_user_\d+$"),
                CallbackQueryHandler(admin_user_list, pattern=r"^adm_users$"),
                CallbackQueryHandler(admin_cancel, pattern=r"^adm_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_cancel_command),
            CommandHandler("admin", admin_command),
        ],
        allow_reentry=True,
    )
    dispatcher.add_handler(admin_conv_handler, group=2)

    # Fallback for stale admin callbacks after restart (conversation state lost)
    def admin_stale_callback_handler(update: Update, context: CallbackContext):
        """Re-route stale admin callbacks when conversation state is lost."""
        query = update.callback_query
        if update.effective_user.id != ADMIN_USER_ID:
            return
        safe_answer_callback_query(query)
        data = query.data
        if data.startswith("adm_user_") or data.startswith("adm_back_user_"):
            return admin_view_user_alerts(update, context)
        elif data.startswith("adm_va_"):
            return admin_view_alert_details(update, context)
        elif data.startswith("adm_users") or data.startswith("adm_upage_"):
            return admin_user_list(update, context)
        elif data.startswith("adm_pause_") or data.startswith("adm_resume_"):
            return admin_toggle_alert(update, context)
        elif data.startswith("adm_editkw_"):
            return admin_edit_keywords_start(update, context)
        elif data.startswith("adm_editloc_"):
            return admin_edit_location_start(update, context)
        elif data.startswith("adm_editflt_"):
            return admin_edit_filters_start(update, context)
        elif data.startswith("adm_delstart_"):
            return admin_delete_alert_start(update, context)
        elif data.startswith("adm_delconf_"):
            return admin_delete_alert_confirm(update, context)
        elif data.startswith("adm_deluserstart_"):
            return admin_delete_user_start(update, context)
        elif data.startswith("adm_deluserconf_"):
            return admin_delete_user_confirm(update, context)
        elif data == "adm_cancel":
            return admin_cancel(update, context)
        else:
            # Unknown adm_ callback, open admin panel fresh
            return admin_command(update, context)

    dispatcher.add_handler(
        CallbackQueryHandler(admin_stale_callback_handler, pattern=r"^adm_"),
        group=2
    )

    logger.info("Bot started polling...")
    logger.info(f"🔧 Auto-restart enabled: {AUTO_RESTART_ON_CRITICAL}")
    logger.info(f"📊 Crash monitor initialized - Status: {crash_monitor.get_health_status()['status']}")
    
    # Start CPU tracker for accurate CPU measurements
    cpu_tracker.start()
    
    try:
        updater.start_polling(timeout=30, read_latency=2)
        logger.info("✅ Bot is now running! Press Ctrl+C to stop.")
        crash_monitor.record_success()  # Record successful startup
        updater.idle()
    except MemoryError as e:
        logger.critical(f"💥 MEMORY ERROR in main polling loop: {e}")
        crash_monitor.record_failure(f"MemoryError: {e}", is_critical=True)
        if AUTO_RESTART_ON_CRITICAL:
            logger.critical("🔄 Attempting restart after memory error...")
            trigger_self_restart()
    except KeyboardInterrupt:
        logger.info("🛑 Received keyboard interrupt, shutting down gracefully...")
    except Exception as e:
        logger.error(f"Bot polling failed: {e}", exc_info=True)
        should_restart = crash_monitor.record_failure(f"Polling failed: {e}", is_critical=True)
        if should_restart and AUTO_RESTART_ON_CRITICAL:
            logger.critical("🔄 Polling failure - triggering restart...")
            trigger_self_restart()
    finally:
        logger.info("Shutting down bot...")
        try:
            # Signal graceful shutdown
            with global_state_lock:
                shutdown_requested.set()
                heartbeat_active.clear()
            
            # Shutdown scheduler
            scheduler.shutdown(wait=False)
            logger.info("✅ Scheduler shutdown complete")
            
            # Shutdown thread pool
            executor.shutdown(wait=False)
            logger.info("✅ Thread pool shutdown complete")
            
            # Stop CPU tracker
            cpu_tracker.stop()
            logger.info("✅ CPU tracker stopped")
            
            # Close database pool
            db_pool.close_all()
            logger.info("✅ Database pool closed")
            
            # Clear global models to free memory
            global _global_jobbert_model, _global_adaptive_matcher
            _global_jobbert_model = None
            _global_adaptive_matcher = None
            gc.collect()
            logger.info("✅ Memory cleanup complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        logger.info("🛑 Bot shutdown complete.")


if __name__ == "__main__":
    main()
