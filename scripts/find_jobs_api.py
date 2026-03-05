#!/usr/bin/env python3
"""
scripts/find_jobs_api.py
Playwright network-interception diagnostic tool.

Loads a company's career page in a headless browser, intercepts all HTTP
responses, and identifies JSON endpoints that look like job-listing APIs.
Use this to discover the internal API URL before writing a custom scraper.

Usage:
    # Single company
    python scripts/find_jobs_api.py --name "Revolut" --url "https://www.revolut.com/careers/"

    # All JS/generic companies from config/profile.yaml
    python scripts/find_jobs_api.py --all

    # Dump the rendered HTML as well
    python scripts/find_jobs_api.py --name "Uber" --url "..." --dump-html uber.html

    # Show all JSON intercepts, not just job-like ones
    python scripts/find_jobs_api.py --name "Revolut" --url "..." --all-json
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

CONFIG_PATH = Path("config/profile.yaml")

_JOB_FIELD_RE = re.compile(r"title|name|position|role|job_title", re.IGNORECASE)
_WAIT_EXTRA_MS = 3_000


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

def _looks_like_job_item(item: dict) -> bool:
    """Return True if a dict looks like a single job listing."""
    if not isinstance(item, dict):
        return False
    return any(_JOB_FIELD_RE.search(k) for k in item)


def _find_job_array(body) -> tuple[list | None, str]:
    """
    Search a parsed JSON body for the most likely job array.
    Returns (array, path_description) or (None, "").
    """
    if isinstance(body, list) and body and _looks_like_job_item(body[0]):
        return body, "root list"

    if isinstance(body, dict):
        # First pass: look for keys named jobs/postings/results/items/data
        priority = re.compile(r"^(jobs|postings|results|items|data|content|opportunities|roles)$", re.I)
        for k, v in body.items():
            if priority.match(k) and isinstance(v, list) and v and _looks_like_job_item(v[0]):
                return v, f".{k}"
        # Second pass: any list of job-like dicts
        for k, v in body.items():
            if isinstance(v, list) and v and _looks_like_job_item(v[0]):
                return v, f".{k}"

    return None, ""


def _sample_fields(items: list, n: int = 1) -> dict:
    """Return the keys and truncated values from the first n items."""
    if not items:
        return {}
    sample = items[0]
    return {k: (str(v)[:80] if not isinstance(v, (dict, list)) else type(v).__name__)
            for k, v in sample.items()}


# ---------------------------------------------------------------------------
# Playwright interception
# ---------------------------------------------------------------------------

def _run_interception(
    url: str,
    company_name: str,
    dump_html: str | None = None,
    show_all_json: bool = False,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ERROR: playwright not installed. Run: uv pip install playwright && playwright install chromium")
        return

    intercepted: list[dict] = []

    def _on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            req_url = response.url
            if "json" not in ct and not req_url.endswith(".json"):
                return
            try:
                body = response.json()
            except Exception:
                return

            items, path = _find_job_array(body)
            intercepted.append({
                "url": req_url,
                "method": response.request.method,
                "status": response.status,
                "body": body,
                "job_array": items,
                "path": path,
            })
        except Exception:
            pass  # Don't crash the interception loop

    print(f"\n{'─' * 60}")
    print(f"  {company_name}")
    print(f"  {url}")
    print(f"{'─' * 60}")
    print("  Launching browser…", end="", flush=True)

    html = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page.on("response", _on_response)
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception:
                # networkidle timeout is common — still capture what we got
                pass
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(_WAIT_EXTRA_MS)
            # Scroll again in case content loaded after first scroll
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1_000)
            if dump_html:
                html = page.content()
            browser.close()
    except Exception as exc:
        print(f"\n  Browser error: {exc}")
        return

    print(f" done. {len(intercepted)} JSON response(s) captured.")

    # Split into job-like vs other
    job_hits = [r for r in intercepted if r["job_array"] is not None]
    other_json = [r for r in intercepted if r["job_array"] is None]

    if job_hits:
        print(f"\n  ✓ Job-like API responses ({len(job_hits)}):")
        for r in job_hits:
            count = len(r["job_array"])
            print(f"\n    [{r['method']:4s}] {r['url']}")
            print(f"           Status {r['status']} — {count} item(s) at {r['path']!r}")
            sample = _sample_fields(r["job_array"])
            print(f"           Fields: {', '.join(sample.keys())}")
            if count > 0:
                print(f"           Sample item:")
                for k, v in sample.items():
                    print(f"             {k}: {v!r}")
    else:
        print("\n  ✗ No job-like API responses found.")
        print("    (Page may require authentication, or jobs are embedded in HTML)")

    if show_all_json and other_json:
        print(f"\n  Other JSON responses ({len(other_json)}):")
        for r in other_json:
            body_type = type(r["body"]).__name__
            size = len(json.dumps(r["body"]))
            print(f"    [{r['method']:4s}] {r['url']}")
            print(f"           Status {r['status']} — {body_type} ({size} bytes)")

    if dump_html and html:
        Path(dump_html).write_text(html, encoding="utf-8")
        print(f"\n  HTML dumped → {dump_html}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_target_companies() -> list[dict]:
    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found. Run from the project root.", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    companies = cfg.get("target_companies", [])
    return [c for c in companies if c.get("ats") in ("js", "generic")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Intercept network calls on a career page to find the jobs API endpoint.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                       help="Run against all JS/generic companies in config/profile.yaml")
    group.add_argument("--url", help="Career page URL to probe")

    parser.add_argument("--name", help="Company name (required with --url)")
    parser.add_argument("--dump-html", metavar="FILE",
                        help="Save rendered HTML to this file")
    parser.add_argument("--all-json", action="store_true",
                        help="Also show non-job-like JSON responses")

    args = parser.parse_args()

    if args.url and not args.name:
        parser.error("--name is required when using --url")

    if args.all:
        companies = _load_target_companies()
        if not companies:
            print("No JS/generic companies found in config/profile.yaml.")
            return
        print(f"Probing {len(companies)} JS/generic company career pages…")
        for c in companies:
            _run_interception(
                url=c["careers_url"],
                company_name=c["name"],
                show_all_json=args.all_json,
            )
    else:
        _run_interception(
            url=args.url,
            company_name=args.name,
            dump_html=args.dump_html,
            show_all_json=args.all_json,
        )

    print()


if __name__ == "__main__":
    main()
