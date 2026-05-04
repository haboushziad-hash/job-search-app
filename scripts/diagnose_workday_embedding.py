"""Diagnose Workday's embedding pre-filter mortality.

Reads Ziad's profile from the latest audit JSON, scrapes ~30 Workday roles
fresh, computes embedding similarities, and reports the distribution
relative to the kept-roles cutoff (0.584 min, 0.611 median in his last run).

Hypothesis: Workday role text+JDs have systematically lower cosine
similarity to AI-consulting-profile embeddings because their JDs are
heavy on EEO/benefits boilerplate in the first 500 chars (which
_role_to_text uses).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
from pathlib import Path

# Project root
sys.path.insert(0, r'C:\Users\habou\OneDrive\Desktop\Job Search App')

from backend.config import config
from backend.models import CandidateProfile, Role
from backend.scraper.workday import WorkdayScraper, WORKDAY_TENANTS
from backend.scraper.client import ScraperClient
from backend.scoring.embedding_filter import _profile_to_text, _role_to_text, _cosine_similarity
from backend.scoring.llm_client import get_llm_client
import numpy as np


async def main():
    # Load Ziad's profile from audit
    audit_glob = r'C:\Users\habou\Documents\JobSearchApp\audits\runs\*94e49f07*audit.json'
    audit_files = glob.glob(audit_glob)
    if not audit_files:
        print(f'No audit found at {audit_glob}', file=sys.stderr)
        sys.exit(1)
    audit = json.load(open(audit_files[0], encoding='utf-8'))
    profile_dict = audit['profile_snapshot']
    profile = CandidateProfile.model_validate(profile_dict)
    profile_text = _profile_to_text(profile)
    print('PROFILE TEXT FOR EMBEDDING:')
    print(f'  "{profile_text[:200]}..."')
    print()

    # Scrape Workday for the 3 most relevant Tier 1 keywords (just sample)
    keywords = ['AI Strategy Consultant', 'AI Enablement Lead', 'Federal AI Strategy Consultant']
    print(f'Scraping Workday for: {keywords}')
    async with ScraperClient() as http:
        scraper = WorkdayScraper(client=http)
        # Limit to first 5 tenants to keep this fast
        original_tenants = WORKDAY_TENANTS[:]
        WORKDAY_TENANTS.clear()
        WORKDAY_TENANTS.extend(original_tenants[:5])  # Accenture, Citi, PNC, PwC, Leidos
        try:
            wd_roles = await scraper.search(keywords=keywords, posted_within_days=30, limit_per_keyword=20)
        finally:
            WORKDAY_TENANTS.clear()
            WORKDAY_TENANTS.extend(original_tenants)
    print(f'Got {len(wd_roles)} Workday roles')
    print()

    if not wd_roles:
        print('No roles to embed - try different keywords')
        return

    # Sample 12 roles for the diagnostic
    wd_roles = wd_roles[:12]

    # FIX VERIFICATION: fetch JDs via Workday's CXS endpoint (proper)
    # This is what _fetch_missing_jds SHOULD be doing for Workday roles.
    print('=' * 80)
    print(f'FETCHING PROPER JDs VIA WORKDAY CXS for {len(wd_roles)} roles...')
    print('=' * 80)
    async with ScraperClient() as http:
        scraper2 = WorkdayScraper(client=http)
        jd_results = []
        for r in wd_roles:
            jd = await scraper2.fetch_jd(r)
            jd_results.append((r, jd))
            r.job_description_full = jd  # populate as the runner would
    for r, jd in jd_results[:5]:
        print(f'  [{r.company}] {(r.job_title or "")[:55]}: jd_len={len(jd)}')
    avg_jd_len = sum(len(j) for _, j in jd_results) / max(len(jd_results), 1)
    print(f'  Avg JD length after CXS fetch: {avg_jd_len:.0f} chars')
    print()

    # Show the role texts that will be embedded — NOW with JDs
    print('=' * 80)
    print('SAMPLE EMBEDDED TEXTS (with proper JDs):')
    print('=' * 80)
    for r in wd_roles[:5]:
        text = _role_to_text(r)
        title = (r.job_title or "")[:60]
        print(f'\n[{r.company}] {title}')
        print(f'  jd_len: {len(r.job_description_full or "")}')
        print(f'  embedded text ({len(text)} chars): "{text[:300]}..."')
    print()

    # Embed profile + roles
    client = get_llm_client()
    profile_emb = await client.embed(model=config.EMBEDDING_MODEL, texts=[profile_text])
    if not profile_emb:
        print('Profile embedding failed', file=sys.stderr)
        return
    profile_emb = np.array(profile_emb[0], dtype=np.float32)

    role_texts = [_role_to_text(r) for r in wd_roles]
    role_emb = await client.embed(model=config.EMBEDDING_MODEL, texts=role_texts)
    role_matrix = np.array(role_emb, dtype=np.float32)
    sims = _cosine_similarity(profile_emb, role_matrix)

    # Report
    print('=' * 80)
    print(f'WORKDAY EMBEDDING SIMILARITIES vs Ziad profile')
    print('=' * 80)
    print(f'  Cutoff in his Run 3 (40% kept): >= 0.584 (min of survivors)')
    print(f'  Median of Run 3 survivors:      0.611')
    print(f'  Hard-floor min_similarity:      0.45')
    print()
    paired = sorted(zip(sims, wd_roles), key=lambda p: -p[0])
    print(f'  sim    company              title')
    for sim, r in paired:
        marker = '  KILLED' if sim < 0.584 else '  SURVIVES'
        title = (r.job_title or '')[:55]
        print(f'  {sim:6.3f} {r.company[:20]:<20} {title:<55} {marker}')
    print()
    n = len(sims)
    n_killed_by_relative = sum(1 for s in sims if s < 0.584)
    n_killed_by_absolute = sum(1 for s in sims if s < 0.45)
    print(f'== SUMMARY ==')
    print(f'  Total: {n}')
    print(f'  Killed by 40%-cutoff (sim<0.584): {n_killed_by_relative} ({100*n_killed_by_relative//max(n,1)}%)')
    print(f'  Killed by hard floor (sim<0.45):  {n_killed_by_absolute} ({100*n_killed_by_absolute//max(n,1)}%)')
    print(f'  Mean similarity: {sims.mean():.3f}')
    print(f'  Max similarity:  {sims.max():.3f}')
    print(f'  Min similarity:  {sims.min():.3f}')


if __name__ == '__main__':
    asyncio.run(main())
