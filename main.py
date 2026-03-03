"""
main.py — JobSearcher entry point

Usage:
    python main.py --scrape [--limit N]   # Scrape, score, send digest email
    python main.py --daemon               # Start IMAP IDLE reply daemon
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

console = Console(width=160)


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
    """Scrape boards, dedup, score, print results, and send the digest email."""
    from scraper.boards import scrape_boards
    from db import Database
    from scorer import score_jobs
    from email_handler import send_digest

    today = date.today().strftime("%Y-%m-%d")
    console.rule(f"[bold]JobSearcher — Scrape Run {today}[/]")

    cfg = _load_config()

    with Database() as db:
        purged = db.reset_week()
        if purged:
            console.print(f"[dim]Cleared {purged} stale job(s) from seen list (older than 7 days)[/]")

        console.print("[dim]Scraping Indeed, LinkedIn…[/]")
        scraped = scrape_boards(cfg)

        # Filter to only jobs we haven't shown before
        new_jobs = [j for j in scraped if not db.is_seen(j.id)]
        already_seen = len(scraped) - len(new_jobs)

        if not new_jobs:
            msg = "[bold yellow]No new jobs found.[/]"
            if already_seen:
                msg += f" ({already_seen} already seen this week)"
            else:
                msg += " Boards may be rate-limiting or no listings match your filters."
            console.print(f"\n{msg}")
            return

        # Score and rank
        console.print("[dim]Scoring…[/]")
        scored = score_jobs(new_jobs, cfg)

        # Persist: mark seen + save digest (uses full scored list for index lookup)
        db.mark_seen_bulk(new_jobs)
        db.save_digest(today, [sj.job for sj in scored])

    # Send email digest
    try:
        console.print("[dim]Sending digest email…[/]")
        send_digest(cfg, scored)
        console.print("[bold green]Digest email sent.[/]")
    except KeyError as exc:
        console.print(f"[yellow]Email not sent:[/] missing credential {exc} in config/.env")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Email not sent:[/] {exc}")

    # Print to terminal (capped at --limit if provided)
    display = scored[:limit] if limit is not None else scored
    seen_note = f"  [dim]({already_seen} already seen this week, hidden)[/]" if already_seen else ""
    console.print(f"\n[bold green]Found {len(scored)} new job(s)[/]{seen_note}\n")

    table = Table(box=box.SIMPLE_HEAD, show_edge=False, highlight=True)
    table.add_column("#", style="dim", width=4, no_wrap=True)
    table.add_column("Title", min_width=28, max_width=40, no_wrap=False)
    table.add_column("Company", min_width=16, max_width=22, no_wrap=True)
    table.add_column("Location", min_width=10, max_width=18, no_wrap=True)
    table.add_column("Salary", min_width=14, max_width=20, no_wrap=True)
    table.add_column("Score", width=7, no_wrap=True)
    table.add_column("Source", min_width=10, no_wrap=True)

    for i, sj in enumerate(display, start=1):
        loc = sj.job.location or "—"
        score_disp = f"{sj.score:.1f}" if sj.score else "—"
        table.add_row(
            str(i),
            sj.job.title,
            sj.job.company,
            loc,
            _salary_display(sj.job),
            score_disp,
            sj.job.source.capitalize(),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Daemon command
# ---------------------------------------------------------------------------

def cmd_daemon() -> None:
    """Start the IMAP IDLE inbox watcher."""
    from db import Database
    from email_handler import start_daemon

    cfg = _load_config()
    console.print("[bold]JobSearcher Daemon[/] — watching inbox for digest replies…")
    console.print("[dim]Press Ctrl+C to stop.[/]\n")

    with Database() as db:
        start_daemon(cfg, db)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s  %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Silence noisy third-party loggers unless --verbose
    if not verbose:
        for noisy in ("JobSpy", "imapclient", "imapclient.imaplib", "httpx",
                      "httpcore", "anthropic", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


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
