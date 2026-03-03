"""
email_handler.py — Gmail SMTP digest sender + IMAP IDLE reply daemon.

Sending:
    send_digest(cfg, scored_jobs, date_str)
        Formats a clean HTML email with the ranked job list and sends it
        from the user's Gmail to themselves.

Receiving (daemon):
    start_daemon(cfg, db)
        Opens an IMAP IDLE connection to Gmail, watches for replies to the
        digest email, parses the user's chosen indices, calls adaptor.py
        for each, and emails the resulting .docx files back as attachments.
"""

import email as email_lib
import imaplib
import logging
import os
import re
import smtplib
import time
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from imapclient import IMAPClient

from scorer import ScoredJob

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

DIGEST_SUBJECT_PREFIX = "JobSearcher Digest"
# Max time (seconds) to sit in IDLE before re-issuing to keep the connection alive
IDLE_TIMEOUT = 280  # Gmail drops IDLE after ~5 min; renew at ~4:40


# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------

_HTML_HEADER = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;margin:auto;padding:16px">
<h2 style="border-bottom:2px solid #4285f4;padding-bottom:8px">
  JobSearcher Digest &mdash; {date}
</h2>
<p style="color:#555">
  Reply to this email with the numbers of any roles you want to apply for,
  separated by commas or spaces &mdash; e.g. <code>1, 3, 7</code>.<br>
  You&rsquo;ll receive tailored resume and cover letter files as attachments.
</p>
<table style="border-collapse:collapse;width:100%;font-size:14px">
  <thead>
    <tr style="background:#4285f4;color:#fff;text-align:left">
      <th style="padding:8px 6px">#</th>
      <th style="padding:8px 6px">Title</th>
      <th style="padding:8px 6px">Company</th>
      <th style="padding:8px 6px">Location</th>
      <th style="padding:8px 6px">Salary</th>
      <th style="padding:8px 6px">Score</th>
      <th style="padding:8px 6px">Notes</th>
      <th style="padding:8px 6px">Link</th>
    </tr>
  </thead>
  <tbody>
"""

_HTML_ROW = """\
    <tr style="background:{bg}">
      <td style="padding:7px 6px;font-weight:bold;color:#4285f4">{idx}</td>
      <td style="padding:7px 6px;font-weight:bold">{title}</td>
      <td style="padding:7px 6px">{company}</td>
      <td style="padding:7px 6px;color:#555">{location}</td>
      <td style="padding:7px 6px;color:#555">{salary}</td>
      <td style="padding:7px 6px;text-align:center">{score}</td>
      <td style="padding:7px 6px;font-style:italic;color:#555;font-size:13px">{reason}</td>
      <td style="padding:7px 6px"><a href="{url}" style="color:#4285f4">View</a></td>
    </tr>
"""

_HTML_FOOTER = """\
  </tbody>
</table>
<p style="color:#aaa;font-size:12px;margin-top:24px">
  Sent by JobSearcher &bull; {count} new listing(s) today
