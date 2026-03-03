"""
main.py — JobSearcher entry point

Usage:
    python main.py --scrape [--limit N]   # Run scraper, print results
    python main.py --daemon               # Start IMAP IDLE daemon (not yet implemented)
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

# ---------------------------------------------------------------------------
# Config / env loading
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("config/profile.yaml")
ENV_PATH = Path("config/.env")

console = Console()


def _check_config() -> bool:
    """Verify config files exist; print helpful setup message if not."""
    ok = True
    if not CONFIG_PATH.exists():
        console.print(
            f"[bold red]Missing config:[/] {CONFIG_PATH}\n"
            "Copy the template and fill in your details before running.",
            highlight=False,
        )
        ok = False
    if not ENV_PATH.exists():
        console.print(
            f"[bold red]Missing config:[/] {ENV_PATH}\n"
            "Create this file with your ANTHROPIC_API_KEY and Gmail credentials.",
            highlight=False,
        )
        ok = False
    return ok


def _load_config() -> dict:
    load_dotenv(ENV_PATH)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Scrape command
# ---------------------------------------------------------------------------

def _salary_display(job) -> str:
    return job.salary_display()


def cmd_scrape(limit: int | None) -> None:
    """Run the board scraper and pretty-print results."""
    from scraper.boards import scrape_boards

    today = date.today().strftime("%Y-%m-%d")
    console.rule(f"[bold]JobSearcher — Scrape Run {today}[/]")

    cfg = _load_config()

    console.print("[dim]Scraping Indeed, LinkedIn, Glassdoor…[/]")
    jobs = scrape_boards(cfg)

    if not jobs:
        console.print("\n[bold yellow]No jobs found.[/] Boards may be rate-limiting or no new listings match your filters.")
        return

    if limit is not None:
        jobs = jobs[:limit]

    console.print(f"\n[bold green]Found {len(jobs)} job(s)[/] (after dedup + filters)\n")

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, highlight=True)
    table.add_column("#", style="dim", width=4, no_wrap=True)
    table.add_column("Title", min_width=28, max_width=40, no_wrap=False)
    table.add_column("Company", min_width=16, max_width=22, no_wrap=True)
    table.add_column("Location", min_width=10, max_width=18, no_wrap=True)
    table.add_column("Salary", min_width=14, max_width=20, no_wrap=True)
    table.add_column("Source", min_width=10, no_wrap=True)
    table.add_column("URL", min_width=10, no_wrap=True, style="blue")

    for i, job in enumerate(jobs, start=1):
        loc = job.location or "—"
        table.add_row(
            str(i),
            job.title,
            job.company,
            loc,
            _salary_display(job),
            job.source.capitalize(),
            job.url,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Daemon command (stub)
# ---------------------------------------------------------------------------

def cmd_daemon() -> None:
    console.print(
        "[bold yellow]--daemon mode is not yet implemented.[/]\n"
        "This will start the IMAP IDLE inbox watcher in a later step."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s  %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jobsearcher",
        description="Automated job search assistant.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scrape", action="store_true", help="Run the board scraper")
    group.add_argument("--daemon", action="store_true", help="Start the IMAP IDLE daemon")
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Cap results at N (useful for quick testing)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show debug logs (scraper progress, filter decisions)",
    )

    args = parser.parse_args()
    _configure_logging(args.verbose)

    if not _check_config():
        sys.exit(1)

    if args.scrape:
        cmd_scrape(limit=args.limit)
    elif args.daemon:
        cmd_daemon()


if __name__ == "__main__":
    main()
