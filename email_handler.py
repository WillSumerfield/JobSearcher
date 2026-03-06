"""
email_handler.py — Gmail SMTP digest sender + IMAP IDLE reply daemon.

Two separate Gmail accounts are used:
  Sender  (GMAIL_SENDER_ADDRESS / GMAIL_SENDER_APP_PASSWORD)
      A dedicated bot account.  Sends the digest and all document emails.
      The daemon logs into this account's IMAP inbox to watch for replies.

  Recipient (GMAIL_RECIPIENT_ADDRESS)
      The user's personal address.  Receives the digest and replies to it.

Flow:
  1. send_digest()  →  bot sends digest TO user
  2. User replies   →  reply lands in bot's inbox (To: is bot address)
  3. start_daemon() →  bot's IMAP watches for messages FROM user,
                       calls adaptor, sends .docx files back TO user
"""

import email as email_lib
import logging
import os
import re
import smtplib
import time
from datetime import date
from email.header import decode_header, make_header
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString
from imapclient import IMAPClient

from scorer import ScoredJob

logger = logging.getLogger(__name__)

_LINK_ICON_PATH = Path(__file__).parent / "assets" / "link_icon.png"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

DIGEST_SUBJECT_PREFIX = "Today's Job Matches"
# Gmail drops IDLE after ~5 min; renew a little before that
IDLE_TIMEOUT = 280


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def _ordinal_suffix(n: int) -> str:
    """Return the ordinal suffix for an integer (1→'st', 2→'nd', 3→'rd', else 'th')."""
    return "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _format_date_written(dt: date) -> str:
    """Format a date as e.g. 'Tuesday, 3rd March 2026'."""
    suffix = _ordinal_suffix(dt.day)
    return f"{dt.strftime('%A')}, {dt.day}{suffix} {dt.strftime('%B %Y')}"


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _sender_creds() -> tuple[str, str]:
    """Return (sender_address, app_password) for the bot account."""
    return os.environ["GMAIL_SENDER_ADDRESS"], os.environ["GMAIL_SENDER_APP_PASSWORD"]


def _recipient() -> str:
    """Return the user's personal address that receives the digest."""
    return os.environ["GMAIL_RECIPIENT_ADDRESS"]


# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------

_HTML_HEADER = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:28px 12px;background:#FEF0F5;font-family:'Trebuchet MS',Tahoma,Verdana,sans-serif;">
<div style="max-width:880px;margin:0 auto;background:#FFFFFF;border-radius:20px;overflow:hidden;box-shadow:0 4px 28px rgba(180,60,90,0.10);">

  <!-- Header band -->
  <div style="background:#FFE4ED;padding:28px 32px 20px;border-bottom:2px solid #F8C0D0;">
    <div style="font-family:Georgia,Cambria,'Times New Roman',serif;font-size:24px;font-weight:normal;color:#3D1020;letter-spacing:-0.2px;">
      &#10022;  Today's Job Matches
    </div>
    <div style="margin-top:5px;font-size:15px;color:#AA7888;">{date}</div>
  </div>

  {intro_block}

  <!-- Instructions -->
  <div style="padding:18px 32px 14px;background:#FFF5F8;border-bottom:1px solid #FFE4ED;">
    <p style="margin:0;font-size:13.5px;color:#5C2A3A;line-height:1.65;">
      Spot a role you like? Hit reply with the numbers &mdash; e.g.
      <span style="background:#FFE4ED;color:#A83050;padding:1px 7px;border-radius:5px;font-family:monospace;font-size:13px;">1, 3, 7</span>
      &mdash; and I&rsquo;ll get your tailored documents sorted!
    </p>
  </div>

  <!-- Table -->
  <div style="padding:8px 20px 20px;">
  <table style="border-collapse:collapse;width:100%;font-size:13.5px;margin-top:12px;">
    <thead>
      <tr style="background:#F8CAD8;color:#3D1020;text-align:left;">
        <th style="padding:10px 10px;font-weight:600;">#</th>
        <th style="padding:10px 10px;font-weight:600;">Title</th>
        <th style="padding:10px 10px;font-weight:600;">Company</th>
        <th style="padding:10px 10px;font-weight:600;">Location</th>
        <th style="padding:10px 10px;font-weight:600;">Salary</th>
        <th style="padding:10px 10px;font-weight:600;text-align:center;">Fit</th>
        <th style="padding:10px 10px;font-weight:600;">Notes</th>
        <th style="padding:10px 10px;font-weight:600;">Link</th>
      </tr>
    </thead>
    <tbody>
"""

_HTML_ROW = """\
    <tr style="background:{bg};border-bottom:1px solid #FFE4ED;">
      <td style="padding:9px 10px;font-weight:700;color:#C4607A;font-size:14px;">{idx}</td>
      <td style="padding:9px 10px;font-weight:600;color:#3D1020;">{title}</td>
      <td style="padding:9px 10px;color:#5C2A3A;">{company}</td>
      <td style="padding:9px 10px;color:#AA7888;font-size:13px;">{location}</td>
      <td style="padding:9px 10px;color:#AA7888;font-size:13px;">{salary}</td>
      <td style="padding:9px 10px;text-align:center;">{score_badge}</td>
      <td style="padding:9px 10px;font-style:italic;color:#AA7888;font-size:12.5px;">{reason}</td>
      <td style="padding:9px 10px;text-align:center;"><a href="{url}" style="text-decoration:none;" title="View listing">{link_icon}</a></td>
    </tr>
