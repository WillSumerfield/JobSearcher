"""
scorer.py — Two-stage job relevance scoring.

Stage 1 — Keyword filter (free, instant):
    Score each job by how many of the applicant's skills appear in the
    job description.  Drop jobs with zero matches; keep the top N for
    Claude.  Jobs outside the shortlist are appended at the end with
    score=0 so they still appear in the digest but sort to the bottom.

Stage 2 — Claude ranking (API call):
    Send the shortlist to claude-sonnet-4-6 as a single prompt.
    Claude returns a JSON array of {index, score (1-10), reason} entries.
    If the API call fails for any reason, falls back to normalised
    keyword scores so the digest still goes out.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass

from scraper.models import Job

logger = logging.getLogger(__name__)

# How many keyword-ranked jobs to forward to Claude for deep scoring.
# Larger = better coverage but slower + more expensive per run.
CLAUDE_SHORTLIST_SIZE = 40

# Jobs with fewer than this many skill matches are considered irrelevant
# and won't be sent to Claude (they still appear at the bottom of the digest).
KEYWORD_MIN_MATCHES = 1


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

@dataclass
class ScoredJob:
    job: Job
    score: float   # 1.0–10.0 (Claude) or 0.0 (below keyword threshold)
    reason: str    # Claude's one-sentence explanation, or "" for fallback


def score_jobs(jobs: list[Job], cfg: dict) -> list[ScoredJob]:
    """
    Score and rank jobs in two stages:
      1. Keyword / skill match filter (fast, free)
      2. Claude ranking of the shortlist

    Returns a list sorted by score descending.  All input jobs are
    included: those outside the shortlist appear at the end with score=0.
    """
    if not jobs:
        return []

    # --- Stage 1: keyword scoring ---
    kw_pairs = _keyword_score(jobs, cfg)       # [(job, match_count), ...]
    eligible = [(j, s) for j, s in kw_pairs if s >= KEYWORD_MIN_MATCHES]
    eligible.sort(key=lambda x: x[1], reverse=True)
    shortlist = eligible[:CLAUDE_SHORTLIST_SIZE]
    below_threshold = [j for j, s in kw_pairs if s < KEYWORD_MIN_MATCHES]

    logger.info(
        "Keyword stage: %d eligible (≥%d match), %d shortlisted for Claude, %d below threshold",
        len(eligible), KEYWORD_MIN_MATCHES, len(shortlist), len(below_threshold),
    )

    # --- Stage 2: Claude ranking ---
    claude_map: dict[int, dict] | None = None
    if shortlist:
        try:
            claude_map = _claude_rank([j for j, _ in shortlist], cfg)
            logger.info("Claude scored %d jobs.", len(claude_map))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Claude ranking failed (%s) — falling back to keyword scores.", exc)

    # --- Merge results ---
    result: list[ScoredJob] = []

    if claude_map:
        for i, (job, _) in enumerate(shortlist):
            entry = claude_map.get(i + 1)
            if entry:
                result.append(ScoredJob(job=job, score=entry["score"], reason=entry["reason"]))
            else:
                result.append(ScoredJob(job=job, score=5.0, reason=""))
        result.sort(key=lambda sj: sj.score, reverse=True)
    else:
        # Fallback: normalise keyword counts to 1–10
        max_kw = max((s for _, s in eligible), default=1)
        for job, kw_score in shortlist:
            norm = round(min(10.0, kw_score / max_kw * 10), 1)
            result.append(ScoredJob(job=job, score=norm, reason=""))
        result.sort(key=lambda sj: sj.score, reverse=True)

    # Jobs that didn't make the keyword cut — append at the bottom
    for job in below_threshold:
        result.append(ScoredJob(job=job, score=0.0, reason=""))

    return result


# ---------------------------------------------------------------------------
# Stage 1: keyword scoring
# ---------------------------------------------------------------------------

_STOP_WORDS = {"and", "the", "for", "with", "based", "our", "your", "you"}


def _extract_skill_terms(cfg: dict) -> list[str]:
    """
    Build a flat list of lowercase search terms from the skills list.

    E.g. "Python (pandas, scikit-learn, numpy)"
      → ["python", "pandas", "scikit", "learn", "numpy"]
    """
    terms: set[str] = set()
    for skill in cfg.get("skills", []):
        # Pull out contiguous alphanumeric tokens (handles parentheses, slashes, etc.)
        words = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", skill)
        for w in words:
            if w.lower() not in _STOP_WORDS:
                terms.add(w.lower())
    return list(terms)


def _keyword_score(jobs: list[Job], cfg: dict) -> list[tuple[Job, int]]:
    """Return (job, skill_match_count) for every job."""
    terms = _extract_skill_terms(cfg)
    if not terms:
        return [(j, 1) for j in jobs]  # no skills configured → treat all as equal

    results: list[tuple[Job, int]] = []
    for job in jobs:
        haystack = f"{job.title} {job.description}".lower()
        matches = sum(1 for t in terms if t in haystack)
        results.append((job, matches))
    return results


# ---------------------------------------------------------------------------
# Stage 2: Claude ranking
# ---------------------------------------------------------------------------

def _build_prompt(jobs: list[Job], cfg: dict) -> str:
    skills_text = ", ".join(cfg.get("skills", []))
    education_text = "; ".join(cfg.get("education", []))
    experience_text = (cfg.get("experience_summary") or "").strip()

    job_entries: list[str] = []
    for i, job in enumerate(jobs, start=1):
        # Truncate description to keep the prompt from ballooning
        desc = (job.description or "").replace("\n", " ").strip()[:2000]
        salary = job.salary_display()
        job_entries.append(
            f"{i}. {job.title} @ {job.company} | {job.location} | {salary}\n"
            f"   {desc}"
        )

    jobs_block = "\n\n".join(job_entries)

    return f"""\
