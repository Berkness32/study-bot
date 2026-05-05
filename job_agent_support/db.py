"""
job_agent_support/db.py
SQLite helper: initialize the applications table and insert records.
"""

import sqlite3
from pathlib import Path


def init_db(db_path: Path) -> None:
    """Create the applications table if it doesn't exist."""
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
        conn.commit()


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