"""

_HTML_FOOTER = """\
    </tbody>
  </table>
  </div>

  <!-- Footer -->
  <div style="padding:16px 32px 24px;text-align:center;border-top:1px solid #FFE4ED;">
    <span style="color:#C8A0AC;font-size:12px;">
      Good luck Rach - you've got this &#10024;
      &nbsp;&bull;&nbsp; JobSearcher &bull; {count} new listing(s)
    </span>
  </div>

</div>
</body></html>
"""


def _score_badge(score: float) -> str:
    """Return a coloured pill <span> for the score, styled by tier."""
    if score >= 8:
        bg, fg = "#B8EED0", "#1B5E38"
    elif score >= 5:
        bg, fg = "#FFE8A8", "#6B4700"
    elif score > 0:
        bg, fg = "#FFD0D8", "#7A1E2E"
    else:
        bg, fg = "#EEEAEA", "#888888"
    label = f"{int(score)}" if score else "—"
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'padding:3px 10px;border-radius:99px;font-weight:700;font-size:12px;'
        f'white-space:nowrap;">{label}</span>'
    )


EXCLUDE_CATEGORIES = {"Sports", "Music", "Sponsors"}

# ---------------------------------------------------------------------------
# DataLemur daily code challenge
# ---------------------------------------------------------------------------

_DATALEMUR_QUESTIONS: list[dict] = [
    # Easy
    {"title": "Histogram of Tweets",            "slug": "sql-histogram-tweets",               "difficulty": "Easy",   "company": "Twitter"},
    {"title": "Data Science Skills",             "slug": "matching-skills",                    "difficulty": "Easy",   "company": "LinkedIn"},
    {"title": "Page With No Likes",              "slug": "sql-page-with-no-likes",             "difficulty": "Easy",   "company": "Facebook"},
    {"title": "Unfinished Parts",                "slug": "tesla-unfinished-parts",             "difficulty": "Easy",   "company": "Tesla"},
    {"title": "Laptop vs. Mobile Viewership",    "slug": "laptop-mobile-viewership",           "difficulty": "Easy",   "company": "NY Times"},
    {"title": "Coin Fairness Test",              "slug": "coin-fairness-test",                 "difficulty": "Easy",   "company": "Facebook"},
    {"title": "Precision/Recall Tradeoff",       "slug": "precision-recall-tradeoff",          "difficulty": "Easy",   "company": "BCG Gamma"},
    {"title": "Average Post Hiatus (Part 1)",    "slug": "sql-average-post-hiatus-1",          "difficulty": "Easy",   "company": "Facebook"},
    {"title": "Teams Power Users",               "slug": "teams-power-users",                  "difficulty": "Easy",   "company": "Microsoft"},
    {"title": "4 Rolls To 4",                    "slug": "4-rolls-to-4",                       "difficulty": "Easy",   "company": "Visa"},
    {"title": "Base 13 Conversion",              "slug": "python-base-13-conversion",          "difficulty": "Easy",   "company": "Capital One"},
    {"title": "Same Stripes",                    "slug": "python-same-stripes",                "difficulty": "Easy",   "company": "ServiceNow"},
    {"title": "Factorial Formula",               "slug": "python-factorial-formula",           "difficulty": "Easy",   "company": "Microsoft"},
    {"title": "Intersection of Two Lists",       "slug": "python-intersection-of-two-lists",   "difficulty": "Easy",   "company": "Amazon"},
    {"title": "Another One",                     "slug": "python-add-another-one",             "difficulty": "Easy",   "company": "Spotify"},
    {"title": "Weakest Strong Link",             "slug": "python-weakest-strong-link",         "difficulty": "Easy",   "company": "Intuit"},
    {"title": "Fizz Buzz Sum",                   "slug": "python-fizz-buzz-sum",               "difficulty": "Easy",   "company": "Google"},
    {"title": "Compound Interest",               "slug": "python-compound-interest",           "difficulty": "Easy",   "company": "Fintech"},
    {"title": "Triangular Sum",                  "slug": "python-triangular-sum-of-an-array",  "difficulty": "Easy",   "company": "TikTok"},
    {"title": "Duplicate Job Listings",          "slug": "duplicate-job-listings",             "difficulty": "Easy",   "company": "LinkedIn"},
    {"title": "Cities With Completed Trades",    "slug": "completed-trades",                   "difficulty": "Easy",   "company": "Robinhood"},
    {"title": "Contains Duplicate",              "slug": "python-contains-duplicate",          "difficulty": "Easy",   "company": "Apple"},
    {"title": "Roman to Integer",                "slug": "python-roman-to-integer",            "difficulty": "Easy",   "company": "Palantir"},
    {"title": "Counting Letters In Numbers",     "slug": "python-counting-letters-in-numbers", "difficulty": "Easy",   "company": "Tesla"},
    {"title": "Average Review Ratings",          "slug": "sql-avg-review-ratings",             "difficulty": "Easy",   "company": "Amazon"},
    {"title": "Well Paid Employees",             "slug": "sql-well-paid-employees",            "difficulty": "Easy",   "company": "FAANG"},
    {"title": "Final Account Balance",           "slug": "final-account-balance",              "difficulty": "Easy",   "company": "PayPal"},
    {"title": "Is Anagram?",                     "slug": "python-is-anagram",                  "difficulty": "Easy",   "company": "Workday"},
    {"title": "Pascals Triangle",                "slug": "python-pascals-triangle",            "difficulty": "Easy",   "company": "Uber"},
    {"title": "App Click-through Rate (CTR)",    "slug": "click-through-rate",                 "difficulty": "Easy",   "company": "Facebook"},
    {"title": "Second Day Confirmation",         "slug": "second-day-confirmation",            "difficulty": "Easy",   "company": "TikTok"},
    {"title": "Is Palindrome",                   "slug": "python-palindrome",                  "difficulty": "Easy",   "company": "Spotify"},
    {"title": "IBM db2 Product Analytics",       "slug": "sql-ibm-db2-product-analytics",      "difficulty": "Easy",   "company": "IBM"},
    {"title": "Cards Issued Difference",         "slug": "cards-issued-difference",            "difficulty": "Easy",   "company": "JPMorgan"},
    {"title": "Compressed Mean",                 "slug": "alibaba-compressed-mean",            "difficulty": "Easy",   "company": "Alibaba"},
    {"title": "Pharmacy Analytics (Part 1)",     "slug": "top-profitable-drugs",               "difficulty": "Easy",   "company": "CVS Health"},
    {"title": "Pharmacy Analytics (Part 2)",     "slug": "non-profitable-drugs",               "difficulty": "Easy",   "company": "CVS Health"},
    {"title": "Pharmacy Analytics (Part 3)",     "slug": "total-drugs-sales",                  "difficulty": "Easy",   "company": "CVS Health"},
    {"title": "Assumptions of Linear Regression","slug": "assumptions-linear-regression",      "difficulty": "Easy",   "company": "McKinsey"},
    {"title": "Patient Support Analysis (Part 1)","slug": "frequent-callers",                  "difficulty": "Easy",   "company": "UnitedHealth"},
    # Medium
    {"title": "Lazy Movie Raters",               "slug": "netflix-interview-lazy-raters",      "difficulty": "Medium", "company": "Netflix"},
    {"title": "User's Third Transaction",        "slug": "sql-third-transaction",              "difficulty": "Medium", "company": "Uber"},
    {"title": "Second Highest Salary",           "slug": "sql-second-highest-salary",          "difficulty": "Medium", "company": "FAANG"},
    {"title": "Sending vs. Opening Snaps",       "slug": "time-spent-snaps",                   "difficulty": "Medium", "company": "Snapchat"},
    {"title": "Tweets' Rolling Averages",        "slug": "rolling-average-tweets",             "difficulty": "Medium", "company": "Twitter"},
    {"title": "Consecutive Fives",               "slug": "consecutive-fives",                  "difficulty": "Medium", "company": "Akuna Capital"},
    {"title": "Highest-Grossing Items",          "slug": "sql-highest-grossing",               "difficulty": "Medium", "company": "Amazon"},
    {"title": "Top Three Salaries",              "slug": "sql-top-three-salaries",             "difficulty": "Medium", "company": "FAANG"},
    {"title": "Signup Activation Rate",          "slug": "signup-confirmation-rate",           "difficulty": "Medium", "company": "TikTok"},
    {"title": "Spotify Streaming History",       "slug": "spotify-streaming-history",          "difficulty": "Medium", "company": "Spotify"},
    {"title": "Longest Consecutive Sequence",    "slug": "python-longest-consecutive-sequence","difficulty": "Medium", "company": "FAANG"},
    {"title": "Looping Number",                  "slug": "python-looping-number",              "difficulty": "Medium", "company": "Fintech"},
    {"title": "Gift Card Satisfaction",          "slug": "python-gift-card-satisfaction",      "difficulty": "Medium", "company": "FAANG"},
    {"title": "Matrix Rotation",                 "slug": "python-martix-rotation",             "difficulty": "Medium", "company": "Salesforce"},
    {"title": "Clock-wise Matrix Rotation",      "slug": "python-clock-wise-matrix-rotation",  "difficulty": "Medium", "company": "Adobe"},
    {"title": "Min Amplitude",                   "slug": "python-min-amplitude",               "difficulty": "Medium", "company": "Google"},
    {"title": "Average Subarray",                "slug": "python-average-subarray",            "difficulty": "Medium", "company": "Walmart"},
    {"title": "k-Radius Average",                "slug": "python-k-radius-average",            "difficulty": "Medium", "company": "Databricks"},
    {"title": "Idle GPU Days",                   "slug": "python-idle-gpu-days",               "difficulty": "Medium", "company": "FAANG"},
    {"title": "Hill Climbing",                   "slug": "python-hill-climbing",               "difficulty": "Medium", "company": "Swiggy"},
    {"title": "Video Ads Insertion",             "slug": "python-video-ads-insertion",         "difficulty": "Medium", "company": "Facebook"},
    {"title": "Spiral Matrix",                   "slug": "python-spiral-matrix",               "difficulty": "Medium", "company": "Walmart"},
    {"title": "Data Conference Attendees",       "slug": "python-data-conference-attendees",   "difficulty": "Medium", "company": "FAANG"},
    {"title": "Supercloud Customer",             "slug": "supercloud-customer",                "difficulty": "Medium", "company": "Microsoft"},
    {"title": "Real Estate Modelling",           "slug": "real-estate-modelling",              "difficulty": "Medium", "company": "Blackstone"},
    {"title": "Most Popular Integers",           "slug": "python-most-popular-integers",       "difficulty": "Medium", "company": "Google"},
    {"title": "Factorial Trailing Zeroes",       "slug": "python-factorial-trailing-zeroes",   "difficulty": "Medium", "company": "Microsoft"},
    {"title": "Generate Fractions",              "slug": "python-generate-fractions",          "difficulty": "Medium", "company": "Blackstone"},
    {"title": "Max Product of Three Numbers",    "slug": "python-maximum-product-three-numbers","difficulty": "Medium", "company": "D.E. Shaw"},
    {"title": "Two Sum",                         "slug": "python-two-sum",                     "difficulty": "Medium", "company": "Amazon"},
    {"title": "Two Sum (Part 2)",                "slug": "python-two-sum-2",                   "difficulty": "Medium", "company": "Amazon"},
    {"title": "Two Sum (Part 3)",                "slug": "python-two-sum-3",                   "difficulty": "Medium", "company": "Amazon"},
    {"title": "Pearson Correlation Coefficient", "slug": "python-pearson-correlation-coefficient","difficulty": "Medium", "company": "AQR"},
    {"title": "Largest Contiguous Subarray Sum", "slug": "python-largest-contiguous-subarray-sum","difficulty": "Medium", "company": "Akuna Capital"},
    {"title": "Biased Coin?",                    "slug": "biased-coin",                        "difficulty": "Medium", "company": "D.E. Shaw"},
    {"title": "Odd and Even Measurements",       "slug": "odd-even-measurements",              "difficulty": "Medium", "company": "Google"},
    {"title": "Swapped Food Delivery",           "slug": "sql-swapped-food-delivery",          "difficulty": "Medium", "company": "Zomato"},
    {"title": "Coin Change",                     "slug": "python-coin-change",                 "difficulty": "Medium", "company": "Amazon"},
    {"title": "FAANG Stock Min-Max (Part 1)",    "slug": "sql-bloomberg-stock-min-max-1",      "difficulty": "Medium", "company": "Bloomberg"},
    {"title": "Best-Selling Product",            "slug": "best-selling-products",              "difficulty": "Medium", "company": "Amazon"},
    {"title": "User Shopping Sprees",            "slug": "amazon-shopping-spree",              "difficulty": "Medium", "company": "Amazon"},
    {"title": "Histogram of Users and Purchases","slug": "histogram-users-purchases",          "difficulty": "Medium", "company": "Walmart"},
    {"title": "Compressed Mode",                 "slug": "alibaba-compressed-mode",            "difficulty": "Medium", "company": "Alibaba"},
]

_DIFFICULTY_BADGE_STYLE: dict[str, tuple[str, str]] = {
    "Easy":   ("#B8EED0", "#1B5E38"),
    "Medium": ("#FFE8A8", "#6B4700"),
    "Hard":   ("#FFD0D8", "#7A1E2E"),
}


def _datalemur_challenge() -> dict:
    """Return a date-seeded DataLemur question (stable within a day)."""
    q = _DATALEMUR_QUESTIONS[date.today().toordinal() % len(_DATALEMUR_QUESTIONS)]
    return {
        "title": q["title"],
        "url": f"https://datalemur.com/questions/{q['slug']}",
        "difficulty": q["difficulty"],
        "company": q["company"],
    }


def _fetch_dles_game() -> dict:
    """
    Scrape https://dles.aukspot.com/, collect all non-Sports games, and return
    a deterministic entry for today (stable within a day, changes each day).

    Falls back to a generic dles.aukspot.com link on any error.
    """
    fallback = {"title": "Dles", "url": "https://dles.aukspot.com/", "description": ""}
    try:
        resp = requests.get(
            "https://dles.aukspot.com/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobSearcher/1.0)"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        games: list[dict] = []
        for card in soup.find_all("div", class_="card"):
            label_div = card.find("div", class_="label")
            if not label_div:
                continue
            # Category name is a direct text node inside label_div (SVG is a child tag, not text)
            direct_texts = [
                str(t).strip()
                for t in label_div.children
                if isinstance(t, NavigableString) and str(t).strip()
            ]
            category = " ".join(direct_texts)
            if category in EXCLUDE_CATEGORIES:
                continue
            ol = card.find("ol")
            if not ol:
                continue
            for a in ol.find_all("a", href=True):
                title = a.get_text(strip=True)
                href = a["href"]
                if title and href.startswith("http"):
                    games.append({"title": title, "url": href, "description": category})
        if not games:
            return fallback
        return games[date.today().toordinal() % len(games)]
    except Exception:  # noqa: BLE001
        logger.warning("dles.aukspot.com fetch failed — using homepage fallback.")
        return fallback


def _fetch_fun_block() -> str:
    """
    Build the daily fun content block: today's bunny from dailybunny.org
    and a date-seeded non-sports puzzle from listdle.com/games.json.

    Uses the Squarespace JSON API (?format=json) to get the image URL directly,
    since the site lazy-loads images via JavaScript and static HTML scraping
    finds nothing.

    Returns an HTML string ready to drop into {intro_block}, or "" on any
    failure so the digest still sends without it.
    """
    try:
        resp = requests.get(
            "https://dailybunny.org/?format=json",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobSearcher/1.0)"},
        )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])
        if not items:
            logger.warning("dailybunny.org JSON: no items found.")
            return ""

        post = items[0]
        img_url = post.get("assetUrl") or post.get("thumbnailUrl")

        if not img_url:
            logger.warning("dailybunny.org JSON: no image URL in first item.")
            return ""

        post_title = post.get("title", "")
        media_html = (
            f'<a href="https://dailybunny.org/" style="display:block;">'
            f'<img src="{_esc(img_url)}" alt="{_esc(post_title)}" '
            f'style="max-width:100%;border-radius:10px;display:block;" /></a>'
        )

        bunny_card = (
            '<td rowspan="2" style="width:56%;padding-right:10px;vertical-align:top;">'
            '<div style="background:#FFF0F5;border-radius:14px;padding:16px;height:100%;">'
            '<p style="margin:0 0 10px;font-family:Georgia,Cambria,serif;'
            'font-size:15px;color:#3D1020;font-weight:normal;">'
            "Today&#39;s Runny Babbit &#x1F407;"
            "</p>"
            + media_html
            + "</div></td>"
        )

        game = _fetch_dles_game()
        game_title = _esc(game["title"])
        game_url = _esc(game["url"])
        game_desc = _esc(game.get("description", ""))

        # Try to fetch the favicon; only include it if the request succeeds.
        favicon_url = "https://dles.aukspot.com/favicon.png"
        try:
            icon_resp = requests.head(favicon_url, timeout=5,
                                      headers={"User-Agent": "Mozilla/5.0"})
            icon_html = (
                f'<img src="{favicon_url}" alt="" width="30" height="30" '
                f'style="border-radius:4px;vertical-align:middle;margin-right:14px;" />'
            ) if icon_resp.ok else ""
        except Exception:  # noqa: BLE001
            icon_html = ""

        icon_style = "vertical-align:middle;" if icon_html else "display:block;text-align:center;"

        listdle_card = (
            '<td style="width:44%;padding-left:10px;vertical-align:top;">'
            '<div style="background:#FFF0F5;border-radius:14px;padding:16px;text-align:center;">'
            '<p style="margin:0 0 14px;font-family:Georgia,Cambria,serif;'
            'font-size:15px;color:#3D1020;font-weight:normal;">'
            "Today&#39;s Puzzle &#x1F9E9;"
            "</p>"
            f'<a href="{game_url}" '
            'style="display:inline-block;background:#FFE4ED;color:#8A2040;text-decoration:none;'
            'padding:12px 20px;border-radius:10px;font-weight:700;font-size:17px;'
            'line-height:1;">'
            + icon_html +
            f'<span style="{icon_style}">{game_title} &#8594;</span>'
            "</a>"
        )
        if game_desc:
            listdle_card += (
                f'<p style="margin:12px 0 0;font-size:12.5px;color:#AA7888;line-height:1.5;">'
                f"{game_desc}</p>"
            )
        listdle_card += (
            '<p style="margin:10px 0 0;font-size:12.5px;color:#AA7888;line-height:1.5;">'
            "Don&#39;t forget to show me what score you get, cutie :3"
            "</p>"
            "</div></td>"
        )

        # --- DataLemur challenge cell (second row, right column) ---
        challenge = _datalemur_challenge()
        diff = challenge["difficulty"]
        badge_bg, badge_fg = _DIFFICULTY_BADGE_STYLE.get(diff, ("#EEEEEE", "#444444"))
        diff_badge = (
            f'<span style="display:inline-block;background:{badge_bg};color:{badge_fg};'
            f'padding:2px 9px;border-radius:99px;font-weight:700;font-size:12px;">'
            f'{_esc(diff)}</span>'
        )
        challenge_cell = (
            '<td style="width:44%;padding-left:10px;vertical-align:top;">'
            '<div style="background:#EEF2FF;border-radius:14px;padding:16px;text-align:center;">'
            '<p style="margin:0 0 10px;font-family:Georgia,Cambria,serif;'
            'font-size:15px;color:#3D1020;font-weight:normal;">'
            "Today&#39;s Code Challenge &#x1F4BB;"
            "</p>"
            f'<a href="{_esc(challenge["url"])}" '
            'style="display:inline-block;background:#3B5BDB;color:#FFFFFF;text-decoration:none;'
            'padding:10px 16px;border-radius:10px;font-weight:700;font-size:15px;'
            'line-height:1.3;">'
            f'{_esc(challenge["title"])} &#8594;'
            "</a>"
            f'<p style="margin:10px 0 0;font-size:12.5px;color:#AA7888;">'
            f'{_esc(challenge["company"])} &nbsp;&bull;&nbsp; {diff_badge}</p>'
            "</div></td>"
        )

        return (
            '<div style="padding:20px 32px;background:#FFF5F8;border-bottom:1px solid #FFE4ED;">'
            '<table style="width:100%;border-collapse:collapse;">'
            "<tr>"
            + bunny_card
            + listdle_card
            + "</tr><tr>"
            + challenge_cell
            + "</tr>"
            "</table></div>"
        )

    except Exception:  # noqa: BLE001
        logger.warning("Fun content block unavailable — skipping.", exc_info=True)
        return ""


def _build_html(scored_jobs: list[ScoredJob], date_str: str, intro_block: str = "",
                has_icon: bool = False) -> str:
    rows = []
    for i, sj in enumerate(scored_jobs, start=1):
        bg = "#FFF5F8" if i % 2 == 0 else "#FFFFFF"
        rows.append(_HTML_ROW.format(
            bg=bg,
            idx=i,
            title=_esc(sj.job.title),
            company=_esc(sj.job.company),
            location=_esc(sj.job.location or "—"),
            salary=_esc(sj.job.salary_display()),
            score_badge=_score_badge(sj.score),
            reason=_esc(sj.reason) if sj.reason else "",
            url=sj.job.url,
            link_icon=(
                '<img src="cid:link_icon" width="20" height="20" alt="View" '
                'style="display:block;margin:0 auto;" />'
                if has_icon else "View &#8594;"
            ),
        ))
    return (
        _HTML_HEADER.format(date=date_str, intro_block=intro_block)
        + "".join(rows)
        + _HTML_FOOTER.format(count=len(scored_jobs))
    )


def _build_plain(scored_jobs: list[ScoredJob], date_str: str) -> str:
    lines = [f"Today's Job Matches — {date_str}", "=" * 40, "",
             "Reply with numbers to apply, e.g: 1, 3, 7", ""]
    for i, sj in enumerate(scored_jobs, start=1):
        score_disp = f"{int(sj.score)}" if sj.score else "—"
        lines.append(f"{i:>3}.  {sj.job.title} @ {sj.job.company}")
        lines.append(f"      {sj.job.location or '—'} | {sj.job.salary_display()} | Score: {score_disp}")
        if sj.reason:
            lines.append(f"      {sj.reason}")
        lines.append(f"      {sj.job.url}")
        lines.append("")
    return "\n".join(lines)


def _esc(text: str) -> str:
    """Minimal HTML escaping."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _smtp_send(sender: str, password: str, to: str, msg) -> None:
    """Open a TLS SMTP connection and send one message."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(sender, password)
        smtp.sendmail(sender, to, msg.as_bytes())


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------

def send_digest(cfg: dict, scored_jobs: list[ScoredJob], date_str: str | None = None) -> None:
    """
    Send the job digest FROM the bot account TO the user's personal address.

    Args:
        cfg:          Parsed profile.yaml.
        scored_jobs:  Ranked job list from scorer.py.
        date_str:     Date string for the subject line; defaults to today.
    """
    if date_str is None:
        date_str = _format_date_written(date.today())

    sender, password = _sender_creds()
    recipient = _recipient()
    send_top_n = cfg.get("digest_email", {}).get("send_top_n", 15)
    jobs_to_send = scored_jobs[:send_top_n]
    subject = f"{DIGEST_SUBJECT_PREFIX} — {date_str}"

    intro_block = _fetch_fun_block()

    # Load icon for CID inline embedding (None if file is missing).
    try:
        icon_data = _LINK_ICON_PATH.read_bytes()
    except FileNotFoundError:
        icon_data = None

    # multipart/related wraps alternative + inline image so email clients
    # resolve cid:link_icon references inside the HTML part.
    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(_build_plain(jobs_to_send, date_str), "plain", "utf-8"))
    alternative.attach(MIMEText(
        _build_html(jobs_to_send, date_str, intro_block=intro_block, has_icon=icon_data is not None),
        "html", "utf-8",
    ))
    related.attach(alternative)

    if icon_data is not None:
        icon_part = MIMEImage(icon_data, _subtype="png")
        icon_part["Content-ID"] = "<link_icon>"
        icon_part["Content-Disposition"] = "inline"
        related.attach(icon_part)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    # No Reply-To header: replies go to From (the bot), landing in its inbox for the daemon.
    msg.attach(related)

    logger.info("Sending digest: %d jobs → %s", len(jobs_to_send), recipient)
    _smtp_send(sender, password, recipient, msg)
    logger.info("Digest sent.")


def send_attachments(
    cfg: dict,
    file_paths: list[Path],
    job_title: str,
    company: str,
    in_reply_to: str = "",
    references: str = "",
    thread_subject: str = "",
) -> None:
    """
    Send tailored .docx files FROM the bot account TO the user's personal address.

    Args:
        cfg:            Parsed profile.yaml.
        file_paths:     List of Paths pointing to the .docx files.
        job_title:      For the email subject line (used when no thread_subject).
        company:        For the email subject line (used when no thread_subject).
        in_reply_to:    Message-ID of the message being replied to (for threading).
        references:     Space-separated chain of ancestor Message-IDs (for threading).
        thread_subject: Subject of the thread to continue (e.g. "Re: Today's Job Matches…").
    """
    sender, password = _sender_creds()
    recipient = _recipient()

    subject = thread_subject or f"JobSearcher — Documents for {job_title} @ {company}"
    body = (
        f"Here are your tailored documents for:\n\n"
        f"  Role:    {job_title}\n"
        f"  Company: {company}\n\n"
        f"Good luck!\n"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for path in file_paths:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=path.name)
        part["Content-Disposition"] = f'attachment; filename="{path.name}"'
        msg.attach(part)

    logger.info("Sending attachments for '%s @ %s': %s", job_title, company,
                [p.name for p in file_paths])
    _smtp_send(sender, password, recipient, msg)
    logger.info("Attachments sent.")


# ---------------------------------------------------------------------------
# Reply parsing helpers
# ---------------------------------------------------------------------------

def _parse_indices(body: str) -> list[int]:
    """
    Extract 1-based job indices from the user's reply body.

    Stops collecting at the first quoted line ("> ...") or at the email
    attribution header ("On Mon, 5 Mar 2026 ... wrote:"), including the
    common multi-line form where the header wraps across several lines.
    This prevents numbers embedded in the quoted digest from leaking into
    the result when a client doesn't terminate the header with "wrote:" on
    the same line.
    """
    clean_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        # First quoted line signals the start of the original message — stop.
        if stripped.startswith(">"):
            break
        # Single-line attribution: "On Wed, 5 Mar 2026 at 10:00, Name wrote:"
        if re.match(r"^On .+wrote:", stripped):
            break
        # Multi-line attribution opening: "On Mon," / "On Wednesday," etc.
        if re.match(r"^On [A-Z][a-z]{2,},?\s+\d", stripped):
            break
        clean_lines.append(stripped)

    clean = " ".join(clean_lines)
    numbers = re.findall(r"\b(\d+)\b", clean)
    seen: set[int] = set()
    result: list[int] = []
    for n in numbers:
        idx = int(n)
        if 1 <= idx <= 100 and idx not in seen:
            seen.add(idx)
            result.append(idx)
    return result


def _is_digest_reply(subject: str, sender_addr: str, expected_sender: str) -> bool:
    """
    Return True if this message looks like the user's reply to a digest email.

    Checks:
      - Subject contains "Re:" and the digest subject prefix
      - Message is FROM the user's personal address (not spam / other mail)
    """
    subj_lower = subject.lower()
    is_reply = "re:" in subj_lower
    is_digest = DIGEST_SUBJECT_PREFIX.lower() in subj_lower
    is_from_user = sender_addr.lower().strip("<> ") == expected_sender.lower()
    return is_reply and is_digest and is_from_user


def _extract_body(raw_message: bytes) -> str:
    """
    Extract the plain-text body from a raw MIME email.

    Prefers text/plain parts.  If none are found (HTML-only email clients),
    falls back to stripping HTML with BeautifulSoup so replies from mobile
    clients are still readable.
    """
    msg = email_lib.message_from_bytes(raw_message)
    html_fallback: str | None = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ct == "text/plain":
                return text
            if ct == "text/html" and html_fallback is None:
                html_fallback = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    if html_fallback:
        return BeautifulSoup(html_fallback, "html.parser").get_text("\n")
    return ""


# ---------------------------------------------------------------------------
# IMAP IDLE daemon
# ---------------------------------------------------------------------------

def _decode_mime_header(raw: bytes | None) -> str:
    """
    Decode a MIME-encoded email header (e.g. =?UTF-8?Q?Re:_Subject?=).

    Gmail IMAP ENVELOPE headers arrive as raw bytes that may contain MIME
    encoded-word sequences.  Decoding them properly turns underscores back
    into spaces so substring checks work correctly.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw.decode("utf-8", errors="replace"))))
    except Exception:  # noqa: BLE001
        return raw.decode("utf-8", errors="replace")