</p>
</body></html>
"""

_PLAIN_HEADER = "JobSearcher Digest — {date}\n{'='*40}\n\nReply with numbers to apply, e.g: 1, 3, 7\n\n"
_PLAIN_ROW = "{idx:>3}.  {title} @ {company}\n     {location} | {salary} | Score: {score}\n     {url}\n\n"


def _build_html(scored_jobs: list[ScoredJob], date_str: str) -> str:
    rows = []
    for i, sj in enumerate(scored_jobs, start=1):
        bg = "#f9f9f9" if i % 2 == 0 else "#fff"
        score_disp = f"{sj.score:.1f}" if sj.score else "—"
        rows.append(_HTML_ROW.format(
            bg=bg,
            idx=i,
            title=_esc(sj.job.title),
            company=_esc(sj.job.company),
            location=_esc(sj.job.location or "—"),
            salary=_esc(sj.job.salary_display()),
            score=score_disp,
            reason=_esc(sj.reason) if sj.reason else "",
            url=sj.job.url,
        ))
    return (
        _HTML_HEADER.format(date=date_str)
        + "".join(rows)
        + _HTML_FOOTER.format(count=len(scored_jobs))
    )


def _build_plain(scored_jobs: list[ScoredJob], date_str: str) -> str:
    lines = [f"JobSearcher Digest — {date_str}", "=" * 40, "",
             "Reply with numbers to apply, e.g: 1, 3, 7", ""]
    for i, sj in enumerate(scored_jobs, start=1):
        score_disp = f"{sj.score:.1f}" if sj.score else "—"
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


# ---------------------------------------------------------------------------
# SMTP sending
# ---------------------------------------------------------------------------

def send_digest(cfg: dict, scored_jobs: list[ScoredJob], date_str: str | None = None) -> None:
    """
    Send the job digest email from the user's Gmail to themselves.

    Args:
        cfg:          Parsed profile.yaml.
        scored_jobs:  Ranked job list from scorer.py.
        date_str:     Date string for the subject line; defaults to today.
    """
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")

    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    send_top_n = cfg.get("digest_email", {}).get("send_top_n", 15)

    jobs_to_send = scored_jobs[:send_top_n]
    subject = f"{DIGEST_SUBJECT_PREFIX} — {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    msg.attach(MIMEText(_build_plain(jobs_to_send, date_str), "plain", "utf-8"))
    msg.attach(MIMEText(_build_html(jobs_to_send, date_str), "html", "utf-8"))

    logger.info("Sending digest: %d jobs, subject='%s'", len(jobs_to_send), subject)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_address, gmail_password)
        smtp.sendmail(gmail_address, gmail_address, msg.as_bytes())
    logger.info("Digest sent.")


def send_attachments(cfg: dict, file_paths: list[Path], job_title: str, company: str) -> None:
    """
    Send tailored resume and cover letter .docx files back to the user.

    Args:
        cfg:        Parsed profile.yaml.
        file_paths: List of Path objects pointing to the .docx files.
        job_title:  For the email subject line.
        company:    For the email subject line.
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    subject = f"JobSearcher — Documents for {job_title} @ {company}"
    body = (
        f"Here are your tailored documents for:\n\n"
        f"  Role:    {job_title}\n"
        f"  Company: {company}\n\n"
        f"Good luck!\n"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for path in file_paths:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=path.name)
        part["Content-Disposition"] = f'attachment; filename="{path.name}"'
        msg.attach(part)

    logger.info("Sending attachments for '%s @ %s': %s", job_title, company,
                [p.name for p in file_paths])
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_address, gmail_password)
        smtp.sendmail(gmail_address, gmail_address, msg.as_bytes())
    logger.info("Attachments sent.")


# ---------------------------------------------------------------------------
# Reply parsing helpers
# ---------------------------------------------------------------------------

def _parse_indices(body: str) -> list[int]:
    """
    Extract 1-based job indices from the user's reply body.

    Strips quoted lines ("> ...") and the original email footer before
    searching for numbers, so "1, 3, 7" or "1 3 7" or "1\n3\n7" all work.
    """
    # Strip quoted reply lines
    clean_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        # Stop at "On ... wrote:" markers (Gmail/Outlook quoting)
        if re.match(r"^On .+ wrote:$", stripped):
            break
        clean_lines.append(stripped)

    clean = " ".join(clean_lines)
    numbers = re.findall(r"\b(\d+)\b", clean)
    # Deduplicate while preserving order; clamp to reasonable range
    seen: set[int] = set()
    result: list[int] = []
    for n in numbers:
        idx = int(n)
        if 1 <= idx <= 100 and idx not in seen:
            seen.add(idx)
            result.append(idx)
    return result


def _is_digest_reply(subject: str, sender: str, our_address: str) -> bool:
    """Return True if this message looks like a user reply to a digest email."""
    subj_lower = subject.lower()
    is_reply = "re:" in subj_lower or "re " in subj_lower
    is_digest = DIGEST_SUBJECT_PREFIX.lower() in subj_lower
    is_from_us = sender.lower().strip("<> ") == our_address.lower()
    return is_reply and is_digest and is_from_us


