#!/usr/bin/env python3
"""
scripts/add_company.py
Auto-detects the ATS (Applicant Tracking System) used by a company's career page
and adds it to config/profile.yaml.

Usage:
    python scripts/add_company.py --name "Stripe" --url "https://stripe.com/jobs"
    python scripts/add_company.py --name "Stripe" --url "https://stripe.com/jobs" --yes

Detected ATS types:
    greenhouse      — boards.greenhouse.io (many UK startups and scale-ups)
    lever           — jobs.lever.co / api.lever.co
    ashby           — jobs.ashbyhq.com
    workday         — *.wd{N}.myworkdayjobs.com
    smartrecruiters — careers.smartrecruiters.com
    generic         — fallback; HTML scraping via BeautifulSoup
"""

import argparse
import re
import sys
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

# Ensure we can import from the project root when run from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

CONFIG_PATH = Path("config/profile.yaml")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}
_TIMEOUT = 15

# Workday pattern: captures (tenant, instance_number, board)
_WORKDAY_RE = re.compile(
    r"([a-z0-9]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[^/\"' ]+/)?([a-zA-Z0-9_-]+)",
    re.I,
)

# Each entry: (compiled pattern to match in URL or HTML, ats_name)
# The first capture group extracts the company token.
_ATS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"boards\.greenhouse\.io/([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
    (re.compile(r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I), "smartrecruiters"),
]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _detect_ats(url: str) -> tuple[str, dict]:
    """
    Fetch the careers URL and detect which ATS is in use.

    Returns (ats_type, extra_fields) where extra_fields is a dict:
      - Most ATS:  {"ats_token": "..."}
      - Workday:   {"ats_token": tenant, "workday_board": board, "workday_instance": N}
      - Generic:   {}
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        final_url = resp.url
        html = resp.text
    except Exception as exc:
        print(f"  Warning: could not fetch {url}: {exc}", file=sys.stderr)
        return "generic", {}

    # 1. Check Workday first (compound config — handled separately)
    for source in (final_url, html):
        m = _WORKDAY_RE.search(source)
        if m:
            return "workday", {
                "ats_token": m.group(1).lower(),
                "workday_instance": int(m.group(2)),
                "workday_board": m.group(3),
            }

    # 2. Check whether the page redirected to a known ATS URL
    for pattern, ats_name in _ATS_PATTERNS:
        m = pattern.search(final_url)
        if m:
            return ats_name, {"ats_token": m.group(1)}

    # 3. Scan page HTML text (links, iframes, inline JS)
    for pattern, ats_name in _ATS_PATTERNS:
        m = pattern.search(html)
        if m:
            return ats_name, {"ats_token": m.group(1)}

    # 4. Check iframe src attributes
    soup = BeautifulSoup(html, "html.parser")
    for iframe in soup.find_all("iframe", src=True):
        for pattern, ats_name in _ATS_PATTERNS:
            m = pattern.search(iframe["src"])
            if m:
                return ats_name, {"ats_token": m.group(1)}

    return "generic", {}


# ---------------------------------------------------------------------------
# API test
# ---------------------------------------------------------------------------

def _test_ats(ats: str, extra_fields: dict, careers_url: str) -> int:
    """
    Test the detected ATS API. Returns the number of total job listings found
    (not filtered by keyword). Returns 0 on failure.
    """
    try:
        if ats == "greenhouse":
            token = extra_fields.get("ats_token", "")
            resp = requests.get(
                f"https://boards.greenhouse.io/v1/boards/{token}/jobs",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json().get("jobs", []))

        elif ats == "lever":
            token = extra_fields.get("ats_token", "")
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{token}?mode=json",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json())

        elif ats == "ashby":
            token = extra_fields.get("ats_token", "")
            resp = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
                headers={**_HEADERS, "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json().get("jobs", []))

        elif ats == "workday":
            tenant = extra_fields.get("ats_token", "")
            board = extra_fields.get("workday_board", "")
            instance = extra_fields.get("workday_instance", 1)
            api_url = (
                f"https://{tenant}.wd{instance}.myworkdayjobs.com"
                f"/wday/cxs/{tenant}/{board}/jobs"
            )
            resp = requests.post(
                api_url,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers={**_HEADERS, "Content-Type": "application/json",
                         "Referer": f"https://{tenant}.wd{instance}.myworkdayjobs.com/en-US/{board}/jobs"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json().get("total", 0)

        elif ats == "smartrecruiters":
            slug = extra_fields.get("ats_token", "")
            resp = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json().get("totalFound", 0)

        else:  # generic
            resp = requests.get(careers_url, headers=_HEADERS, timeout=_TIMEOUT)
            return 1 if resp.ok else 0

    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _company_exists(cfg: dict, name: str) -> bool:
    companies = cfg.get("target_companies", [])
    return any(c.get("name", "").lower() == name.lower() for c in companies)


def _append_to_yaml(name: str, careers_url: str, ats: str, extra_fields: dict) -> None:
    """
    Append a new company entry to the target_companies list in profile.yaml.
    Uses line-level string manipulation to preserve comments and formatting.
    """
    lines = CONFIG_PATH.read_text().splitlines()

    token = extra_fields.get("ats_token", "")

    # Build the entry block
    entry_lines = [
        f'  - name: "{name}"',
        f'    careers_url: "{careers_url}"',
        f'    ats: {ats}',
    ]
    if token:
        entry_lines.append(f'    ats_token: {token}')
    if ats == "workday":
        board = extra_fields.get("workday_board", "")
        instance = extra_fields.get("workday_instance", 1)
        if board:
            entry_lines.append(f'    workday_board: {board}')
        if instance != 1:
            entry_lines.append(f'    workday_instance: {instance}')

    # Find the last line of the target_companies block
    in_target = False
    last_entry_line = None
    for i, line in enumerate(lines):
        if line.strip().startswith("target_companies:"):
            in_target = True
            continue
        if in_target:
            # A non-indented, non-empty, non-comment line signals the end of the block
            if line and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
                break
            # Only count indented/list lines as data lines; skip comment and blank lines
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                last_entry_line = i

    if last_entry_line is None:
        print("ERROR: Could not find the target_companies list in profile.yaml.", file=sys.stderr)
        sys.exit(1)

    # Insert after the last entry line
    for offset, entry_line in enumerate(entry_lines):
        lines.insert(last_entry_line + 1 + offset, entry_line)

    CONFIG_PATH.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-detect ATS and add a company to config/profile.yaml.",
    )
    parser.add_argument("--name", required=True, help="Company display name (e.g. 'Stripe')")
    parser.add_argument("--url", required=True, help="Company careers page URL")
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt and write to profile.yaml immediately",
    )
    parser.add_argument(
        "--detect-only", "-d", action="store_true",
        help="Detect ATS and show results without adding to profile.yaml (bypasses already-exists check)",
    )
    parser.add_argument(
        "--ats", default=None,
        help="Override ATS type instead of auto-detecting (e.g. greenhouse, lever, ashby, smartrecruiters)",
    )
    parser.add_argument(
        "--token", default=None,
        help="ATS token/slug to use with --ats override (e.g. revolut, palantir)",
    )
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Run from the project root.", file=sys.stderr)
        sys.exit(1)

    cfg = _load_config()
    if not args.detect_only and _company_exists(cfg, args.name):
        print(f"'{args.name}' is already listed in profile.yaml.")
        sys.exit(0)

    print(f"\nDetecting ATS for: {args.name}")
    print(f"URL: {args.url}")
    print()

    if args.ats:
        ats = args.ats
        extra_fields = {"ats_token": args.token or ""} if args.token else {}
        print(f"  (ATS override — skipping auto-detection)")
    else:
        ats, extra_fields = _detect_ats(args.url)
    job_count = _test_ats(ats, extra_fields, args.url)

    # Display results
    print(f"  Detected ATS  : {ats}")
    if ats == "workday":
        print(f"  Tenant        : {extra_fields.get('ats_token')}")
        print(f"  Board         : {extra_fields.get('workday_board')}")
        print(f"  Instance      : wd{extra_fields.get('workday_instance', 1)}")
    elif extra_fields.get("ats_token"):
        print(f"  Token         : {extra_fields['ats_token']}")
    else:
        print(f"  Token         : (none — generic HTML scraping)")
    print(f"  Total listings: {job_count}")
    print()

    if job_count == 0 and ats not in ("generic", "js"):
        print(
            "  Warning: API test returned 0 jobs. "
            "The token may be incorrect or the company has no open roles right now.\n"
            "  You can manually edit the fields in profile.yaml after adding.\n"
        )

    if args.detect_only:
        return

    if not args.yes:
        try:
            answer = input(f"Add '{args.name}' ({ats}) to profile.yaml? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
        if answer != "y":
            print("Cancelled.")
            return

    _append_to_yaml(args.name, args.url, ats, extra_fields)
    print(f"\n  Added '{args.name}' ({ats}) to config/profile.yaml")


if __name__ == "__main__":
    main()
