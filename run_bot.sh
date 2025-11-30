#!/bin/bash
# =============================================================================
# JobQuestTG Bot Auto-Restart Wrapper Script
# =============================================================================
# This script provides automatic restart functionality for the bot with:
# - Automatic restart on crash
# - Memory monitoring and automatic restart on memory issues
# - Exponential backoff for rapid failures
# - Logging of all restarts
# =============================================================================

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_SCRIPT="$SCRIPT_DIR/bot.py"

# Auto-detect virtual environment directory
if [ -d "$SCRIPT_DIR/.venv" ]; then
    VENV_DIR="$SCRIPT_DIR/.venv"
elif [ -d "$SCRIPT_DIR/venv" ]; then
    VENV_DIR="$SCRIPT_DIR/venv"
elif [ -d "$SCRIPT_DIR/jobs" ]; then
    VENV_DIR="$SCRIPT_DIR/jobs"
else
    VENV_DIR=""  # No venv, use system Python
fi

LOG_FILE="$SCRIPT_DIR/bot_wrapper.log"
PID_FILE="$SCRIPT_DIR/bot.pid"

# Memory thresholds (three-tier system)
MEMORY_WARNING_MB=2200   # Warning threshold - wait and re-check (3 times)
MEMORY_CRITICAL_MB=2800  # Critical threshold - quick re-check (1 time, 15s)
MEMORY_EXTREME_MB=3200   # Extreme threshold - immediate restart (no wait, OOM imminent)
MEMORY_CHECK_RETRIES=3   # Number of re-checks at warning level
MEMORY_CHECK_WAIT=60     # Seconds to wait between re-checks at warning level
MEMORY_CRITICAL_WAIT=15  # Seconds to wait for critical re-check (quick)

MIN_RESTART_INTERVAL=30  # Minimum seconds between restarts
MAX_RESTART_INTERVAL=3600  # Maximum backoff (1 hour)
HEALTH_CHECK_INTERVAL=60  # Check health every 60 seconds
MAX_RAPID_RESTARTS=5  # Maximum restarts within rapid restart window
RAPID_RESTART_WINDOW=300  # 5 minutes window for rapid restart detection

# Track memory occurrences
high_memory_count=0
critical_memory_count=0

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Restart tracking
restart_count=0
last_restart_time=0
restart_times=()
current_backoff=$MIN_RESTART_INTERVAL

# Logging function
log() {
    local level=$1
    shift
    local message="$@"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "[$timestamp] [$level] $message" | tee -a "$LOG_FILE"
}

log_info() { log "INFO" "$@"; }
log_warn() { log "WARN" "$@"; }
log_error() { log "ERROR" "$@"; }
log_critical() { log "CRITICAL" "$@"; }

# Cleanup function
cleanup() {
    log_info "🛑 Received shutdown signal, cleaning up..."
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "Sending SIGTERM to bot process (PID: $pid)..."
            kill -TERM "$pid" 2>/dev/null || true
            # Wait up to 30 seconds for graceful shutdown
            for i in {1..30}; do
                if ! kill -0 "$pid" 2>/dev/null; then
                    log_info "✅ Bot process terminated gracefully"
                    break
                fi
                sleep 1
            done
            # Force kill if still running
            if kill -0 "$pid" 2>/dev/null; then
                log_warn "⚠️ Force killing bot process..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$PID_FILE"
    fi
    log_info "🏁 Wrapper script shutdown complete"
    exit 0
}

# Set up signal handlers
trap cleanup SIGINT SIGTERM SIGHUP

# Check if another instance is running
check_existing_instance() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_error "Another instance is already running (PID: $pid)"
            log_error "If this is incorrect, remove $PID_FILE and try again"
            exit 1
        else
            log_warn "Stale PID file found, removing..."
            rm -f "$PID_FILE"
        fi
    fi
}

