"""
scraper/companies.py
Scrapes specific company career pages for job listings.

Supports Greenhouse, Lever, and Ashby ATS platforms via their public APIs.
Falls back to generic HTML scraping for other platforms.

Config schema (one entry in profile.yaml → target_companies):
    - name: "Monzo"
      careers_url: "https://monzo.com/careers/"
      ats: greenhouse          # greenhouse | lever | ashby | generic
      ats_token: monzo         # omit for 'generic'
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from scraper.models import Job

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_patterns(cfg: dict) -> tuple[list[re.Pattern], list[str]]:
    """Return (keyword_patterns, exclude_title_substrings) from cfg."""
    keywords = cfg.get("search", {}).get("keywords", [])
    kw_patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords]
    exclude = [t.lower() for t in cfg.get("exclude_titles", [])]
    return kw_patterns, exclude


def _matches_keywords(text: str, kw_patterns: list[re.Pattern]) -> bool:
    return any(p.search(text) for p in kw_patterns)


def _matches_exclude(title: str, excl_patterns: list[str]) -> bool:
    title_lower = title.lower()
    return any(ex in title_lower for ex in excl_patterns)


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _epoch_ms_to_date(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ATS backends
# ---------------------------------------------------------------------------

def _scrape_greenhouse(token: str, company_name: str,
                       kw_patterns: list[re.Pattern],
                       excl_patterns: list[str]) -> list[Job]:
    url = f"https://boards.greenhouse.io/v1/boards/{token}/jobs?content=true"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Greenhouse fetch failed for %s (token=%s): %s", company_name, token, exc)
        return []

    jobs: list[Job] = []
    for item in data.get("jobs", []):
        title = item.get("title", "")
        if _matches_exclude(title, excl_patterns):
            continue
        description = _strip_html(item.get("content", ""))
        if not _matches_keywords(title + " " + description, kw_patterns):
            continue

        location = (item.get("location") or {}).get("name", "") or ""
        job_url = item.get("absolute_url", "")
        updated = item.get("updated_at", "")
        date_posted = updated[:10] if updated else None

        jobs.append(Job(
            title=title,
            company=company_name,
            location=location,
            url=job_url,
            source="company_careers",
            description=description,
            date_posted=date_posted,
        ))

    logger.info("Greenhouse %s (%s): %d job(s) matched", company_name, token, len(jobs))
    return jobs


def _scrape_lever(slug: str, company_name: str,
                  kw_patterns: list[re.Pattern],
                  excl_patterns: list[str]) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        postings = resp.json()
    except Exception as exc:
        logger.warning("Lever fetch failed for %s (slug=%s): %s", company_name, slug, exc)
        return []

    jobs: list[Job] = []
    for p in postings:
        title = p.get("text", "")
        if _matches_exclude(title, excl_patterns):
            continue
        # Prefer plaintext description; fall back to HTML-stripped
        desc_plain = p.get("descriptionPlain", "") or _strip_html(p.get("description", ""))
        if not _matches_keywords(title + " " + desc_plain, kw_patterns):
            continue

        cats = p.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        location = cats.get("location") or (all_locs[0] if all_locs else "")
        job_url = p.get("hostedUrl", "")
        date_posted = _epoch_ms_to_date(p.get("createdAt"))

        jobs.append(Job(
            title=title,
            company=company_name,
            location=str(location),
            url=job_url,
            source="company_careers",
            description=desc_plain,
            date_posted=date_posted,
        ))

    logger.info("Lever %s (%s): %d job(s) matched", company_name, slug, len(jobs))
    return jobs


def _scrape_ashby(org: str, company_name: str,
                  kw_patterns: list[re.Pattern],
                  excl_patterns: list[str]) -> list[Job]:
    # Ashby public posting API: path-based org slug, returns JSON with 'jobs' array
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
    try:
        resp = requests.get(
            url,
            headers={**_HEADERS, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Ashby fetch failed for %s (org=%s): %s", company_name, org, exc)
        return []

    jobs: list[Job] = []
    for item in data.get("jobs", []):
        title = item.get("title", "")
        if _matches_exclude(title, excl_patterns):
            continue
        # Prefer plain text; fall back to stripping HTML
        description = item.get("descriptionPlain", "") or _strip_html(item.get("descriptionHtml", ""))
        if not _matches_keywords(title + " " + description, kw_patterns):
            continue

        # location is a plain string in this API version
        location = item.get("location", "") or ""

        job_url = item.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{org}/{item.get('id', '')}"

        published = item.get("publishedAt", "")
        date_posted = published[:10] if published else None

        jobs.append(Job(
            title=title,
            company=company_name,
            location=str(location),
            url=job_url,
            source="company_careers",
            description=description,
            date_posted=date_posted,
        ))

    logger.info("Ashby %s (%s): %d job(s) matched", company_name, org, len(jobs))
    return jobs


def _scrape_generic(careers_url: str, company_name: str,
                    kw_patterns: list[re.Pattern],
                    excl_patterns: list[str]) -> list[Job]:
    """Best-effort HTML scraping for companies without a known ATS API."""
    try:
        resp = requests.get(
            careers_url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Generic scrape failed for %s: %s", company_name, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    from urllib.parse import urlparse
    parsed_base = urlparse(careers_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    jobs: list[Job] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        link_text = a.get_text(" ", strip=True)
        href = a["href"]

        if not link_text or len(link_text) < 5 or len(link_text) > 150:
            continue
        if _matches_exclude(link_text, excl_patterns):
            continue
        if not _matches_keywords(link_text, kw_patterns):
            continue

        # Resolve relative URLs
        if href.startswith("/"):
            href = base_origin + href
        elif not href.startswith("http"):
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        jobs.append(Job(
            title=link_text,
            company=company_name,
            location="",
            url=href,
            source="company_careers",
            description="",
        ))

    if jobs:
        logger.info("Generic %s: %d matching job link(s) found", company_name, len(jobs))
    else:
        logger.warning(
            "Generic %s: no matching jobs found — page may require JavaScript "
            "or a different ATS configuration.",
            company_name,
        )
    return jobs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scrape_company(company_config: dict, cfg: dict) -> list[Job]:
    """Scrape a single company using the ATS type in company_config['ats']."""
    name = company_config.get("name", "Unknown")
    ats = company_config.get("ats", "generic")
    token = company_config.get("ats_token", "")
    careers_url = company_config.get("careers_url", "")
    kw_patterns, excl_patterns = _build_patterns(cfg)

    if ats == "greenhouse":
        return _scrape_greenhouse(token or name.lower(), name, kw_patterns, excl_patterns)
    elif ats == "lever":
        return _scrape_lever(token or name.lower(), name, kw_patterns, excl_patterns)
    elif ats == "ashby":
        return _scrape_ashby(token or name.lower(), name, kw_patterns, excl_patterns)
    else:
        return _scrape_generic(careers_url, name, kw_patterns, excl_patterns)


def scrape_all_companies(cfg: dict) -> list[Job]:
    """
    Scrape all target_companies listed in cfg in parallel (8 threads).
    Returns a deduplicated list of Jobs (by URL).
    Per-company failures are logged as warnings and never propagate.
    """
    companies = cfg.get("target_companies", [])
    if not companies:
        return []

    results: dict[str, Job] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(scrape_company, c, cfg): c.get("name", "?")
            for c in companies
        }
        for future in as_completed(futures):
            company_name = futures[future]
            try:
                for job in future.result():
                    if job.url and job.url not in results:
                        results[job.url] = job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scrape failed for %s: %s", company_name, exc)

    logger.info(
        "Company career pages: %d unique job(s) found across %d companies",
        len(results), len(companies),
    )
    return list(results.values())
