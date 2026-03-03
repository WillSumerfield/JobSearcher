"""
scorer.py — Job relevance scoring.

Currently a dummy implementation: every job gets score=5.0 and no reasoning.
Will be replaced with keyword pre-filter + Claude ranking in a later step.

The ScoredJob dataclass is the shared type used by email_handler to format
the digest, so the interface is fixed even though the logic is a placeholder.
"""

from dataclasses import dataclass

from scraper.models import Job


@dataclass
class ScoredJob:
    job: Job
    score: float   # 1.0–10.0
    reason: str    # Claude's explanation; empty string until real scorer is built


def score_jobs(jobs: list[Job], cfg: dict) -> list[ScoredJob]:  # noqa: ARG001
    """
    Score and rank a list of jobs against the applicant's profile.

    Dummy implementation: returns all jobs in original order, score=5.0.
    A real implementation will:
      1. Keyword-filter by skill/keyword overlap (fast, no API cost)
      2. Send the shortlist to Claude claude-sonnet-4-6 for 1-10 ranking with reasoning
    """
    return [ScoredJob(job=j, score=5.0, reason="") for j in jobs]
