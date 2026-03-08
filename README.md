# JobSearcher

An automated job search assistant that scrapes job boards and company career pages daily, ranks results using Claude AI, emails you a curated digest, and tailors your resume and cover letter on request.

---

## How It Works

1. **Scrape** — Pulls jobs from Indeed, LinkedIn, Welcome to the Jungle, and target company career pages
2. **Filter** — Drops excluded titles and jobs below your salary floor
3. **Score** — Keyword match (instant) → Claude ranking (1–10 with reason)
4. **Email** — Sends you an HTML digest of the top results
5. **Tailor** — Reply with job numbers; the daemon rewrites your resume and cover letter for each one and emails them back

---

## Setup

### Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) package manager
- Two Gmail accounts (one for the bot, one for you)
- An [Anthropic API key](https://console.anthropic.com) with credits
- [Claude Code CLI](https://docs.anthropic.com/claude-code) installed and authenticated (`claude` must be on your `PATH`)

### Install Dependencies

```bash
cd ~/JobSearcher
uv pip install -r requirements.txt
```

### Configure Credentials — `config/.env`

Create `config/.env` (not committed to git):

```env
ANTHROPIC_API_KEY=sk-ant-...

GMAIL_SENDER_ADDRESS=your.bot.account@gmail.com
GMAIL_SENDER_APP_PASSWORD=xxxx xxxx xxxx xxxx

GMAIL_RECIPIENT_ADDRESS=you@gmail.com
```

**Getting a Gmail App Password:**

1. Enable 2-Factor Authentication on the **bot** account
2. Go to **Google Account → Security → App Passwords**
3. Create a new app password (any name); copy the 16-character string (spaces included)
4. Paste it as `GMAIL_SENDER_APP_PASSWORD`

> The bot account is the one that sends emails and listens for replies. The recipient address is your personal inbox where you receive digests and tailored documents.

### Prepare Document Templates

Place your resume and cover letter in the `templates/` directory:

```bash
cp ~/my-resume.docx templates/resume.docx
cp ~/my-cover-letter.docx templates/cover_letter.docx
```

These are the base documents Claude will tailor for each job you request. Files must be `.docx` format (not `.doc`). The repo includes placeholder files — replace them with your real documents before running.

---

## Configuration — `config/profile.yaml`

All search and application behaviour is controlled from a single YAML file.

### Applicant Profile

```yaml
profile:
  name: "Your Name"
  email: "you@gmail.com"          # Must match GMAIL_RECIPIENT_ADDRESS
```

Your name and email are injected into Claude's prompts when tailoring documents, and used to match inbound replies to the daemon.

### Search Settings

```yaml
search:
  keywords:                        # Job titles to search across all boards
    - "Senior Data Scientist"
    - "Data Scientist"
    - "Machine Learning Engineer"

  location: "New York, NY"         # Primary location for non-remote results
  remote_ok: true                  # Include remote roles
  hybrid_ok: true                  # Include hybrid roles

  salary_min_gbp: 60000            # Drop jobs where max salary is known and below this
  contract_ok: true                # Include contract / day-rate roles
  results_per_board: 30            # Max results per keyword per board per location
```

> **Note:** Each keyword is searched twice — once in `location`, once as `remote_only`. Setting `location: "Remote"` won't work as jobspy geocodes it incorrectly.

> **Currency:** The `salary_min_gbp` field name is a legacy artefact — the value is compared against whatever currency the job board reports, so treat it as a numeric floor in your local currency.

### Skills

```yaml
skills:
  - Python
  - SQL
  - Machine Learning
  - Data Analysis
  - Cloud Platforms (AWS, GCP, Azure)
```

List the technical skills most relevant to your target roles. These serve two purposes:

1. **Stage 1 keyword scoring** — alphanumeric tokens (≥3 chars) are extracted and matched against job descriptions to produce a fast pre-score before Claude is called
2. **Claude's applicant profile** — Claude uses your skills when ranking jobs and writing tailored documents

The more specific and accurate your skills list, the better the scoring and tailoring quality.

### Education & Experience

```yaml
education:
  - "BSc Computer Science"
  - "MSc Machine Learning"

experience_summary: |
  3 years as a Data Scientist at Example Corp.
  Specialising in NLP, model deployment, and building data pipelines at scale.
```

Both fields are included in Claude's prompts when scoring and tailoring documents. Write `experience_summary` in a brief narrative style — Claude uses it to identify achievements to highlight in your cover letter.

### Exclude Titles

```yaml
exclude_titles:
  - "Intern"
  - "Graduate"
  - "Manager"
  - "Director"
```

Case-insensitive substring match against job title. Any matching job is dropped before scoring. Use this to filter out roles that share keywords with your target titles but aren't relevant — for example, a search for "Data Scientist" might surface "Data Science Manager" or "Graduate Data Scientist" if you don't exclude them.

### Target Companies

Careers pages scraped directly, bypassing job boards:

```yaml
target_companies:
  - name: "Example Corp"
    careers_url: "https://example.com/careers/"
    ats: greenhouse
    ats_token: example-corp

  - name: "Another Company"
    careers_url: "https://jobs.ashbyhq.com/another-company"
    ats: ashby
    ats_token: another-company

  - name: "Small Startup"
    careers_url: "https://smallstartup.com/jobs"
    ats: generic
```

**Supported ATS types:**

| `ats` value | API used |
|---|---|
| `greenhouse` | `https://boards.greenhouse.io/v1/boards/{ats_token}/jobs` |
| `lever` | `https://api.lever.co/v0/postings/{ats_token}` |
| `ashby` | `https://api.ashbyhq.com/posting-api/job-board/{ats_token}` |
| `generic` | Best-effort HTML scraping (JavaScript-free pages only) |

To find the right `ats_token` for a company, look at the URL of their job listings page. For Greenhouse it's typically the subdomain in `boards.greenhouse.io/{token}`, and similarly for Lever and Ashby. Use `generic` for companies that don't use one of these ATSes — results may be incomplete if their careers page relies on JavaScript.

Companies are scraped in parallel; a failure on one won't block the others.

### Digest Settings

```yaml
digest_email:
  send_top_n: 15                   # Number of top-scored jobs to include in the email
```

Jobs outside the top N that still scored above zero are held over to the next run (not marked as seen). Jobs in the digest and jobs scoring zero are marked seen and won't reappear for 7 days.

---

## Usage

### Run a Scrape

```bash
python main.py --scrape [--limit N] [--verbose]
```

Runs the full pipeline — scrape, filter, score, send digest — and prints a results table to the terminal.

| Flag | Effect |
|---|---|
| `--limit N` | Cap pipeline at N jobs (useful for testing) |
| `--verbose` | Show debug logs (filter decisions, scraper progress, API calls) |

```bash
# Quick test run (no email sent if 0 jobs pass filters)
python main.py --scrape --limit 5 --verbose

# Production run
python main.py --scrape
```

### Start the Reply Daemon

```bash
python main.py --daemon
```

Logs into the bot's inbox, processes any pending replies, then enters IMAP IDLE — waking instantly when you reply to a digest. For each reply:

1. Extracts job indices from your message body (e.g. `"1, 3, 7"`)
2. Looks up those jobs from the most recent digest
3. Tailors your resume and cover letter for each one using Claude
4. Saves `.docx` files to `documents/{Company}/{date}/{Title}/`
5. Emails the tailored documents back to you

The daemon reconnects automatically with exponential backoff (5s → 5min) if the IMAP connection drops.

### Reset the Database

```bash
python main.py --reset-db
```

Deletes `jobs.db` and recreates an empty schema — clearing the 7-day dedup window so all jobs can be re-fetched on the next scrape. Use this when you change location, update your keywords, or want a clean slate.

---

## Tailored Documents

When you reply to a digest with job numbers, the daemon generates two files per job:

```
documents/
└── ExampleCorp/
    └── 2026-03-08_09-00/
        └── Senior_Data_Scientist/
            ├── Resume_Senior_Data_Scientist_ExampleCorp.docx
            └── Cover_Letter_Senior_Data_Scientist_ExampleCorp.docx
```

**Resume:** Claude selects 3–7 bullet points to rewrite, weaving in job-relevant keywords while preserving your formatting, dates, company names, and section headers.

**Cover letter:** Claude writes a fresh 3–4 paragraph body (≤350 words) — opening hook, 2–3 specific achievements from your original, alignment with the role — and replaces the body of your template, preserving the header/contact block above the salutation.

---

## Common Customisations

| What to change | Where |
|---|---|
| Job titles to search | `search.keywords` in `profile.yaml` |
| Salary floor | `search.salary_min_gbp` in `profile.yaml` |
| Target companies | `target_companies` list in `profile.yaml` |
| Filter out unwanted roles | `exclude_titles` in `profile.yaml` |
| Digest length | `digest_email.send_top_n` in `profile.yaml` |
| Number of jobs sent to Claude | `CLAUDE_SHORTLIST_SIZE` constant in `scorer.py` |
| Claude scoring criteria / preferences | `_build_prompt()` in `scorer.py` |
| Resume/cover letter instructions | `adapt_for_job()` in `adaptor.py` |

---

## Deployment

### Scheduled Scrapes (cron)

```bash
crontab -e
```

```
# Run scrape at 08:00 every weekday
0 8 * * 1-5 cd /path/to/jobsearcher && python main.py --scrape >> logs/scrape.log 2>&1
```

### Always-On Daemon (systemd)

Create `/etc/systemd/system/jobsearcher-daemon.service`:

```ini
[Unit]
Description=JobSearcher IMAP Daemon
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/jobsearcher
ExecStart=/path/to/jobsearcher/.venv/bin/python main.py --daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jobsearcher-daemon
sudo systemctl status jobsearcher-daemon
```

### Quick Background Daemon (no systemd)

```bash
nohup python main.py --daemon >> logs/daemon.log 2>&1 &
```

---

## Troubleshooting

**No new jobs found**
- All results may already be in the 7-day dedup window — try `--reset-db`
- Boards may be rate-limiting — check logs with `--verbose`
- Keywords may be too specific — broaden `search.keywords`

**Claude ranking failed — falling back to keyword scores**
- Check `ANTHROPIC_API_KEY` is valid and has credits: [console.anthropic.com](https://console.anthropic.com/settings/billing)
- Verify `claude` CLI is on your `PATH`: `which claude`
- Digest still sends using normalised keyword scores as fallback

**Gmail authentication error**
- Confirm 2FA is enabled on the bot account
- Re-generate and update the App Password in `config/.env`
- Check no other client has locked the bot's IMAP session

**Resume/cover letter tailor failed**
- Confirm `templates/resume.docx` and `templates/cover_letter.docx` exist and are valid `.docx` files (not `.doc`)
- Check Claude CLI is authenticated and API credits are available

**Daemon not processing replies**
- Reply subject must contain `Re:` and `Today's Job Matches`
- Reply must come from the address in `GMAIL_RECIPIENT_ADDRESS`
- Check daemon logs for IMAP reconnect loops

---

## Database Reference

`jobs.db` (SQLite, auto-created on first run):

```sql
-- Deduplication — rolling 7-day window
CREATE TABLE seen_jobs (
    job_id   TEXT PRIMARY KEY,   -- SHA1 of title + company + url
    source   TEXT NOT NULL,      -- 'indeed', 'linkedin', etc.
    seen_at  TEXT NOT NULL       -- ISO-8601 timestamp
);

-- Reply parsing — maps digest index → full job JSON
CREATE TABLE digest_jobs (
    digest_date  TEXT NOT NULL,  -- YYYY-MM-DD
    idx          INTEGER NOT NULL,
    job_id       TEXT NOT NULL,
    job_json     TEXT NOT NULL,
    PRIMARY KEY (digest_date, idx)
);
```

Useful queries:

```bash
# Jobs in today's digest
sqlite3 jobs.db "SELECT idx, json_extract(job_json, '$.title'), json_extract(job_json, '$.company') FROM digest_jobs WHERE digest_date = date('now');"

# Size of dedup window
sqlite3 jobs.db "SELECT COUNT(*) FROM seen_jobs WHERE datetime(seen_at) > datetime('now', '-7 days');"
```
