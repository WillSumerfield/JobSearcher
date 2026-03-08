"""
scraper/boards.py
Fetches job listings from Indeed, LinkedIn, and Glassdoor using python-jobspy.

Usage:
    from scraper.boards import scrape_boards
    jobs = scrape_boards(cfg)      # cfg = parsed profile.yaml dict
"""

import json
import logging
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin, parse_qs, unquote, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

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
        hi = _parse_gbp(hi_raw) if hi_raw else None
        # Sanity-check: plausible annual UK salary
        if 10_000 <= lo <= 500_000 and (hi is None or 10_000 <= hi <= 500_000):
            if hi is not None:
                return (lo, hi) if lo <= hi else (hi, lo)
            return lo, None
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
# Welcome to the Jungle (Algolia)
# ---------------------------------------------------------------------------

_WTTJ_APP_ID = "***REMOVED***"
_WTTJ_API_KEY = "***REMOVED***"
_WTTJ_INDEX = "wttj_jobs_production_en"
_WTTJ_HEADERS = {
    "X-Algolia-Application-Id": _WTTJ_APP_ID,
    "X-Algolia-API-Key": _WTTJ_API_KEY,
    "Content-Type": "application/json",
    # Algolia validates origin; must match the site's own domain
    "Origin": "https://www.welcometothejungle.com",
    "Referer": "https://www.welcometothejungle.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
_WTTJ_FIELDS = [
    "name", "slug", "organization", "offices", "summary",
    "salary_yearly_minimum", "salary_maximum", "salary_currency",
    "published_at_timestamp", "remote", "contract_type",
]


def _scrape_wttj(cfg: dict) -> list[Job]:
    """
    Query the Welcome to the Jungle Algolia index for each keyword in the
    search config, filtering to UK-based and/or fully-remote jobs.

    Returns a deduplicated list of Job objects (source = 'welcometothejungle').
    Errors per keyword are logged as warnings; the function never raises.
    """
    from datetime import datetime

    search_cfg = cfg.get("search", {})
    keywords: list[str] = search_cfg.get("keywords", [])
    results_per_board: int = search_cfg.get("results_per_board", 30)
    remote_ok: bool = search_cfg.get("remote_ok", True)

    algolia_url = (
        f"https://{_WTTJ_APP_ID}-dsn.algolia.net/1/indexes/{_WTTJ_INDEX}/query"
    )

    # Run two passes per keyword: UK offices, and (if remote_ok) fully remote
    passes: list[str] = ["offices.country_code:GB"]
    if remote_ok:
        passes.append("remote:full")

    seen_urls: dict[str, Job] = {}

    for keyword in keywords:
        for algolia_filter in passes:
            label = "Remote" if "remote" in algolia_filter else "UK"
            logger.info("WTTJ: scraping '%s' [%s]", keyword, label)
            try:
                resp = requests.post(
                    algolia_url,
                    json={
                        "query": keyword,
                        "hitsPerPage": results_per_board,
                        "filters": algolia_filter,
                        "attributesToRetrieve": _WTTJ_FIELDS,
                    },
                    headers=_WTTJ_HEADERS,
                    timeout=15,
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WTTJ: request failed for '%s' [%s]: %s", keyword, label, exc)
                continue

            for hit in resp.json().get("hits", []):
                org = hit.get("organization") or {}
                org_slug = org.get("slug", "")
                job_slug = hit.get("slug", "")
                if not org_slug or not job_slug:
                    continue
                url = (
                    f"https://www.welcometothejungle.com/en/companies"
                    f"/{org_slug}/jobs/{job_slug}"
                )
                if url in seen_urls:
                    continue

                # Location: prefer city from UK offices; note remote status
                offices = hit.get("offices") or []
                cities = [o["city"] for o in offices if o.get("city")]
                location = ", ".join(dict.fromkeys(cities))  # dedup, preserve order
                remote_status = hit.get("remote") or ""
                if remote_status == "full":
                    location = (f"{location} (Remote)" if location else "Remote")

                # Salary (WTTJ stores annual min/max directly)
                sal_min = hit.get("salary_yearly_minimum")
                sal_max = hit.get("salary_maximum")
                currency = (hit.get("salary_currency") or "GBP").upper()
                # Only use salary if it's GBP; otherwise leave None
                if currency != "GBP":
                    sal_min = sal_max = None

                # Date posted
                ts = hit.get("published_at_timestamp")
                date_posted = (
                    datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else None
                )

                seen_urls[url] = Job(
                    title=hit.get("name", ""),
                    company=org.get("name", ""),
                    location=location,
                    url=url,
                    source="welcometothejungle",
                    description=hit.get("summary") or "",
                    salary_min=float(sal_min) if sal_min is not None else None,
                    salary_max=float(sal_max) if sal_max is not None else None,
                    date_posted=date_posted,
                )

            logger.info("WTTJ: %d unique jobs so far", len(seen_urls))

    return list(seen_urls.values())


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

    # Merge Welcome to the Jungle results into the same dedup dict
    wttj_jobs = _scrape_wttj(cfg)
    wttj_new = 0
    for job in wttj_jobs:
        if job.url not in seen_urls:
            seen_urls[job.url] = job
            wttj_new += 1
    if wttj_new:
        logger.info("WTTJ: added %d new job(s)", wttj_new)

    all_jobs = list(seen_urls.values())
    filtered = _apply_filters(all_jobs, cfg)

    logger.info(
        "Scrape complete: %d raw → %d after dedup+filters",
        len(all_jobs), len(filtered),
    )
    return filtered


# ---------------------------------------------------------------------------
# Description and salary enrichment — follow job URLs to fetch full data
# ---------------------------------------------------------------------------

_ENRICH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# Tags whose content we strip before extracting body text in the description fallback
_STRIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript"}

# Description length below which we fetch the full page for description enrichment
_ENRICH_THRESHOLD = 500

# Minimum seconds between successive requests to a given domain
_DOMAIN_DELAYS: dict[str, float] = {
    "linkedin.com": 3.0,  # ~20 req/min max
}
_DEFAULT_DOMAIN_DELAY = 0.5

# Known ATS domains — used to identify external apply links
_ATS_DOMAINS = frozenset({
    "greenhouse.io", "lever.co", "jobs.lever.co", "haystackapp.io",
    "workday.com", "myworkdayjobs.com", "ashbyhq.com", "smartrecruiters.com",
    "taleo.net", "icims.com", "successfactors.com", "bamboohr.com",
    "recruitee.com", "teamtailor.com", "pinpointhq.com",
})

# Matches embedded JSON salary data (e.g. Apollo/GraphQL state):
# looks for "currency":"GBP" then "min":<digits> within 300 chars
_JSON_SAL_RE = re.compile(
    r'"[Cc]urrency"\s*:\s*"GBP".{0,300}"(?:min|minimum|minValue|salary_min)"\s*:\s*(\d{4,6})',
    re.DOTALL,
)
# Matches the max salary figure that follows a min match
_JSON_SAL_MAX_RE = re.compile(
    r'"(?:max|maximum|maxValue|salary_max)"\s*:\s*(\d{4,6})',
)


def _fetch_page(url: str) -> tuple[BeautifulSoup, str] | None:
    """
    Fetch a URL (with 429 retries) and return (soup, raw_html), or None on failure.
    """
    _MAX_RETRIES = 2
    resp = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=12,
                headers={"User-Agent": _ENRICH_UA},
                allow_redirects=True,
            )
            if resp.status_code == 429:
                if attempt < _MAX_RETRIES:
                    wait = 5 * (2 ** attempt)  # 5s, 10s
                    logger.warning(
                        "enrich: rate-limited (429) fetching %s, retrying in %ds… (%d/%d)",
                        url, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                logger.warning("enrich: blocked (429) after %d retries: %s", _MAX_RETRIES, url)
                return None
            if resp.status_code == 403:
                logger.warning("enrich: blocked (403): %s", url)
                return None
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrich: failed to fetch %s — %s", url, exc)
            return None
    if resp is None:
        return None
    raw = resp.text
    return BeautifulSoup(raw, "html.parser"), raw


def _description_from_soup(soup: BeautifulSoup, job_url: str) -> str | None:
    """
    Extract job description text from a pre-fetched soup.
    Tries board-specific CSS selectors first, then falls back to stripped body text.

    NOTE: the body fallback decomposes script/style tags from the soup in place.
    Always call _salary_from_soup BEFORE this function.
    """
    host = urlparse(job_url).netloc.lower()
    selectors: list[str] = []
    if "linkedin.com" in host:
        selectors = [
            "div.description__text",
            "div[class*='description']",
            "section[class*='description']",
        ]
    elif "indeed.com" in host:
        selectors = ["div#jobDescriptionText", "div[class*='jobDescription']"]

    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if len(text) > 200:
                return text

    # Fallback: strip chrome and return body text (mutates soup)
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    body = soup.find("body")
    if body:
        text = body.get_text(separator=" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text if len(text) > 200 else None
    return None


def _salary_from_soup(soup: BeautifulSoup, raw_html: str) -> tuple[float | None, float | None]:
    """
    Extract a GBP salary from a fetched page. Tries in order:
      1. JSON-LD <script type="application/ld+json"> baseSalary (JobPosting schema)
      2. Embedded <script> JSON blobs with "currency":"GBP" + min/max (Apollo/GraphQL)
      3. <meta name="description"> / og:description / twitter:description text
      4. Full body text

    Call this BEFORE _description_from_soup, which strips script tags from the soup.
    """
    # 1. JSON-LD baseSalary
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                    continue
                sal = item.get("baseSalary") or {}
                val = sal.get("value") or {}
                currency = (sal.get("currency") or "").upper()
                if currency and currency != "GBP":
                    continue
                lo = _safe_float(val.get("minValue"))
                hi = _safe_float(val.get("maxValue"))
                single = _safe_float(val.get("value"))
                if lo and hi and 10_000 <= lo <= 500_000 and 10_000 <= hi <= 500_000:
                    return (lo, hi) if lo <= hi else (hi, lo)
                if lo and 10_000 <= lo <= 500_000:
                    return lo, None
                if single and 10_000 <= single <= 500_000:
                    return single, None
        except Exception:  # noqa: BLE001
            continue

    # 2. Embedded script JSON (Apollo/GraphQL state, etc.)
    for script in soup.find_all("script"):
        if script.get("type") == "application/ld+json":
            continue
        text = script.string or ""
        if "GBP" not in text:
            continue
        m = _JSON_SAL_RE.search(text)
        if m:
            lo = _safe_float(m.group(1))
            mx = _JSON_SAL_MAX_RE.search(text[m.start(): m.start() + 400])
            hi = _safe_float(mx.group(1)) if mx else None
            if lo and 10_000 <= lo <= 500_000:
                if hi and 10_000 <= hi <= 500_000:
                    return (lo, hi) if lo <= hi else (hi, lo)
                return lo, None

    # 3. Meta description tags
    for attrs in (
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ):
        meta = soup.find("meta", attrs)
        if meta:
            content = meta.get("content") or ""
            if content:
                lo, hi = _salary_from_description(content)
                if lo or hi:
                    return lo, hi

    # 4. Body text
    body = soup.find("body")
    if body:
        return _salary_from_description(body.get_text(separator=" ", strip=True))
    return None, None


def _find_apply_url(soup: BeautifulSoup, job_url: str) -> str | None:
    """
    Find the external apply/application URL from a job page.

    For LinkedIn, decodes the destination from externalApply redirect hrefs.
    For other pages, checks for links to known ATS domains, then falls back
    to any external <a> with "apply" in its text, href, or aria-label.

    Returns None if no suitable external link is found.
    """
    base_domain = urlparse(job_url).netloc.lower()

    # LinkedIn: externalApply links encode the real destination in a `url=` param
    if "linkedin.com" in base_domain:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "externalApply" in href or "job-apply" in href:
                full = urljoin(job_url, href)
                qs = parse_qs(urlparse(full).query)
                if "url" in qs:
                    decoded = unquote(qs["url"][0])
                    if decoded.startswith("http"):
                        return decoded
                if urlparse(full).netloc != base_domain:
                    return full

    # Known ATS domains — most reliable signal
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full = urljoin(job_url, href)
        link_domain = urlparse(full).netloc.lower()
        if link_domain == base_domain:
            continue
        if any(ats in link_domain for ats in _ATS_DOMAINS):
            return full

    # Generic fallback: external <a> with "apply" in text, href, or aria-label
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full = urljoin(job_url, href)
        link_domain = urlparse(full).netloc.lower()
        if link_domain == base_domain:
            continue
        text = a.get_text(strip=True).lower()
        aria = (a.get("aria-label") or "").lower()
        if "apply" in href.lower() or "apply" in text or "apply" in aria:
            return full

    return None


def enrich_descriptions(jobs: list[Job], concurrency: int = 8) -> None:
    """
    Fetch full job descriptions and salary data in parallel. Mutates jobs in place.

    Description enrichment: jobs whose description is shorter than _ENRICH_THRESHOLD.
    Salary enrichment: jobs with no salary (both salary_min and salary_max are None).
      - Primary pass: fetch job.url, extract salary from JSON-LD / embedded JSON /
        meta tags / body text.
      - Secondary pass: for jobs still without salary, follow the external apply link
        (e.g. Greenhouse, Lever, Haystack) and repeat salary extraction there.

    Important: salary extraction runs before description extraction per job, because
    _description_from_soup's body fallback strips script tags from the soup in place.
    """
    import threading

    needs_desc = {j for j in jobs if len(j.description or "") < _ENRICH_THRESHOLD}
    needs_salary = {j for j in jobs if j.salary_min is None and j.salary_max is None}
    to_fetch = needs_desc | needs_salary

    if not to_fetch:
        logger.info("enrich: all jobs already have sufficient descriptions and salary data.")
        return

    logger.info(
        "enrich: fetching %d job page(s) (%d need description, %d need salary)…",
        len(to_fetch), len(needs_desc), len(needs_salary),
    )

    # Per-domain throttling shared across both fetch passes
    domain_last_hit: dict[str, float] = {}
    domain_lock_map: dict[str, threading.Lock] = {}
    global_lock = threading.Lock()

    def _get_domain_lock(host: str) -> threading.Lock:
        with global_lock:
            if host not in domain_lock_map:
                domain_lock_map[host] = threading.Lock()
        return domain_lock_map[host]

    def _throttled_get(url: str) -> tuple[BeautifulSoup, str] | None:
        host = urlparse(url).netloc.lower()
        with _get_domain_lock(host):
            now = time.monotonic()
            gap = now - domain_last_hit.get(host, 0)
            delay = next(
                (v for k, v in _DOMAIN_DELAYS.items() if k in host),
                _DEFAULT_DOMAIN_DELAY,
            )
            if gap < delay:
                time.sleep(delay - gap)
            domain_last_hit[host] = time.monotonic()
        return _fetch_page(url)

    def _fetch_primary(job: Job) -> tuple[Job, tuple[BeautifulSoup, str] | None]:
        return job, _throttled_get(job.url)

    desc_enriched = salary_enriched = 0
    needs_apply_fetch: list[tuple[Job, BeautifulSoup, str]] = []

    # First pass: fetch each job's primary URL
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_fetch_primary, job): job for job in to_fetch}
        for future in as_completed(futures):
            job, result = future.result()
            if result is None:
                continue
            soup, raw = result

            # Salary before description — _description_from_soup may strip script tags
            if job in needs_salary:
                lo, hi = _salary_from_soup(soup, raw)
                if lo or hi:
                    job.salary_min, job.salary_max = lo, hi
                    salary_enriched += 1
                else:
                    needs_apply_fetch.append((job, soup, raw))

            if job in needs_desc:
                text = _description_from_soup(soup, job.url)
                if text and len(text) > len(job.description or ""):
                    job.description = text
                    desc_enriched += 1

    # Second pass: follow external apply links for jobs still without salary
    if needs_apply_fetch:
        logger.info(
            "enrich: following apply links for %d job(s) still missing salary…",
            len(needs_apply_fetch),
        )
        apply_targets: list[tuple[Job, str]] = []
        for job, soup, raw in needs_apply_fetch:
            apply_url = _find_apply_url(soup, job.url)
            if apply_url:
                apply_targets.append((job, apply_url))
                logger.debug(
                    "enrich: apply URL for '%s' @ %s → %s", job.title, job.company, apply_url
                )
            else:
                logger.debug("enrich: no apply URL found for '%s' @ %s", job.title, job.company)

        def _fetch_apply(args: tuple[Job, str]) -> tuple[Job, tuple[BeautifulSoup, str] | None]:
            job, url = args
            return job, _throttled_get(url)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_apply, item): item for item in apply_targets}
            for future in as_completed(futures):
                job, result = future.result()
                if result is None:
                    continue
                soup, raw = result
                lo, hi = _salary_from_soup(soup, raw)
                if lo or hi:
                    job.salary_min, job.salary_max = lo, hi
                    salary_enriched += 1

    logger.info(
        "enrich: done — %d description(s) enriched, %d salary/ies resolved.",
        desc_enriched, salary_enriched,
    )
