"""
adaptor.py — Claude-powered resume and cover letter tailoring.

TODO: Not yet implemented. Will be built in a later step.

Interface (to be implemented):
    adapt_for_job(job_dict: dict, cfg: dict) -> tuple[Path, Path]
        Reads templates/resume.docx and templates/cover_letter.docx,
        calls Claude to rewrite them for the target job, saves to
        documents/{company}/{YYYY-MM-DD_HH-MM}/{job_title}/ and returns
        the two file paths.
"""

from pathlib import Path


def adapt_for_job(job_dict: dict, cfg: dict) -> tuple[Path, Path]:  # noqa: ARG001
    """Tailor resume and cover letter for a specific job. Not yet implemented."""
    raise NotImplementedError(
        "adaptor.py is a stub — resume/cover letter tailoring will be "
        "implemented in a later step."
    )
