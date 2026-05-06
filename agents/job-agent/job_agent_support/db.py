"""
job_agent_support/db.py
SQLite helper: applications, apply_later, and dead_listings tables.

Tables
------
applications   — jobs that were applied to
apply_later    — easy-apply jobs saved for later (user chose not to apply immediately)
dead_listings  — jobs where the posting was no longer available (separate so
                 applications remains a clean record of what was actually applied to)
"""

import sqlite3
from pathlib import Path


def init_db(db_path: Path) -> None:
    """Create all tables if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title    TEXT    NOT NULL,
                company      TEXT    NOT NULL,
                job_board    TEXT,
                date_applied TEXT    NOT NULL,
                pay          TEXT,
                address      TEXT,
                apply_url    TEXT,
                resume_path  TEXT,
                easy_apply   INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'applied'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS apply_later (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title    TEXT    NOT NULL,
                company      TEXT    NOT NULL,
                job_board    TEXT,
                date_saved   TEXT    NOT NULL,
                pay          TEXT,
                address      TEXT,
                apply_url    TEXT,
                resume_path  TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dead_listings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title    TEXT,
                company      TEXT,
                job_board    TEXT,
                date_found   TEXT    NOT NULL,
                listing_url  TEXT,
                reason       TEXT DEFAULT 'job_not_found'
            )
        """)
        conn.commit()


def is_dead_listing(db_path: Path, url: str = None,
                    job_title: str = None, company: str = None) -> bool:
    """Return True if this listing was previously logged as not found."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        if url:
            row = conn.execute(
                "SELECT 1 FROM dead_listings WHERE listing_url = ? LIMIT 1", (url,)
            ).fetchone()
            if row:
                return True
        if job_title and company:
            row = conn.execute(
                "SELECT 1 FROM dead_listings WHERE lower(job_title) = lower(?) "
                "AND lower(company) = lower(?) LIMIT 1",
                (job_title, company),
            ).fetchone()
            if row:
                return True
    return False


def log_dead_listing(db_path: Path, record: dict) -> int:
    """
    Insert a dead-listing record. Returns the new row id.
    record keys: job_title, company, job_board, date_found, listing_url, reason
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO dead_listings
                (job_title, company, job_board, date_found, listing_url, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("job_title", ""),
                record.get("company", ""),
                record.get("job_board", "direct"),
                record.get("date_found", ""),
                record.get("listing_url"),
                record.get("reason", "job_not_found"),
            ),
        )
        return cursor.lastrowid


def already_applied(db_path: Path, url: str = None,
                    job_title: str = None, company: str = None) -> bool:
    """Return True if a matching application is already in the DB."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        if url:
            row = conn.execute(
                "SELECT 1 FROM applications WHERE apply_url = ? LIMIT 1", (url,)
            ).fetchone()
            if row:
                return True
        if job_title and company:
            row = conn.execute(
                "SELECT 1 FROM applications WHERE lower(job_title) = lower(?) "
                "AND lower(company) = lower(?) LIMIT 1",
                (job_title, company),
            ).fetchone()
            if row:
                return True
    return False


def is_apply_later(db_path: Path, url: str = None,
                   job_title: str = None, company: str = None) -> bool:
    """Return True if this job is already in the apply_later table."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        if url:
            row = conn.execute(
                "SELECT 1 FROM apply_later WHERE apply_url = ? LIMIT 1", (url,)
            ).fetchone()
            if row:
                return True
        if job_title and company:
            row = conn.execute(
                "SELECT 1 FROM apply_later WHERE lower(job_title) = lower(?) "
                "AND lower(company) = lower(?) LIMIT 1",
                (job_title, company),
            ).fetchone()
            if row:
                return True
    return False


def log_apply_later(db_path: Path, record: dict) -> int:
    """
    Insert a job into the apply_later table. Returns the new row id.
    record keys: job_title, company, job_board, date_saved,
                 pay, address, apply_url, resume_path
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO apply_later
                (job_title, company, job_board, date_saved, pay, address,
                 apply_url, resume_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("job_title", ""),
                record.get("company", ""),
                record.get("job_board", "direct"),
                record.get("date_saved", ""),
                record.get("pay"),
                record.get("address"),
                record.get("apply_url"),
                record.get("resume_path"),
            ),
        )
        return cursor.lastrowid


def log_application(db_path: Path, record: dict) -> int:
    """
    Insert a new application record. Returns the new row id.
    record keys: job_title, company, job_board, date_applied,
                 pay, address, apply_url, resume_path, easy_apply
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO applications
                (job_title, company, job_board, date_applied, pay, address,
                 apply_url, resume_path, easy_apply)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("job_title", ""),
                record.get("company", ""),
                record.get("job_board", "direct"),
                record.get("date_applied", ""),
                record.get("pay"),
                record.get("address"),
                record.get("apply_url"),
                record.get("resume_path"),
                record.get("easy_apply", 0),
            ),
        )
        return cursor.lastrowid
