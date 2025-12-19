#!/usr/bin/env python3
"""
Migration script to transfer data from SQLite to PostgreSQL.
Run this script once to migrate your existing data.

Prerequisites:
1. PostgreSQL server running with target database created
2. Set environment variables (or DATABASE_URL)
3. Existing job_alerts.db file in current directory

Usage:
    python migrate_to_postgres.py
    
    or with env variables:
    
    POSTGRES_HOST=localhost POSTGRES_PORT=5432 POSTGRES_DB=job_alerts \
    POSTGRES_USER=postgres POSTGRES_PASSWORD=mypassword python migrate_to_postgres.py
"""

import os
import sqlite3
import sys
from datetime import datetime

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("❌ psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def get_postgres_config():
    """Get PostgreSQL configuration from environment."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        import urllib.parse
        result = urllib.parse.urlparse(database_url)
        return {
            "host": result.hostname,
            "port": result.port or 5432,
            "database": result.path[1:],
            "user": result.username,
            "password": result.password,
        }
    
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "database": os.getenv("POSTGRES_DB", "job_alerts"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
    }


def create_postgres_tables(pg_conn):
    """Create PostgreSQL tables if they don't exist."""
    cursor = pg_conn.cursor()
    
    print("📋 Creating PostgreSQL tables...")
    
    # Alerts table
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
    
    # Sent jobs table
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
            sent_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (alert_id, job_link),
            FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
        )
    """)
    
    # User settings table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id BIGINT PRIMARY KEY,
            timezone TEXT
        )
    """)
    
    # Saved jobs table
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
    
    # Job details cache table
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
    
    pg_conn.commit()
    print("✅ PostgreSQL tables created successfully")


