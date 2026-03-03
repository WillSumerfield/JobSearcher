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
# Gmail drops IDLE after ~5 min; renew a little before that
IDLE_TIMEOUT = 280


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
        date_str = date.today().strftime("%Y-%m-%d")

    sender, password = _sender_creds()
    recipient = _recipient()
    send_top_n = cfg.get("digest_email", {}).get("send_top_n", 15)
    jobs_to_send = scored_jobs[:send_top_n]
    subject = f"{DIGEST_SUBJECT_PREFIX} — {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    # No Reply-To header: replies go to From (the bot), landing in its inbox for the daemon.
    msg.attach(MIMEText(_build_plain(jobs_to_send, date_str), "plain", "utf-8"))
    msg.attach(MIMEText(_build_html(jobs_to_send, date_str), "html", "utf-8"))

    logger.info("Sending digest: %d jobs → %s", len(jobs_to_send), recipient)
    _smtp_send(sender, password, recipient, msg)
    logger.info("Digest sent.")


def send_attachments(cfg: dict, file_paths: list[Path], job_title: str, company: str) -> None:
    """
    Send tailored .docx files FROM the bot account TO the user's personal address.

    Args:
        cfg:        Parsed profile.yaml.
        file_paths: List of Paths pointing to the .docx files.
        job_title:  For the email subject line.
        company:    For the email subject line.
    """
    sender, password = _sender_creds()
    recipient = _recipient()

    subject = f"JobSearcher — Documents for {job_title} @ {company}"
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

    Strips quoted lines ("> ...") and "On ... wrote:" markers before
    searching, so "1, 3, 7" or "1 3 7" or "1\\n3\\n7" all work.
    """
    clean_lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
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

        subject = (envelope.subject or b"").decode("utf-8", errors="replace")
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

        body = _extract_body(raw)
        indices = _parse_indices(body)

        if not indices:
            logger.warning("Reply from user had no valid indices "
                           "(subject=%r, body snippet=%r)", subject, body[:100])
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
            )

        if errors:
            _send_error_note(errors)

        client.set_flags([uid], [b"\\Seen"])


def _send_error_note(errors: list[str]) -> None:
    """Send a plain-text note FROM the bot TO the user about processing issues."""
    sender, password = _sender_creds()
    recipient = _recipient()

    body = "JobSearcher encountered the following issues processing your reply:\n\n"
    body += "\n".join(f"  • {e}" for e in errors)
    body += "\n"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = "JobSearcher — Processing Notes"
    msg["From"] = sender
    msg["To"] = recipient
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
