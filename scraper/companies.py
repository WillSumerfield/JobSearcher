"""
scraper/companies.py
Scrapes specific company career pages for job listings.

TODO: Not yet implemented. This will be built in a later step once the
      board scraper is working and verified.

Interface (to be implemented):
    scrape_company(company_config: dict) -> list[Job]
        company_config is one entry from target_companies in profile.yaml:
            {"name": "Monzo", "careers_url": "https://monzo.com/careers/"}
"""

from scraper.models import Job


def scrape_company(company_config: dict) -> list[Job]:  # noqa: ARG001
    """Scrape a single company's career page. Not yet implemented."""
    raise NotImplementedError(
        "scraper/companies.py is a stub — company career page scraping "
        "will be implemented in a later step."
    )


def scrape_all_companies(cfg: dict) -> list[Job]:
    """Scrape all target_companies listed in profile.yaml. Not yet implemented."""
    raise NotImplementedError(
        "scraper/companies.py is a stub — company career page scraping "
        "will be implemented in a later step."
    )
