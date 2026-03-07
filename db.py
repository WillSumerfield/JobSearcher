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
    db.save_digest("2026-03-03", scored_jobs)
    job_dict = db.get_digest_job(3)
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from scraper.models import Job

if TYPE_CHECKING:
    from scorer import ScoredJob

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
    score        REAL NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
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
            # Migrate existing DBs that pre-date the score/reason columns.
            for col, defn in [("score", "REAL NOT NULL DEFAULT 0"),
                               ("reason", "TEXT NOT NULL DEFAULT ''")]:
                try:
                    self._conn.execute(f"ALTER TABLE digest_jobs ADD COLUMN {col} {defn}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

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

    def expire_seen(self) -> int:
        """
        Rolling 7-day window: drop any seen_jobs entry whose seen_at timestamp
        is more than 7 days old.  Called at the start of every scrape run so
        listings age out one-by-one as time passes — no batch weekly reset.
        Returns the number of rows removed.
        """
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM seen_jobs WHERE seen_at < ?", (cutoff,)
            )
        purged = cur.rowcount
        if purged:
            logger.info("expire_seen: removed %d seen_jobs row(s) older than 7 days", purged)
        return purged

    # ------------------------------------------------------------------
    # digest_jobs
    # ------------------------------------------------------------------

    def save_digest(self, digest_date: str, scored_jobs: list[ScoredJob]) -> None:
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
                (digest_date, idx, sj.job.id, _job_to_json(sj.job), sj.score, sj.reason)
                for idx, sj in enumerate(scored_jobs, start=1)
            ]
            self._conn.executemany(
                "INSERT INTO digest_jobs (digest_date, idx, job_id, job_json, score, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        logger.info("save_digest: stored %d jobs for %s", len(scored_jobs), digest_date)

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

    def get_week_top_jobs(self, n: int = 15) -> list[dict]:
        """
        Return the top n jobs (by score desc) across the last 7 digest dates,
        de-duplicated by job_id, as dicts with keys: job (dict), score, reason.
        """
        cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        rows = self._conn.execute(
            "SELECT job_id, job_json, score, reason FROM digest_jobs "
            "WHERE digest_date >= ? ORDER BY score DESC",
            (cutoff,),
        ).fetchall()
        seen: set[str] = set()
        result: list[dict] = []
        for row in rows:
            if row["job_id"] in seen:
                continue
            seen.add(row["job_id"])
            result.append({
                "job": json.loads(row["job_json"]),
                "score": row["score"],
                "reason": row["reason"],
            })
            if len(result) >= n:
                break
        return result


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