def _extract_body(raw_message: bytes) -> str:
    """Extract the plain-text body from a raw MIME email."""
    msg = email_lib.message_from_bytes(raw_message)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


# ---------------------------------------------------------------------------
# IMAP IDLE daemon
# ---------------------------------------------------------------------------

def _process_new_messages(client: IMAPClient, cfg: dict, db) -> None:
    """
    Fetch any unseen replies to digest emails, parse indices, and dispatch.
    Called immediately on connect and after each IDLE notification.
    """
    from adaptor import adapt_for_job

    gmail_address = os.environ["GMAIL_ADDRESS"]
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

        subject = (envelope.subject or b"").decode("utf-8", errors="replace")
        # sender is a list of Address objects
        sender_list = envelope.from_ or []
        if not sender_list:
            continue
        sender_addr = f"{(sender_list[0].mailbox or b'').decode()}@{(sender_list[0].host or b'').decode()}"

        if not _is_digest_reply(subject, sender_addr, gmail_address):
            logger.debug("Skipping non-digest message (subject=%r)", subject)
            continue

        body = _extract_body(raw)
        indices = _parse_indices(body)

        if not indices:
            logger.warning("Reply contained no valid indices (subject=%r, body snippet=%r)",
                           subject, body[:100])
            client.set_flags([uid], [b"\\Seen"])
            continue

        logger.info("Reply detected: indices=%s", indices)

        attachment_paths: list[Path] = []
        errors: list[str] = []

        for idx in indices:
            job_dict = db.get_digest_job(idx)
            if job_dict is None:
                errors.append(f"No job found at index {idx} in the most recent digest.")
                logger.warning("Index %d not found in digest.", idx)
                continue

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

        # Send back whatever we have (files + any error notes)
        if attachment_paths:
            send_attachments(cfg, attachment_paths,
                             job_title="Multiple roles" if len(indices) > 1 else job_dict["title"],
                             company="See attachments" if len(indices) > 1 else job_dict["company"])

        if errors:
            _send_error_reply(cfg, errors)

        # Mark the reply as seen so we don't process it again
        client.set_flags([uid], [b"\\Seen"])


def _send_error_reply(cfg: dict, errors: list[str]) -> None:
    """Send a plain-text email reporting processing errors back to the user."""
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    body = "JobSearcher encountered the following issues processing your reply:\n\n"
    body += "\n".join(f"  • {e}" for e in errors)
    body += "\n"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "JobSearcher — Processing Notes"
    msg["From"] = gmail_address
    msg["To"] = gmail_address

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_address, gmail_password)
        smtp.sendmail(gmail_address, gmail_address, msg.as_bytes())


def start_daemon(cfg: dict, db) -> None:
    """
    Start the IMAP IDLE daemon. Runs until KeyboardInterrupt or fatal error.

    Connects to Gmail IMAP, processes any already-pending replies immediately,
    then enters IDLE mode. On each inbox notification the IDLE loop wakes,
    processes new messages, and re-enters IDLE.

    Reconnects automatically on transient connection errors.
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    backoff = 5  # seconds; doubles on each reconnect, capped at 5 minutes

    logger.info("Daemon starting. Watching inbox as %s", gmail_address)

    while True:
        try:
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(gmail_address, gmail_password)
                client.select_folder("INBOX", readonly=False)
                logger.info("IMAP connected. Checking for pending replies…")
                backoff = 5  # reset on successful connect

                _process_new_messages(client, cfg, db)

                logger.info("Entering IDLE loop. Waiting for inbox activity…")
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
                        # Re-issue IDLE to keep the connection alive
                        client.idle_done()
                        client.idle()
                        idle_start = time.monotonic()

        except KeyboardInterrupt:
            logger.info("Daemon stopped by user.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("IMAP connection error: %s. Reconnecting in %ds…", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
