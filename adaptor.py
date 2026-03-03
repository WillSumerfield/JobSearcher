"""
adaptor.py — Claude-powered resume and cover letter tailoring.

Reads templates/resume.docx and templates/cover_letter.docx, calls Claude to
tailor them for a specific job, and saves the results to:

    documents/{company}/{YYYY-MM-DD_HH-MM}/{job_title}/resume.docx
    documents/{company}/{YYYY-MM-DD_HH-MM}/{job_title}/cover_letter.docx

The resume is modified in-place (selected bullet points rewritten to emphasise
relevant skills).  The cover letter body is fully replaced with a fresh draft
while the header / contact block is preserved.
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from docx import Document

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path("templates")
DOCUMENTS_DIR = Path("documents")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def adapt_for_job(job_dict: dict, cfg: dict) -> tuple[Path, Path]:
    """
    Tailor resume and cover letter for a specific job.

    Args:
        job_dict: Job data dict as stored in the DB
                  (keys: title, company, location, url, description, …)
        cfg:      Parsed profile.yaml.

    Returns:
        (resume_path, cover_letter_path) — absolute Paths to the saved .docx files.

    Raises:
        FileNotFoundError: if templates/resume.docx or templates/cover_letter.docx
                           are missing (user must place their own documents there).
    """
    resume_template = TEMPLATES_DIR / "resume.docx"
    cover_template = TEMPLATES_DIR / "cover_letter.docx"

    for path in (resume_template, cover_template):
        if not path.exists():
            raise FileNotFoundError(
                f"Template not found: {path}\n"
                "Place your resume and cover letter as:\n"
                "  templates/resume.docx\n"
                "  templates/cover_letter.docx"
            )

    out_dir = _output_dir(job_dict)
    out_dir.mkdir(parents=True, exist_ok=True)

    title_slug = _safe_name(job_dict.get("title", "Role"))
    company_slug = _safe_name(job_dict.get("company", "Company"))
    resume_path = out_dir / f"Resume_{title_slug}_{company_slug}.docx"
    cover_path = out_dir / f"Cover_Letter_{title_slug}_{company_slug}.docx"

    logger.info(
        "Tailoring documents for: %s @ %s",
        job_dict.get("title"), job_dict.get("company"),
    )

    _tailor_resume(resume_template, resume_path, job_dict, cfg)
    _tailor_cover_letter(cover_template, cover_path, job_dict, cfg)

    return resume_path, cover_path


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _output_dir(job_dict: dict) -> Path:
    company = _safe_name(job_dict.get("company", "unknown"))
    title = _safe_name(job_dict.get("title", "role"))
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    return DOCUMENTS_DIR / company / ts / title


def _safe_name(name: str) -> str:
    """Strip characters unsafe for directory names; collapse whitespace."""
    safe = re.sub(r"[^\w\s-]", "", str(name)).strip()
    safe = re.sub(r"\s+", "_", safe)
    return safe[:50] or "unknown"


# ---------------------------------------------------------------------------
# Resume tailoring
# ---------------------------------------------------------------------------

def _tailor_resume(src: Path, dst: Path, job_dict: dict, cfg: dict) -> None:
    """
    Load the resume template, ask Claude which bullet points to rewrite,
    apply the changes while preserving formatting, and save.
    """
    doc = Document(src)
    all_paras = _collect_paragraphs(doc)

    # Build numbered list for the prompt (only non-blank paragraphs)
    indexed = [(i, p["text"]) for i, p in enumerate(all_paras) if p["text"].strip()]
    changes = _get_resume_changes(indexed, job_dict, cfg)

    applied = 0
    for change in changes:
        idx = change.get("index")
        new_text = (change.get("text") or "").strip()
        if idx is None or not new_text or idx >= len(all_paras):
            continue
        _replace_paragraph_text(all_paras[idx]["para"], new_text)
        applied += 1

    doc.save(dst)
    logger.info("Resume saved → %s  (%d paragraph(s) updated)", dst, applied)


def _get_resume_changes(
    indexed_paras: list[tuple[int, str]],
    job_dict: dict,
    cfg: dict,
) -> list[dict]:
    """
    Ask Claude to return targeted paragraph rewrites for the resume.

    Returns a list of {"index": int, "text": str} dicts.
    Falls back to [] on any API or parse error.
    """
    skills = ", ".join(cfg.get("skills", []))
    experience = (cfg.get("experience_summary") or "").strip()
    education = "; ".join(cfg.get("education", []))

    job_title = job_dict.get("title", "")
    job_company = job_dict.get("company", "")
    job_description = (job_dict.get("description") or "")[:1500]

    numbered = "\n".join(f"{i}: {text}" for i, text in indexed_paras)

    prompt = f"""\
You are tailoring a resume for a specific job application. Your task: identify \
bullet points and summary sentences that can be rewritten to better highlight \
the applicant's fit for this role.

JOB DETAILS
Title:       {job_title}
Company:     {job_company}
Description: {job_description}

APPLICANT PROFILE
Skills:     {skills}
Education:  {education}
Experience: {experience}

CURRENT RESUME (paragraph index: text)
{numbered}

RULES
1. Only modify bullet-point lines and the profile/summary paragraph.
2. Do NOT change: the applicant's name, contact details, section headers (e.g.
   "Experience", "Education", "Skills"), job titles, company names, or dates.
