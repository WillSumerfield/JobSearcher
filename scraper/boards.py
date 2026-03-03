"""
scraper/boards.py
Fetches job listings from Indeed, LinkedIn, and Glassdoor using python-jobspy.

Usage:
    from scraper.boards import scrape_boards
    jobs = scrape_boards(cfg)      # cfg = parsed profile.yaml dict
"""

import logging
import re
import warnings
from typing import Any

import pandas as pd

from scraper.models import Job

logger = logging.getLogger(__name__)

# jobspy emits noisy deprecation warnings from internal deps; suppress them.
warnings.filterwarnings("ignore", category=FutureWarning)

# Matches GBP salary figures: £50,000 / £50k / £50.5k
_GBP_AMOUNT = r"£\s*(\d[\d,]*(?:\.\d+)?)\s*k?"

# Matches a salary range or single figure in job description text
_SALARY_RE = re.compile(
    r"(?:"
    r"(?P<lo>" + _GBP_AMOUNT + r")"          # lower bound (or solo figure)
    r"(?:\s*(?:[-–—]|to)\s*"
    r"(?P<hi>" + _GBP_AMOUNT + r"))?"         # optional upper bound
    r")",
    re.IGNORECASE,
)


def _parse_gbp(raw: str) -> float:
    """Convert a raw matched amount string (e.g. '£50,000', '£50k', '50') to annual GBP."""
    cleaned = raw.replace("£", "").replace(",", "").strip()
    has_k = cleaned.lower().endswith("k")
    if has_k:
        cleaned = cleaned[:-1]
    val = float(cleaned)
    return val * 1000 if (has_k or val < 1000) else val


def _salary_from_description(description: str) -> tuple[float | None, float | None]:
    """
    Fallback: scan the job description for GBP salary figures.
    Returns (min, max) annualised, or (None, None) if nothing found.
    """
    if not description:
        return None, None
    for m in _SALARY_RE.finditer(description):
        lo_raw = m.group("lo")
        hi_raw = m.group("hi")
        if not lo_raw:
            continue
        lo = _parse_gbp(lo_raw)
        hi = _parse_gbp(hi_raw) if hi_raw else lo
        # Sanity-check: plausible annual UK salary
        if 10_000 <= lo <= 500_000 and 10_000 <= hi <= 500_000:
            return (lo, hi) if lo <= hi else (hi, lo)
    return None, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    """Convert a value to float, returning None on failure."""
    try:
        f = float(value)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _salary_from_row(row: pd.Series) -> tuple[float | None, float | None]:
    """
    jobspy exposes salary as either:
      - min_amount / max_amount columns  (numeric)
      - interval column ("yearly", "monthly", etc.)
    We normalise everything to annual GBP.
    """
    lo = _safe_float(row.get("min_amount"))
    hi = _safe_float(row.get("max_amount"))
    interval = str(row.get("interval", "")).lower()

    # Convert to annual if interval is not yearly
    multiplier = 1
    if interval == "monthly":
        multiplier = 12
    elif interval == "weekly":
        multiplier = 52
    elif interval == "hourly":
        multiplier = 2080  # 40h/week × 52 weeks

    if lo:
        lo *= multiplier
    if hi:
        hi *= multiplier

    if lo or hi:
        return lo, hi

    # Fallback: scan the description text for GBP salary figures
    return _salary_from_description(str(row.get("description") or ""))


def _row_to_job(row: pd.Series, source: str) -> Job:
    salary_min, salary_max = _salary_from_row(row)

    # jobspy returns location as a string; normalise None/NaN to empty string
    location = row.get("location") or ""
    if not isinstance(location, str):
        location = str(location) if pd.notna(location) else ""

    description = row.get("description") or ""
    if not isinstance(description, str):
        description = str(description) if pd.notna(description) else ""

    date_posted = row.get("date_posted")
    if pd.isna(date_posted) if date_posted is not None else False:
        date_posted = None
    else:
        date_posted = str(date_posted) if date_posted else None

    return Job(
        title=str(row.get("title") or ""),
        company=str(row.get("company") or ""),
        location=location,
        url=str(row.get("job_url") or ""),
        source=source,
        description=description,
        salary_min=salary_min,
        salary_max=salary_max,
        date_posted=date_posted,
    )