def migrate_alerts(sqlite_conn, pg_conn):
    """Migrate alerts table."""
    print("\n📤 Migrating alerts...")
    
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Get data from SQLite
    sqlite_cursor.execute("SELECT id, chat_id, keywords, location, filters, is_active, last_checked FROM alerts")
    rows = sqlite_cursor.fetchall()
    
    if not rows:
        print("   No alerts to migrate")
        return {}
    
    # We need to map old IDs to new IDs since PostgreSQL SERIAL will auto-generate
    old_to_new_id = {}
    
    for row in rows:
        old_id, chat_id, keywords, location, filters, is_active, last_checked = row
        
        # Insert into PostgreSQL
        pg_cursor.execute("""
            INSERT INTO alerts (chat_id, keywords, location, filters, is_active, last_checked)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (chat_id, keywords, location, filters, is_active, last_checked))
        
        new_id = pg_cursor.fetchone()[0]
        old_to_new_id[old_id] = new_id
    
    pg_conn.commit()
    print(f"   ✅ Migrated {len(rows)} alerts")
    return old_to_new_id


def migrate_sent_jobs(sqlite_conn, pg_conn, alert_id_map):
    """Migrate sent_jobs table."""
    print("\n📤 Migrating sent jobs...")
    
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Get data from SQLite
    try:
        sqlite_cursor.execute("""
            SELECT alert_id, chat_id, job_link, job_id, job_title, company, 
                   canonical_title, canonical_company, sent_at 
            FROM sent_jobs
        """)
        rows = sqlite_cursor.fetchall()
    except sqlite3.OperationalError:
        # Table might not have all columns
        print("   ⚠️ sent_jobs table has old schema, attempting migration...")
        sqlite_cursor.execute("SELECT * FROM sent_jobs")
        rows = sqlite_cursor.fetchall()
    
    if not rows:
        print("   No sent jobs to migrate")
        return
    
    migrated = 0
    skipped = 0
    
    for row in rows:
        try:
            alert_id = row[0]
            if alert_id and alert_id in alert_id_map:
                new_alert_id = alert_id_map[alert_id]
            else:
                skipped += 1
                continue
            
            chat_id = row[1] if len(row) > 1 else 0
            job_link = row[2] if len(row) > 2 else ''
            job_id = row[3] if len(row) > 3 else ''
            job_title = row[4] if len(row) > 4 else 'N/A'
            company = row[5] if len(row) > 5 else 'N/A'
            canonical_title = row[6] if len(row) > 6 else ''
            canonical_company = row[7] if len(row) > 7 else ''
            sent_at = row[8] if len(row) > 8 else None
            
            pg_cursor.execute("""
                INSERT INTO sent_jobs (alert_id, chat_id, job_link, job_id, job_title, 
                                       company, canonical_title, canonical_company, sent_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (alert_id, job_link) DO NOTHING
            """, (new_alert_id, chat_id, job_link, job_id, job_title, 
                  company, canonical_title, canonical_company, sent_at))
            migrated += 1
        except Exception as e:
            print(f"   ⚠️ Skipping row due to error: {e}")
            skipped += 1
    
    pg_conn.commit()
    print(f"   ✅ Migrated {migrated} sent jobs (skipped {skipped})")


def migrate_user_settings(sqlite_conn, pg_conn):
    """Migrate user_settings table."""
    print("\n📤 Migrating user settings...")
    
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    try:
        sqlite_cursor.execute("SELECT chat_id, timezone FROM user_settings")
        rows = sqlite_cursor.fetchall()
    except sqlite3.OperationalError:
        print("   No user_settings table found")
        return
    
    if not rows:
        print("   No user settings to migrate")
        return
    
    for chat_id, timezone in rows:
        pg_cursor.execute("""
            INSERT INTO user_settings (chat_id, timezone)
            VALUES (%s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET timezone = EXCLUDED.timezone
        """, (chat_id, timezone))
    
    pg_conn.commit()
    print(f"   ✅ Migrated {len(rows)} user settings")


def migrate_saved_jobs(sqlite_conn, pg_conn):
    """Migrate saved_jobs table."""
    print("\n📤 Migrating saved jobs...")
    
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    try:
        sqlite_cursor.execute("""
            SELECT chat_id, job_link, job_title, company, location, date_posted,
                   alert_keywords, alert_location, saved_at
            FROM saved_jobs
        """)
        rows = sqlite_cursor.fetchall()
    except sqlite3.OperationalError:
        print("   No saved_jobs table found")
        return
    
    if not rows:
        print("   No saved jobs to migrate")
        return
    
    for row in rows:
        chat_id, job_link, job_title, company, location, date_posted, \
            alert_keywords, alert_location, saved_at = row
        
        pg_cursor.execute("""
            INSERT INTO saved_jobs (chat_id, job_link, job_title, company, location, 
                                    date_posted, alert_keywords, alert_location, saved_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, job_link) DO NOTHING
        """, (chat_id, job_link, job_title, company, location,
              date_posted, alert_keywords, alert_location, saved_at))
    
    pg_conn.commit()
    print(f"   ✅ Migrated {len(rows)} saved jobs")


def migrate_job_details_cache(sqlite_conn, pg_conn, alert_id_map):
    """Migrate job_details_cache table."""
    print("\n📤 Migrating job details cache...")
    
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    try:
        sqlite_cursor.execute("""
            SELECT alert_id, job_id, job_link, job_title, company, location, 
                   date_posted, cached_at
            FROM job_details_cache
        """)
        rows = sqlite_cursor.fetchall()
    except sqlite3.OperationalError:
        print("   No job_details_cache table found")
        return
    
    if not rows:
        print("   No job details cache to migrate")
        return
    
    migrated = 0
    skipped = 0
    
    for row in rows:
        alert_id = row[0]
        if alert_id and alert_id in alert_id_map:
            new_alert_id = alert_id_map[alert_id]
        else:
            skipped += 1
            continue
        
        pg_cursor.execute("""
            INSERT INTO job_details_cache (alert_id, job_id, job_link, job_title, 
                                           company, location, date_posted, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (alert_id, job_id) DO UPDATE SET
                job_link = EXCLUDED.job_link,
                job_title = EXCLUDED.job_title,
                company = EXCLUDED.company,
                location = EXCLUDED.location,
                date_posted = EXCLUDED.date_posted,
                cached_at = EXCLUDED.cached_at
        """, (new_alert_id, row[1], row[2], row[3], row[4], row[5], row[6], row[7]))
        migrated += 1
    
    pg_conn.commit()
    print(f"   ✅ Migrated {migrated} job details cache entries (skipped {skipped})")


def create_indexes(pg_conn):
    """Create performance indexes."""
    print("\n📊 Creating indexes...")
    
    cursor = pg_conn.cursor()
    
    indexes = [
        ("idx_alert_jobid", "CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_jobid ON sent_jobs(alert_id, job_id)"),
        ("idx_chat_jobid", "CREATE INDEX IF NOT EXISTS idx_chat_jobid ON sent_jobs(chat_id, job_id)"),
        ("idx_canonical", "CREATE INDEX IF NOT EXISTS idx_canonical ON sent_jobs(chat_id, canonical_title, canonical_company)"),
        ("idx_alerts_active", "CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(is_active) WHERE is_active = 1"),
        ("idx_alerts_chat_id", "CREATE INDEX IF NOT EXISTS idx_alerts_chat_id ON alerts(chat_id)"),
    ]
    
    for name, sql in indexes:
        try:
            cursor.execute(sql)
            print(f"   ✅ Created index: {name}")
        except Exception as e:
            print(f"   ⚠️ Index {name} warning: {e}")
    
    pg_conn.commit()


def main():
    """Main migration function."""
    print("=" * 60)
    print("🔄 SQLite to PostgreSQL Migration Script")
    print("=" * 60)
    
    # Check if SQLite database exists
    sqlite_db = "job_alerts.db"
    if not os.path.exists(sqlite_db):
        print(f"❌ SQLite database not found: {sqlite_db}")
        print("   Make sure job_alerts.db is in the current directory")
        sys.exit(1)
    
    # Get PostgreSQL config
    pg_config = get_postgres_config()
    print(f"\n📊 PostgreSQL target: {pg_config['host']}:{pg_config['port']}/{pg_config['database']}")
    
    # Connect to databases
    print("\n🔌 Connecting to databases...")
    
    try:
        sqlite_conn = sqlite3.connect(sqlite_db)
        print("   ✅ Connected to SQLite")
    except Exception as e:
        print(f"   ❌ Failed to connect to SQLite: {e}")
        sys.exit(1)
    
    try:
        pg_conn = psycopg2.connect(
            host=pg_config["host"],
            port=pg_config["port"],
            database=pg_config["database"],
            user=pg_config["user"],
            password=pg_config["password"],
            connect_timeout=10
        )
        print("   ✅ Connected to PostgreSQL")
    except Exception as e:
        print(f"   ❌ Failed to connect to PostgreSQL: {e}")
        print("\n   Make sure:")
        print("   1. PostgreSQL server is running")
        print("   2. Database exists (create with: createdb job_alerts)")
        print("   3. Environment variables are set correctly")
        sqlite_conn.close()
        sys.exit(1)
    
    try:
        # Create tables
        create_postgres_tables(pg_conn)
        
        # Migrate data
        alert_id_map = migrate_alerts(sqlite_conn, pg_conn)
        migrate_sent_jobs(sqlite_conn, pg_conn, alert_id_map)
        migrate_user_settings(sqlite_conn, pg_conn)
        migrate_saved_jobs(sqlite_conn, pg_conn)
        migrate_job_details_cache(sqlite_conn, pg_conn, alert_id_map)
        
        # Create indexes
        create_indexes(pg_conn)
        
        print("\n" + "=" * 60)
        print("✅ Migration completed successfully!")
        print("=" * 60)
        print("\n📝 Next steps:")
        print("   1. Verify the migration by checking data in PostgreSQL")
        print("   2. Update your .env file with PostgreSQL credentials")
        print("   3. Backup and rename job_alerts.db (e.g., job_alerts.db.bak)")
        print("   4. Start the bot with: python bot.py")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        pg_conn.rollback()
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