You are scoring job listings for a specific applicant. Rate each job 1–10 for fit.

APPLICANT PROFILE
Education:  {education_text}
Experience: {experience_text}
Skills:     {skills_text}

PREFERENCES
- Target roles: Senior Data Scientist, Data Scientist, Quantitative Research Analyst, \
Statistical Scientist / Modeller, Applied Data Scientist
- Preferred sectors: fintech (Monzo, Revolut, Wise), asset management \
(BlackRock, Schroders, Vanguard), scaled tech (Amazon, Deliveroo)
- Open to: any intellectual sector; contract or permanent
- Avoid: pharma (unless pure data science), deep learning, MLOps, software engineering
- Location: London (in-person or hybrid) or fully remote UK

SCORING GUIDE
9–10  Excellent — right role type, strong skill match, good company/sector
7–8   Good — right type of role, skills mostly align
5–6   Moderate — relevant but some mismatch (wrong level, missing key skills)
3–4   Weak — tangentially related (Data Analyst, wrong sector)
1–2   Poor — wrong role entirely

JOBS TO SCORE
{jobs_block}

Return ONLY a valid JSON array, no markdown fences, no extra text.
Write each reason in second person, addressing the applicant directly as "you" \
(e.g. "Great fit for you — your PySpark background…", not "her" or "him").
[
  {{"index": 1, "score": 8.5, "reason": "One concise sentence addressed to 'you'."}},
  ...
]"""


def _claude_rank(jobs: list[Job], cfg: dict) -> dict[int, dict]:
    """
    Call Claude via the CLI to score the shortlisted jobs.

    Returns a dict mapping 1-based job index → {score: float, reason: str}.
    Raises on CLI or parse errors (caller falls back gracefully).
    """
    prompt = _build_prompt(jobs, cfg)

    logger.info("Sending %d jobs to Claude for ranking…", len(jobs))
    import os
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=180, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()}")

    raw = result.stdout.strip()

    # Strip accidental markdown fences (```json … ```)
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)

    return {
        int(item["index"]): {
            "score": float(item["score"]),
            "reason": str(item.get("reason", "")),
        }
        for item in data
    }
