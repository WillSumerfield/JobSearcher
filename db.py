"""
db.py — SQLite persistence for JobSearcher.

Two tables:
  seen_jobs   — tracks every job_id we've shown, for cross-run deduplication.
                Rows older than 7 days are purged at the start of each scrape
                so that stale listings can resurface after a week.
  digest_jobs — maps today's 1-based digest index → full job JSON so the
                IMAP daemon can look up which job the user asked for.

Usage:
    from db import Database
    db = Database()          # opens/creates jobs.db in the project root
    db.reset_week()
    new_jobs = [j for j in scraped if not db.is_seen(j.id)]
    for job in new_jobs:
        db.mark_seen(job.id, job.source)
    db.save_digest("2026-03-03", new_jobs)
    job_dict = db.get_digest_job(3)
"""

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from scraper.models import Job

logger = logging.getLogger(__name__)

DB_PATH = Path("jobs.db")

_CREATE_SEEN_JOBS = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id   TEXT PRIMARY KEY,
    source   TEXT NOT NULL,
    seen_at  TEXT NOT NULL        -- ISO-8601 timestamp
);
"""

_CREATE_DIGEST_JOBS = """
CREATE TABLE IF NOT EXISTS digest_jobs (
    digest_date  TEXT NOT NULL,
    idx          INTEGER NOT NULL, -- 1-based index shown in the email
    job_id       TEXT NOT NULL,
    job_json     TEXT NOT NULL,    -- full Job serialised as JSON
    PRIMARY KEY (digest_date, idx)
);
"""


class Database:
    """Thin wrapper around the jobs.db SQLite database."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._conn: sqlite3.Connection = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_SEEN_JOBS)
            self._conn.execute(_CREATE_DIGEST_JOBS)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # seen_jobs
    # ------------------------------------------------------------------

    def is_seen(self, job_id: str) -> bool:
        """Return True if this job_id has already been shown this week."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, job_id: str, source: str) -> None:
        """Record that a job has been shown."""
        now = datetime.utcnow().isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_jobs (job_id, source, seen_at) VALUES (?, ?, ?)",
                (job_id, source, now),
            )

    def mark_seen_bulk(self, jobs: list[Job]) -> None:
        """Mark multiple jobs as seen in a single transaction."""
        now = datetime.utcnow().isoformat()
        rows = [(j.id, j.source, now) for j in jobs]
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_jobs (job_id, source, seen_at) VALUES (?, ?, ?)",
                rows,
            )

    def reset_week(self) -> int:
        """
        Delete seen_jobs rows older than 7 days so stale listings can resurface.
        Returns the number of rows purged.
        """
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM seen_jobs WHERE seen_at < ?", (cutoff,)
            )
        purged = cur.rowcount
        if purged:
            logger.info("reset_week: purged %d stale seen_jobs row(s)", purged)
        return purged

    # ------------------------------------------------------------------
    # digest_jobs
    # ------------------------------------------------------------------

    def save_digest(self, digest_date: str, jobs: list[Job]) -> None:
        """
        Persist today's ranked job list so the daemon can map email reply
        indices back to full job objects.

        Replaces any existing digest for the same date.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM digest_jobs WHERE digest_date = ?", (digest_date,)
            )
            rows = [
                (digest_date, idx, job.id, _job_to_json(job))
                for idx, job in enumerate(jobs, start=1)
            ]
            self._conn.executemany(
                "INSERT INTO digest_jobs (digest_date, idx, job_id, job_json) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        logger.info("save_digest: stored %d jobs for %s", len(jobs), digest_date)

    def get_digest_job(self, index: int, digest_date: Optional[str] = None) -> Optional[dict]:
        """
        Look up a job by its 1-based digest index.

        If digest_date is None, uses the most recent digest date in the DB.
        Returns the job as a plain dict, or None if not found.
        """
        if digest_date is None:
            row = self._conn.execute(
                "SELECT digest_date FROM digest_jobs ORDER BY digest_date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            digest_date = row["digest_date"]

        row = self._conn.execute(
            "SELECT job_json FROM digest_jobs WHERE digest_date = ? AND idx = ?",
            (digest_date, index),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["job_json"])

    def get_digest_date(self) -> Optional[str]:
        """Return the most recent digest date, or None if no digest exists."""
        row = self._conn.execute(
            "SELECT digest_date FROM digest_jobs ORDER BY digest_date DESC LIMIT 1"
        ).fetchone()
        return row["digest_date"] if row else None


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _job_to_json(job: Job) -> str:
    d = dataclasses.asdict(job)
    return json.dumps(d)


def job_from_dict(d: dict) -> Job:
    """Reconstruct a Job from a plain dict (e.g. loaded from DB)."""
    # id is computed in __post_init__, so we rebuild then overwrite it
    j = Job(
        title=d["title"],
        company=d["company"],
        location=d["location"],
        url=d["url"],
        source=d["source"],
        description=d.get("description", ""),
        salary_min=d.get("salary_min"),
        salary_max=d.get("salary_max"),
        date_posted=d.get("date_posted"),
    )
    j.id = d["id"]  # restore the original id rather than recomputing
    return j
