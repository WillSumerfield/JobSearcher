#!/usr/bin/env python3
"""
scripts/add_company.py
Auto-detects the ATS (Applicant Tracking System) used by a company's career page
and adds it to config/profile.yaml.

Usage:
    python scripts/add_company.py --name "Stripe" --url "https://stripe.com/jobs"
    python scripts/add_company.py --name "Stripe" --url "https://stripe.com/jobs" --yes

Detected ATS types:
    greenhouse  — boards.greenhouse.io (many UK startups and scale-ups)
    lever       — jobs.lever.co / api.lever.co
    ashby       — jobs.ashbyhq.com
    generic     — fallback; HTML scraping via BeautifulSoup
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

# Each entry: (compiled pattern to match in URL or HTML, ats_name)
# The first capture group extracts the company token.
_ATS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"boards\.greenhouse\.io/([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _detect_ats(url: str) -> tuple[str, str]:
    """
    Fetch the careers URL and detect which ATS is in use.
    Returns (ats_type, token). Token is empty string for 'generic'.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        final_url = resp.url
        html = resp.text
    except Exception as exc:
        print(f"  Warning: could not fetch {url}: {exc}", file=sys.stderr)
        return "generic", ""

    # 1. Check whether the page redirected to a known ATS URL
    for pattern, ats_name in _ATS_PATTERNS:
        m = pattern.search(final_url)
        if m:
            return ats_name, m.group(1)

    # 2. Scan page HTML text (links, iframes, inline JS)
    for pattern, ats_name in _ATS_PATTERNS:
        m = pattern.search(html)
        if m:
            return ats_name, m.group(1)

    # 3. Check iframe src attributes
    soup = BeautifulSoup(html, "html.parser")
    for iframe in soup.find_all("iframe", src=True):
        for pattern, ats_name in _ATS_PATTERNS:
            m = pattern.search(iframe["src"])
            if m:
                return ats_name, m.group(1)

    return "generic", ""


# ---------------------------------------------------------------------------
# API test
# ---------------------------------------------------------------------------

def _test_ats(ats: str, token: str, careers_url: str) -> int:
    """
    Test the detected ATS API. Returns the number of total job listings found
    (not filtered by keyword). Returns 0 on failure.
    """
    try:
        if ats == "greenhouse":
            resp = requests.get(
                f"https://boards.greenhouse.io/v1/boards/{token}/jobs",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json().get("jobs", []))

        elif ats == "lever":
            resp = requests.get(
                f"https://api.lever.co/v0/postings/{token}?mode=json",
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json())

        elif ats == "ashby":
            resp = requests.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
                headers={**_HEADERS, "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                return len(resp.json().get("jobs", []))

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


def _append_to_yaml(name: str, careers_url: str, ats: str, token: str) -> None:
    """
    Append a new company entry to the target_companies list in profile.yaml.
    Uses line-level string manipulation to preserve comments and formatting.
    """
    lines = CONFIG_PATH.read_text().splitlines()

    # Build the entry block
    entry_lines = [
        f'  - name: "{name}"',
        f'    careers_url: "{careers_url}"',
        f'    ats: {ats}',
    ]
    if token:
        entry_lines.append(f'    ats_token: {token}')

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
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Run from the project root.", file=sys.stderr)
        sys.exit(1)

    cfg = _load_config()
    if _company_exists(cfg, args.name):
        print(f"'{args.name}' is already listed in profile.yaml.")
        sys.exit(0)

    print(f"\nDetecting ATS for: {args.name}")
    print(f"URL: {args.url}")
    print()

    ats, token = _detect_ats(args.url)
    job_count = _test_ats(ats, token, args.url)

    # Display results table
    print(f"  Detected ATS  : {ats}")
    if token:
        print(f"  Token         : {token}")
    else:
        print(f"  Token         : (none — generic HTML scraping)")
    print(f"  Total listings: {job_count}")
    print()

    if job_count == 0 and ats != "generic":
        print(
            "  Warning: API test returned 0 jobs. "
            "The token may be incorrect or the company has no open roles right now.\n"
            "  You can manually edit ats_token in profile.yaml after adding.\n"
        )

    if not args.yes:
        try:
            answer = input(f"Add '{args.name}' ({ats}) to profile.yaml? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
        if answer != "y":
            print("Cancelled.")
            return

    _append_to_yaml(args.name, args.url, ats, token)
    print(f"\n  Added '{args.name}' ({ats}) to config/profile.yaml")


if __name__ == "__main__":
    main()
