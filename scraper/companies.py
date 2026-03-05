"""
scraper/companies.py
Scrapes specific company career pages for job listings.

Supports the following ATS platforms:
    greenhouse      — boards.greenhouse.io public API
    lever           — api.lever.co public API
    ashby           — api.ashbyhq.com public API
    workday         — Workday CXS API (boards.{tenant}.wd{N}.myworkdayjobs.com)
    smartrecruiters — api.smartrecruiters.com public API
    js              — Playwright headless-browser rendering (fallback: generic HTML)
    generic         — BeautifulSoup HTML scraping (best-effort)
    custom          — Per-company custom API (set custom_scraper: <name> in profile.yaml)

Config schema (one entry in profile.yaml → target_companies):
    - name: "Monzo"
      careers_url: "https://monzo.com/careers/"
      ats: greenhouse          # greenhouse | lever | ashby | workday | smartrecruiters | js | generic | custom
      ats_token: monzo         # ATS-specific identifier (omit for generic/js/custom)

    - name: "Millennium Management"
      careers_url: "https://career.mlp.com/careers"
      ats: custom
      custom_scraper: millennium   # maps to _scrape_millennium() below
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

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


def _extract_jobs_from_soup(
    soup: BeautifulSoup,
    careers_url: str,
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """
    Walk <a> tags in a parsed page and return keyword-matched job links.
    Shared by _scrape_generic and _scrape_js.
    """
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

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

    return jobs


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

    logger.debug("Greenhouse %s (%s): %d job(s) matched", company_name, token, len(jobs))
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

    logger.debug("Lever %s (%s): %d job(s) matched", company_name, slug, len(jobs))
    return jobs


def _scrape_ashby(org: str, company_name: str,
                  kw_patterns: list[re.Pattern],
                  excl_patterns: list[str]) -> list[Job]:
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
        description = item.get("descriptionPlain", "") or _strip_html(item.get("descriptionHtml", ""))
        if not _matches_keywords(title + " " + description, kw_patterns):
            continue

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

    logger.debug("Ashby %s (%s): %d job(s) matched", company_name, org, len(jobs))
    return jobs


def _scrape_workday(
    tenant: str,
    board: str,
    instance: int,
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """
    Scrape a Workday CXS job board.

    tenant   — Workday tenant slug (e.g. "blackrock")
    board    — Workday board/requisition group (e.g. "BlackRock")
    instance — The N in wd{N}.myworkdayjobs.com (e.g. 1)
    """
    if not tenant or not board:
        logger.warning("Workday config missing tenant or board for %s — skipping", company_name)
        return []

    base_url = (
        f"https://{tenant}.wd{instance}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{board}/jobs"
    )
    referer = f"https://{tenant}.wd{instance}.myworkdayjobs.com/en-US/{board}/jobs"
    headers = {**_HEADERS, "Content-Type": "application/json", "Referer": referer}

    def _post(offset: int) -> dict:
        return requests.post(
            base_url,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            headers=headers,
            timeout=_TIMEOUT,
        ).json()

    try:
        first = _post(0)
    except Exception as exc:
        logger.warning("Workday fetch failed for %s: %s", company_name, exc)
        return []

    total = first.get("total", 0)
    all_postings = list(first.get("jobPostings", []))

    while len(all_postings) < total:
        try:
            batch = _post(len(all_postings)).get("jobPostings", [])
        except Exception as exc:
            logger.warning(
                "Workday pagination error for %s at offset %d: %s",
                company_name, len(all_postings), exc,
            )
            break
        if not batch:
            break
        all_postings.extend(batch)

    jobs: list[Job] = []
    for posting in all_postings:
        title = posting.get("title", "")
        if _matches_exclude(title, excl_patterns):
            continue
        if not _matches_keywords(title, kw_patterns):
            continue

        ext_path = (posting.get("externalPath") or "").lstrip("/")
        job_url = (
            f"https://{tenant}.wd{instance}.myworkdayjobs.com"
            f"/en-US/{board}/job/{ext_path}"
        )
        location = posting.get("locationsText", "") or ""

        jobs.append(Job(
            title=title,
            company=company_name,
            location=location,
            url=job_url,
            source="company_careers",
            description="",
            date_posted=None,  # Workday returns human strings like "Posted 3 Days Ago"
        ))

    logger.debug(
        "Workday %s (%s/wd%d/%s): %d/%d job(s) matched",
        company_name, tenant, instance, board, len(jobs), total,
    )
    return jobs


def _scrape_smartrecruiters(
    slug: str,
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """Scrape the SmartRecruiters public postings API."""
    base_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"

    def _get(offset: int) -> dict:
        return requests.get(
            base_url,
            params={"limit": 100, "offset": offset},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        ).json()

    try:
        first = _get(0)
    except Exception as exc:
        logger.warning("SmartRecruiters fetch failed for %s (slug=%s): %s", company_name, slug, exc)
        return []

    total_found = first.get("totalFound", 0)
    all_postings = list(first.get("content", []))

    while len(all_postings) < total_found:
        try:
            batch = _get(len(all_postings)).get("content", [])
        except Exception as exc:
            logger.warning(
                "SmartRecruiters pagination error for %s at offset %d: %s",
                company_name, len(all_postings), exc,
            )
            break
        if not batch:
            break
        all_postings.extend(batch)

    jobs: list[Job] = []
    for posting in all_postings:
        title = posting.get("name", "")
        if _matches_exclude(title, excl_patterns):
            continue
        if not _matches_keywords(title, kw_patterns):
            continue

        loc_obj = posting.get("location") or {}
        city = loc_obj.get("city", "") or ""
        country = loc_obj.get("country", "") or ""
        location = ", ".join(filter(None, [city, country]))

        job_url = posting.get("ref", "")
        released = posting.get("releasedDate", "")
        date_posted = released[:10] if released else None

        jobs.append(Job(
            title=title,
            company=company_name,
            location=location,
            url=job_url,
            source="company_careers",
            description="",
            date_posted=date_posted,
        ))

    logger.debug(
        "SmartRecruiters %s (%s): %d/%d job(s) matched",
        company_name, slug, len(jobs), total_found,
    )
    return jobs


def _scrape_js(
    careers_url: str,
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """
    Render a JavaScript-heavy career page with Playwright and extract job links.
    Falls back to _scrape_generic if Playwright is not installed/available.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "JS scrape skipped for %s — playwright not installed "
            "(run: uv pip install playwright && playwright install chromium)",
            company_name,
        )
        return _scrape_generic(careers_url, company_name, kw_patterns, excl_patterns)

    html = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_UA)
            page.goto(careers_url, wait_until="networkidle", timeout=60_000)
            # Scroll once to trigger lazy-loaded content
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_500)
            html = page.content()
            browser.close()
    except Exception as exc:
        logger.warning(
            "JS scrape failed for %s — falling back to generic HTML. "
            "If browser binary is missing, run: playwright install chromium. Error: %s",
            company_name, exc,
        )
        return _scrape_generic(careers_url, company_name, kw_patterns, excl_patterns)

    soup = BeautifulSoup(html, "html.parser")
    jobs = _extract_jobs_from_soup(soup, careers_url, company_name, kw_patterns, excl_patterns)

    if jobs:
        logger.debug("JS %s: %d matching job link(s) found", company_name, len(jobs))
    else:
        logger.debug(
            "JS %s: 0 jobs found after rendering (page may require interaction beyond initial load)",
            company_name,
        )
    return jobs