3. Keep each rewritten paragraph roughly the same length as the original.
4. Weave in relevant keywords from the job description naturally — not keyword-stuffed.
5. Only return changes that genuinely improve the match; aim for 3–7 changes.

Return ONLY a valid JSON array (no markdown fences, no extra text):
[
  {{"index": 3, "text": "Rewritten bullet point here."}},
  {{"index": 7, "text": "Another improved sentence."}}
]

If no changes are beneficial, return: []"""

    try:
        _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=180, env=_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()}")
        raw = result.stdout.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Resume Claude call failed (%s) — no changes applied.", exc)
        return []


# ---------------------------------------------------------------------------
# Cover letter tailoring
# ---------------------------------------------------------------------------

def _tailor_cover_letter(src: Path, dst: Path, job_dict: dict, cfg: dict) -> None:
    """
    Load the cover letter template, preserve the contact/header block, replace
    everything from "Dear..." onward with a Claude-generated draft.
    """
    doc = Document(src)
    paras = doc.paragraphs

    # Find where the salutation ("Dear ...") begins
    body_start = None
    for i, para in enumerate(paras):
        if para.text.strip().lower().startswith("dear"):
            body_start = i
            break

    if body_start is None:
        # No "Dear" found — rewrite everything (unusual template)
        body_start = 0
        logger.warning("Cover letter template has no 'Dear' salutation — rewriting from start.")

    # Extract current body text for style reference
    current_body = "\n".join(p.text for p in paras[body_start:])

    new_body_text = _get_cover_letter_text(current_body, job_dict, cfg)
    new_lines = new_body_text.splitlines()  # preserves blank lines as empty strings

    # Remove all existing body paragraphs from the XML tree
    body_para_elements = [p._element for p in paras[body_start:]]
    for el in body_para_elements:
        el.getparent().remove(el)

    # Append new paragraphs to the document body
    for line in new_lines:
        doc.add_paragraph(line)

    doc.save(dst)
    logger.info("Cover letter saved → %s", dst)


def _get_cover_letter_text(
    current_body: str,
    job_dict: dict,
    cfg: dict,
) -> str:
    """
    Ask Claude to write a tailored cover letter body (salutation through sign-off).

    Falls back to a generic template on API failure.
    """
    profile = cfg.get("profile", {})
    name = profile.get("name", "the applicant")
    skills = ", ".join(cfg.get("skills", []))
    experience = (cfg.get("experience_summary") or "").strip()
    education = "; ".join(cfg.get("education", []))

    job_title = job_dict.get("title", "the role")
    job_company = job_dict.get("company", "your company")
    job_description = (job_dict.get("description") or "")[:1500]

    prompt = f"""\
Write a tailored cover letter body for a job application.

APPLICANT
Name:       {name}
Skills:     {skills}
Education:  {education}
Experience: {experience}

JOB DETAILS
Title:       {job_title}
Company:     {job_company}
Description: {job_description}

CURRENT COVER LETTER BODY (style reference — do not copy verbatim):
{current_body[:800]}

INSTRUCTIONS
Write 3–4 focused paragraphs (≤ 350 words total):
  1. Opening salutation + concise hook — why this specific role and company.
  2. Core paragraph — 2–3 concrete achievements most relevant to the job description.
     Use specific numbers / outcomes where the current letter has them.
  3. Connecting paragraph — how the applicant's background meets the role's key requirements.
  4. Closing — enthusiasm, availability for interview, professional sign-off.

Style: confident, specific, professional. Avoid clichés ("I am passionate about…").
Start with "Dear Hiring Manager," (or an appropriate named salutation if a name is known).
End with "Yours sincerely," followed by a blank line and the applicant's name.

Output ONLY the letter body — plain text, paragraphs separated by blank lines.
No markdown, no JSON, no explanation."""

    try:
        _env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=180, env=_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()}")
        return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cover letter Claude call failed (%s) — using fallback.", exc)
        return (
            f"Dear Hiring Manager,\n\n"
            f"I am writing to apply for the {job_title} position at {job_company}.\n\n"
            f"{experience}\n\n"
            f"I would welcome the opportunity to discuss my application further.\n\n"
            f"Yours sincerely,\n{name}"
        )


# ---------------------------------------------------------------------------
# python-docx helpers
# ---------------------------------------------------------------------------

def _collect_paragraphs(doc: Document) -> list[dict]:
    """
    Return a flat list of every paragraph in the document (body + table cells),
    each as {"para": <Paragraph>, "text": str}.

    The order is: all body paragraphs first, then table-cell paragraphs in
    document order.  This matches the numbered list shown to Claude.
    """
    result: list[dict] = []

    for para in doc.paragraphs:
        result.append({"para": para, "text": para.text})

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    result.append({"para": para, "text": para.text})

    return result


def _replace_paragraph_text(para, new_text: str) -> None:
    """
    Replace a paragraph's visible text while preserving the first run's formatting.
    Extra runs are cleared so no old text bleeds through.
    """
    if not para.runs:
        if new_text:
            para.add_run(new_text)
        return

    para.runs[0].text = new_text
    for run in para.runs[1:]:
        run.text = ""