def _apply_filters(jobs: list[Job], cfg: dict) -> list[Job]:
    """Apply exclude_titles and salary_min filters."""
    exclude = [t.lower() for t in cfg.get("exclude_titles", [])]
    salary_floor = cfg.get("search", {}).get("salary_min_gbp", 0)

    filtered: list[Job] = []
    for job in jobs:
        title_lower = job.title.lower()

        # Exclude by title substring
        if any(ex in title_lower for ex in exclude):
            logger.debug("Excluded (title): %s @ %s", job.title, job.company)
            continue

        # Salary filter — only drop if max salary is KNOWN and below floor
        if job.salary_max is not None and job.salary_max < salary_floor:
            logger.debug(
                "Excluded (salary £%s < floor £%s): %s @ %s",
                int(job.salary_max), int(salary_floor), job.title, job.company,
            )
            continue

        filtered.append(job)

    return filtered


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_boards(cfg: dict) -> list[Job]:
    """
    Scrape Indeed, LinkedIn, and Glassdoor for every keyword in cfg.

    Each keyword is searched twice:
      - location = configured location (e.g. "London, United Kingdom")
      - location = "Remote" (if remote_ok is set)

    Results are merged and deduplicated by URL before filters are applied.

    Args:
        cfg: Parsed profile.yaml as a dict.

    Returns:
        List of Job objects after dedup + filters.
    """
    try:
        import jobspy  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "python-jobspy is not installed. Run: pip install python-jobspy"
        ) from exc

    search_cfg = cfg.get("search", {})
    keywords: list[str] = search_cfg.get("keywords", [])
    location: str = search_cfg.get("location", "London, United Kingdom")
    remote_ok: bool = search_cfg.get("remote_ok", True)
    results_per_board: int = search_cfg.get("results_per_board", 30)

    # Glassdoor frequently returns 400 errors; use only stable boards.
    boards = ["indeed", "linkedin"]

    # Collect all raw rows keyed by job_url for deduplication
    seen_urls: dict[str, Job] = {}
    board_errors: list[str] = []

    # We run two passes per keyword: one for the configured location and,
    # if remote_ok, one with remote_only=True (avoids geocoding "Remote").
    search_passes: list[dict] = [
        {"location": location, "remote_only": False},
    ]
    if remote_ok:
        search_passes.append({"location": location, "remote_only": True})

    for keyword in keywords:
        for pass_cfg in search_passes:
            loc_label = "Remote" if pass_cfg["remote_only"] else pass_cfg["location"]
            logger.info("Scraping: '%s' in '%s'", keyword, loc_label)
            try:
                df: pd.DataFrame = jobspy.scrape_jobs(
                    site_name=boards,
                    search_term=keyword,
                    location=pass_cfg["location"],
                    remote_only=pass_cfg["remote_only"],
                    results_wanted=results_per_board,
                    hours_old=24,
                    country_indeed="UK",
                    verbose=0,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"Board scrape failed for '{keyword}' @ '{loc_label}': {exc}"
                logger.warning(msg)
                board_errors.append(msg)
                continue

            if df is None or df.empty:
                logger.info("  → No results for '%s' @ '%s'", keyword, loc_label)
                continue

            for _, row in df.iterrows():
                url = str(row.get("job_url") or "")
                if not url or url in seen_urls:
                    continue
                source = str(row.get("site") or "unknown")
                job = _row_to_job(row, source)
                seen_urls[url] = job

            logger.info("  → %d unique jobs so far", len(seen_urls))

    if board_errors:
        logger.warning(
            "%d scrape error(s) occurred (boards may have rate-limited).",
            len(board_errors),
        )

    all_jobs = list(seen_urls.values())
    filtered = _apply_filters(all_jobs, cfg)

    logger.info(
        "Scrape complete: %d raw → %d after dedup+filters",
        len(all_jobs), len(filtered),
    )
    return filtered