def _scrape_millennium(
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """
    Millennium Management — Eightfold.ai career API.
    Discovered via scripts/find_jobs_api.py.
    """
    base_url = "https://career.mlp.com/api/apply/v2/jobs/755954850488/jobs"
    all_positions: list[dict] = []
    start = 0
    num = 100

    while True:
        try:
            resp = requests.get(
                base_url,
                params={"domain": "mlp.com", "start": start, "num": num},
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Millennium API fetch failed at offset %d: %s", start, exc)
            break

        positions = data.get("positions", [])
        if not positions:
            break
        all_positions.extend(positions)
        start += len(positions)
        if len(positions) < num:
            break

    jobs: list[Job] = []
    for p in all_positions:
        title = p.get("name", "")
        if _matches_exclude(title, excl_patterns) or not _matches_keywords(title, kw_patterns):
            continue

        location = p.get("location", "") or ""
        url = p.get("canonicalPositionUrl", "") or ""
        t_update = p.get("t_update")
        date_posted = _epoch_ms_to_date(int(t_update) * 1000) if t_update else None

        jobs.append(Job(
            title=title,
            company=company_name,
            location=location,
            url=url,
            source="company_careers",
            description="",
            date_posted=date_posted,
        ))

    logger.debug(
        "Custom Millennium: %d/%d job(s) matched", len(jobs), len(all_positions),
    )
    return jobs


def _scrape_dufrain(
    company_name: str,
    kw_patterns: list[re.Pattern],
    excl_patterns: list[str],
) -> list[Job]:
    """
    Dufrain — HiBob job board API.
    Discovered via scripts/find_jobs_api.py.
    """
    try:
        resp = requests.get(
            "https://dufrainconsulting.careers.hibob.com/api/job-ad",
            headers={
                **_HEADERS,
                "companyidentifier": "dufrainconsulting",
                "Accept": "application/json",
                "Referer": "https://dufrainconsulting.careers.hibob.com/jobs",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Dufrain HiBob API fetch failed: %s", exc)
        return []

    all_items = data.get("jobAdDetails", [])
    jobs: list[Job] = []
    for item in all_items:
        title = item.get("title", "")
        if _matches_exclude(title, excl_patterns) or not _matches_keywords(title, kw_patterns):
            continue

        job_id = item.get("id", "")
        url = f"https://dufrainconsulting.careers.hibob.com/jobs/{job_id}"
        location = item.get("site", "") or item.get("country", "") or ""
        published = item.get("publishedAt", "")
        date_posted = published[:10] if published else None
        description = _strip_html(item.get("description", "") or "")

        jobs.append(Job(
            title=title,
            company=company_name,
            location=location,
            url=url,
            source="company_careers",
            description=description,
            date_posted=date_posted,
        ))

    logger.debug("Custom Dufrain: %d/%d job(s) matched", len(jobs), len(all_items))
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
    jobs = _extract_jobs_from_soup(soup, careers_url, company_name, kw_patterns, excl_patterns)

    if jobs:
        logger.debug("Generic %s: %d matching job link(s) found", company_name, len(jobs))
    else:
        logger.debug(
            "Generic %s: 0 jobs found (page likely requires JavaScript — use ats: js)",
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
    elif ats == "workday":
        return _scrape_workday(
            token,
            company_config.get("workday_board", ""),
            int(company_config.get("workday_instance", 1)),
            name, kw_patterns, excl_patterns,
        )
    elif ats == "smartrecruiters":
        return _scrape_smartrecruiters(token or name.lower(), name, kw_patterns, excl_patterns)
    elif ats == "js":
        return _scrape_js(careers_url, name, kw_patterns, excl_patterns)
    elif ats == "custom":
        _CUSTOM_SCRAPERS = {
            "millennium": _scrape_millennium,
            "dufrain":    _scrape_dufrain,
        }
        scraper_name = company_config.get("custom_scraper", "").lower()
        fn = _CUSTOM_SCRAPERS.get(scraper_name)
        if fn:
            return fn(name, kw_patterns, excl_patterns)
        logger.warning(
            "No custom scraper registered for '%s' (custom_scraper: %s) — falling back to js",
            name, scraper_name,
        )
        return _scrape_js(careers_url, name, kw_patterns, excl_patterns)
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
