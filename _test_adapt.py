"""Temporary test script for adapt_for_job pipeline."""
import logging
import yaml
from db import Database
from adaptor import adapt_for_job

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")

with open("config/profile.yaml") as f:
    cfg = yaml.safe_load(f)

with Database() as db:
    job_dict = db.get_digest_job(1)

if job_dict is None:
    print("ERROR: No jobs found in DB. Run --scrape first.")
    raise SystemExit(1)

print(f"\nTesting with: {job_dict['title']} @ {job_dict['company']}")
print(f"URL: {job_dict['url']}")
print(f"Description snippet: {(job_dict.get('description') or '')[:300]}\n")

resume_path, cover_path = adapt_for_job(job_dict, cfg)

print(f"\nResume  -> {resume_path}")
print(f"Cover   -> {cover_path}")