def _process_new_messages(client: IMAPClient, cfg: dict, db) -> None:
    """
    Inspect unseen messages in the bot's inbox.
    Skips anything not from the user; processes digest replies.
    """
    from adaptor import adapt_for_job

    recipient = _recipient()  # we expect replies FROM this address
    uids = client.search(["UNSEEN"])
    if not uids:
        return

    logger.info("Found %d unseen message(s) to inspect.", len(uids))
    fetch_data = client.fetch(uids, ["ENVELOPE", "RFC822"])

    for uid, data in fetch_data.items():
        envelope = data.get(b"ENVELOPE")
        raw = data.get(b"RFC822")
        if not envelope or not raw:
            continue

        subject = _decode_mime_header(envelope.subject)
        sender_list = envelope.from_ or []
        if not sender_list:
            continue
        sender_addr = (
            f"{(sender_list[0].mailbox or b'').decode()}"
            f"@{(sender_list[0].host or b'').decode()}"
        )

        if not _is_digest_reply(subject, sender_addr, recipient):
            logger.debug("Skipping non-digest message (from=%r, subject=%r)",
                         sender_addr, subject)
            continue

        # Extract thread headers so the attachment reply appears in the same thread.
        parsed_msg = email_lib.message_from_bytes(raw)
        reply_msg_id = parsed_msg.get("Message-ID", "").strip()
        reply_in_reply_to = parsed_msg.get("In-Reply-To", "").strip()
        # References chain: original digest id + this reply's id
        reply_references = " ".join(filter(None, [reply_in_reply_to, reply_msg_id]))

        body = _extract_body(raw)
        indices = _parse_indices(body)

        if not indices:
            logger.warning("Reply from user had no valid indices "
                           "(subject=%r, body snippet=%r)", subject, body[:100])
            _send_error_note(
                ["I received your reply but couldn't find any job numbers in it.\n"
                 "  Please reply with the numbers of the roles you want, e.g: 1, 3, 7"],
                in_reply_to=reply_msg_id,
                references=reply_references,
                thread_subject=subject,
            )
            client.set_flags([uid], [b"\\Seen"])
            continue

        logger.info("Digest reply: indices=%s from %s", indices, sender_addr)

        attachment_paths: list[Path] = []
        errors: list[str] = []
        last_job: dict | None = None

        for idx in indices:
            job_dict = db.get_digest_job(idx)
            if job_dict is None:
                errors.append(f"No job found at index {idx} in the most recent digest.")
                logger.warning("Index %d not found in digest.", idx)
                continue

            last_job = job_dict
            try:
                resume_path, cover_path = adapt_for_job(job_dict, cfg)
                attachment_paths.extend([resume_path, cover_path])
                logger.info("Adapted documents for index %d: %s @ %s",
                            idx, job_dict["title"], job_dict["company"])
            except NotImplementedError:
                errors.append(
                    f"Index {idx} ({job_dict['title']} @ {job_dict['company']}): "
                    "document tailoring is not yet implemented."
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Index {idx}: unexpected error — {exc}")
                logger.exception("adaptor failed for index %d", idx)

        if attachment_paths and last_job is not None:
            multi = len(indices) > 1
            send_attachments(
                cfg,
                attachment_paths,
                job_title="Multiple roles" if multi else last_job["title"],
                company="See attachments" if multi else last_job["company"],
                in_reply_to=reply_msg_id,
                references=reply_references,
                thread_subject=subject,
            )

        if errors:
            _send_error_note(
                errors,
                in_reply_to=reply_msg_id,
                references=reply_references,
                thread_subject=subject,
            )

        client.set_flags([uid], [b"\\Seen"])


def _send_error_note(
    errors: list[str],
    in_reply_to: str = "",
    references: str = "",
    thread_subject: str = "",
) -> None:
    """Send a plain-text note FROM the bot TO the user about processing issues."""
    sender, password = _sender_creds()
    recipient = _recipient()

    body = "JobSearcher encountered the following issues processing your reply:\n\n"
    body += "\n".join(f"  • {e}" for e in errors)
    body += "\n"

    subject = thread_subject or "JobSearcher — Processing Notes"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    _smtp_send(sender, password, recipient, msg)


def start_daemon(cfg: dict, db) -> None:
    """
    Start the IMAP IDLE daemon. Runs until KeyboardInterrupt or fatal error.

    Logs into the bot account's IMAP inbox, processes any pending replies
    immediately, then enters IDLE. Wakes on each inbox event to check for
    new messages from the user. Reconnects with exponential backoff on drops.
    """
    sender, password = _sender_creds()

    backoff = 5
    logger.info("Daemon starting. Watching bot inbox (%s) for replies from %s",
                sender, _recipient())

    while True:
        try:
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(sender, password)
                client.select_folder("INBOX", readonly=False)
                logger.info("IMAP connected. Checking for pending replies…")
                backoff = 5

                _process_new_messages(client, cfg, db)

                logger.info("Entering IDLE loop…")
                client.idle()
                idle_start = time.monotonic()

                while True:
                    elapsed = time.monotonic() - idle_start
                    remaining = max(1, IDLE_TIMEOUT - elapsed)
                    responses = client.idle_check(timeout=remaining)

                    if responses:
                        client.idle_done()
                        _process_new_messages(client, cfg, db)
                        client.idle()
                        idle_start = time.monotonic()
                    elif (time.monotonic() - idle_start) >= IDLE_TIMEOUT:
                        client.idle_done()
                        client.idle()
                        idle_start = time.monotonic()

        except KeyboardInterrupt:
            logger.info("Daemon stopped by user.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("IMAP error: %s. Reconnecting in %ds…", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