# Activate virtual environment
activate_venv() {
    if [ -n "$VENV_DIR" ] && [ -d "$VENV_DIR" ]; then
        log_info "🐍 Activating virtual environment at $VENV_DIR..."
        source "$VENV_DIR/bin/activate"
    else
        log_warn "⚠️ No virtual environment found, using system Python"
        # Check if python is available
        if ! command -v python &> /dev/null && ! command -v python3 &> /dev/null; then
            log_error "Python not found in PATH"
            exit 1
        fi
    fi
}

# Get memory usage of a process in MB
get_memory_usage() {
    local pid=$1
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo 0
        return
    fi
    
    # Try different methods to get memory usage
    if command -v ps &> /dev/null; then
        # macOS and Linux compatible
        local mem=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
        if [ -n "$mem" ]; then
            echo $((mem / 1024))
            return
        fi
    fi
    
    # Fallback for Linux
    if [ -f "/proc/$pid/status" ]; then
        local mem=$(grep VmRSS /proc/$pid/status 2>/dev/null | awk '{print $2}')
        if [ -n "$mem" ]; then
            echo $((mem / 1024))
            return
        fi
    fi
    
    echo 0
}

# Check if we're experiencing rapid restarts (potential crash loop)
check_rapid_restarts() {
    local current_time=$(date +%s)
    
    # Remove old restart times outside the window
    local new_times=()
    for t in "${restart_times[@]}"; do
        if [ $((current_time - t)) -lt $RAPID_RESTART_WINDOW ]; then
            new_times+=("$t")
        fi
    done
    restart_times=("${new_times[@]}")
    
    # Add current restart
    restart_times+=("$current_time")
    
    # Check if too many restarts in window
    if [ ${#restart_times[@]} -ge $MAX_RAPID_RESTARTS ]; then
        return 0  # Yes, rapid restarts detected
    fi
    return 1  # No rapid restarts
}

# Calculate backoff time
calculate_backoff() {
    if check_rapid_restarts; then
        # Exponential backoff
        current_backoff=$((current_backoff * 2))
        if [ $current_backoff -gt $MAX_RESTART_INTERVAL ]; then
            current_backoff=$MAX_RESTART_INTERVAL
        fi
        log_warn "⚠️ Rapid restarts detected! Backing off for $current_backoff seconds"
    else
        # Reset backoff if no rapid restarts
        current_backoff=$MIN_RESTART_INTERVAL
    fi
}

# Get file modification time in seconds since epoch (cross-platform)
get_file_mtime() {
    local file=$1
    if [ -f "$file" ]; then
        # Try Linux stat first, then macOS
        stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

# Smart memory check with three-tier thresholds
# Returns: 0 = OK, 1 = needs restart (after checks), 2 = critical restart needed
check_memory_smart() {
    local pid=$1
    local mem=$(get_memory_usage "$pid")
    
    # Tier 1: EXTREME (>3.2GB) - Immediate restart, OOM imminent
    if [ "$mem" -gt "$MEMORY_EXTREME_MB" ]; then
        log_critical "🚨 EXTREME: Memory ($mem MB) exceeds extreme limit ($MEMORY_EXTREME_MB MB)!"
        log_critical "⚡ IMMEDIATE restart - OOM crash imminent, no time to wait!"
        high_memory_count=0
        critical_memory_count=0
        return 2
    fi
    
    # Tier 2: CRITICAL (2.8-3.2GB) - Quick re-check (15s wait, 1 retry)
    if [ "$mem" -gt "$MEMORY_CRITICAL_MB" ]; then
        critical_memory_count=$((critical_memory_count + 1))
        log_critical "🔴 CRITICAL: Memory at $mem MB (critical check $critical_memory_count/2)"
        
        if [ "$critical_memory_count" -ge 2 ]; then
            log_critical "🚨 Memory still critical after quick re-check - restarting!"
            critical_memory_count=0
            high_memory_count=0
            return 2
        fi
        
        log_warn "⏱️ Quick re-check in ${MEMORY_CRITICAL_WAIT}s (might be a spike)..."
        sleep "$MEMORY_CRITICAL_WAIT"
        
        # Re-check immediately
        mem=$(get_memory_usage "$pid")
        if [ "$mem" -gt "$MEMORY_CRITICAL_MB" ]; then
            log_critical "🚨 Still critical ($mem MB) after ${MEMORY_CRITICAL_WAIT}s - restarting!"
            critical_memory_count=0
            high_memory_count=0
            return 2
        else
            log_info "✅ Memory dropped to $mem MB after quick wait - spike resolved!"
            critical_memory_count=0
            return 0
        fi
    fi
    
    # Tier 3: WARNING (2.2-2.8GB) - Wait and re-check (60s wait, 3 retries)
    if [ "$mem" -gt "$MEMORY_WARNING_MB" ]; then
        high_memory_count=$((high_memory_count + 1))
        critical_memory_count=0  # Reset critical counter
        log_warn "⚠️ High memory detected: $mem MB (check $high_memory_count/$MEMORY_CHECK_RETRIES)"
        
        if [ "$high_memory_count" -ge "$MEMORY_CHECK_RETRIES" ]; then
            log_critical "🚨 Memory persistently high after $MEMORY_CHECK_RETRIES checks - restarting"
            high_memory_count=0
            return 1
        fi
        
        log_info "💡 Waiting ${MEMORY_CHECK_WAIT}s to see if memory drops (might be temporary spike)..."
        return 0  # Don't restart yet, wait for next health check cycle
    fi
    
    # Memory is OK - reset all counters
    if [ "$high_memory_count" -gt 0 ] || [ "$critical_memory_count" -gt 0 ]; then
        log_info "✅ Memory back to normal ($mem MB) - resetting counters"
        high_memory_count=0
        critical_memory_count=0
    fi
    
    return 0
}

# Health check function
health_check() {
    local pid=$1
    
    # Check if process is alive
    if ! kill -0 "$pid" 2>/dev/null; then
        log_error "❌ Bot process (PID: $pid) is not running!"
        return 1
    fi
    
    # Smart memory check with two-tier thresholds
    local mem=$(get_memory_usage "$pid")
    check_memory_smart "$pid"
    local mem_status=$?
    
    if [ "$mem_status" -eq 2 ]; then
        # Critical - immediate restart
        log_critical "🚨 CRITICAL memory - immediate restart!"
        return 2
    elif [ "$mem_status" -eq 1 ]; then
        # Persistent high memory - restart
        log_warn "⚠️ Persistent high memory - triggering restart..."
        return 2
    fi
    
    # Check if diagnostic.log has been updated recently (within last 5 minutes)
    local diagnostic_log="$SCRIPT_DIR/diagnostic.log"
    if [ -f "$diagnostic_log" ]; then
        local file_mtime=$(get_file_mtime "$diagnostic_log")
        local current_time=$(date +%s)
        local log_age=$((current_time - file_mtime))
        if [ "$log_age" -gt 300 ]; then
            log_warn "⚠️ Diagnostic log hasn't been updated in $log_age seconds"
            # This could indicate a hung process
            if [ "$log_age" -gt 600 ]; then
                log_critical "🚨 Bot appears to be hung (no log activity for $log_age seconds)!"
                return 3
            fi
        fi
    fi
    
    return 0
}

# Start the bot
start_bot() {
    log_info "🚀 Starting JobQuestTG Bot..."
    
    cd "$SCRIPT_DIR"
    
    # Determine Python command
    local python_cmd="python"
    if ! command -v python &> /dev/null; then
        python_cmd="python3"
    fi
    
    log_info "📍 Using Python: $(which $python_cmd)"
    log_info "📍 Working directory: $SCRIPT_DIR"
    
    # Run the bot in background and capture PID
    $python_cmd "$BOT_SCRIPT" &
    local pid=$!
    echo $pid > "$PID_FILE"
    
    log_info "✅ Bot started with PID: $pid"
    
    # Wait a moment for startup
    sleep 5
    
    # Verify it started successfully
    if ! kill -0 "$pid" 2>/dev/null; then
        log_error "❌ Bot failed to start!"
        return 1
    fi
    
    return 0
}

# Stop the bot gracefully
stop_bot() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "🛑 Stopping bot (PID: $pid)..."
            kill -TERM "$pid" 2>/dev/null
            
            # Wait for graceful shutdown
            local timeout=30
            while [ $timeout -gt 0 ] && kill -0 "$pid" 2>/dev/null; do
                sleep 1
                ((timeout--))
            done
            
            # Force kill if still running
            if kill -0 "$pid" 2>/dev/null; then
                log_warn "⚠️ Force killing bot..."
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$PID_FILE"
    fi
}

# Main loop
main() {
    log_info "=========================================="
    log_info "🤖 JobQuestTG Bot Auto-Restart Wrapper"
    log_info "=========================================="
    log_info "Memory Thresholds (3-tier system):"
    log_info "  - Warning  : >${MEMORY_WARNING_MB}MB  → Re-check ${MEMORY_CHECK_RETRIES}x (${MEMORY_CHECK_WAIT}s each)"
    log_info "  - Critical : >${MEMORY_CRITICAL_MB}MB  → Quick re-check (${MEMORY_CRITICAL_WAIT}s)"
    log_info "  - Extreme  : >${MEMORY_EXTREME_MB}MB  → Immediate restart"
    log_info "Other Settings:"
    log_info "  - Health Check: every ${HEALTH_CHECK_INTERVAL}s"
    log_info "  - Restart Backoff: ${MIN_RESTART_INTERVAL}s - ${MAX_RESTART_INTERVAL}s"
    log_info "=========================================="
    
    check_existing_instance
    activate_venv
    
    while true; do
        # Start the bot
        if ! start_bot; then
            log_error "Failed to start bot, waiting before retry..."
            calculate_backoff
            sleep $current_backoff
            continue
        fi
        
        local pid=$(cat "$PID_FILE")
        restart_count=$((restart_count + 1))
        last_restart_time=$(date +%s)
        
        log_info "📊 Restart count: $restart_count"
        
        # Monitor the bot
        while true; do
            sleep $HEALTH_CHECK_INTERVAL
            
            local health_status
            health_check "$pid"
            health_status=$?
            
            case $health_status in
                0)
                    # Healthy
                    local mem=$(get_memory_usage "$pid")
                    log_info "💚 Health OK - PID: $pid, Memory: ${mem}MB"
                    ;;
                1)
                    # Process died
                    log_error "❌ Bot process died unexpectedly!"
                    break
                    ;;
                2)
                    # Memory issue
                    log_critical "🚨 Restarting due to memory issues..."
                    stop_bot
                    break
                    ;;
                3)
                    # Hung process
                    log_critical "🚨 Restarting due to hung process..."
                    stop_bot
                    break
                    ;;
            esac
        done
        
        # Calculate backoff before restart
        calculate_backoff
        
        log_info "⏳ Waiting $current_backoff seconds before restart..."
        sleep $current_backoff
        
        # Clear any stale state
        rm -f "$PID_FILE"
        
        log_info "🔄 Restarting bot..."
    done
}

# Handle command line arguments
case "${1:-}" in
    start)
        main
        ;;
    stop)
        stop_bot
        log_info "Bot stopped"
        ;;
    status)
        if [ -f "$PID_FILE" ]; then
            pid=$(cat "$PID_FILE")
            if kill -0 "$pid" 2>/dev/null; then
                mem=$(get_memory_usage "$pid")
                echo -e "${GREEN}✅ Bot is running (PID: $pid, Memory: ${mem}MB)${NC}"
            else
                echo -e "${RED}❌ Bot is not running (stale PID file)${NC}"
            fi
        else
            echo -e "${YELLOW}⚠️ Bot is not running (no PID file)${NC}"
        fi
        ;;
    restart)
        stop_bot
        sleep 2
        main
        ;;
    *)
        # Default: start with auto-restart
        main
        ;;
esac

