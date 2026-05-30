"""Ashby scraper.

Ashby exposes a public job board API:
  https://api.ashbyhq.com/posting-api/job-board/{org_id}?includeCompensation=true

Returns: { "jobs": [ { id, title, descriptionHtml, locationName, employmentType,
                       publishedAt, departmentName, jobUrl, ... } ], ... }
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html, _parse_iso_date
from backend.scraper import _keyword_match as _kw_match


# Curated list of orgs known to use Ashby. Each entry maps display name → org slug.
ASHBY_COMPANIES: list[tuple[str, str]] = [
    ("Linear", "linear"),
    ("Ramp", "ramp"),
    ("Mercury", "Mercury"),
    ("Notion", "notion"),
    ("Perplexity", "perplexity"),
    ("Modal", "modal"),
    ("Replit", "replit"),
    ("Vercel", "vercel"),
    ("Pinecone", "Pinecone"),
    ("Sierra", "sierra"),
    ("Decagon", "decagon"),
    ("Cursor", "cursor"),
    ("Reka", "reka"),
    ("Anyscale", "anyscale"),
    ("Wiz", "wiz"),
    ("Snyk", "snyk"),
    ("Vanta", "vanta"),
    ("Drata", "drata"),
    ("Persona", "persona"),
    ("Stytch", "stytch"),
    ("WorkOS", "workos"),
    ("Modern Treasury", "moderntreasury"),
    ("Unit", "unit"),
    ("Plaid", "plaid"),
    ("Hightouch", "hightouch"),
    ("Whatnot", "whatnot"),
    ("Stedi", "stedi"),

    # ========== Phase 1 expansion (2026-05-03) — AI-native + dev-tools ==========
    ("Character AI", "character"),
    ("Mistral AI", "mistral"),
    ("Cohere", "cohere"),
    ("Supabase", "supabase"),
    ("Substack", "substack"),
    ("PostHog", "posthog"),
    ("LaunchDarkly", "launchdarkly"),
    ("Speak", "speak"),
    # Phase C1 (verified live 2026-05-03)
    ("Browserbase",       "browserbase"),       #  11 jobs — browser-automation infra
    # Phase C1 v2 — broader employer pool
    ("Atomic Industries", "atomic"),            #  10 jobs — manufacturing / industrial

    # Phase C2 — companies that MIGRATED FROM GREENHOUSE TO ASHBY
    # Discovered 2026-05-03 via probe_all_companies_health.py — these were
    # 404'ing on our Greenhouse list because they moved their job boards.
    # Net coverage gain: ~2,289 jobs across 19 tenants.
    ("OpenAI",            "openai"),            # 671 jobs — AI lab
    ("Snowflake",         "snowflake"),         # 421 jobs — data cloud
    ("Harvey",            "harvey"),            # 261 jobs — AI for law
    ("Writer",            "writer"),            #  48 jobs — enterprise AI writing
    ("SentiLink",         "sentilink"),         #  44 jobs — fraud detection
    ("Dust",              "dust"),              #  25 jobs — AI agents platform
    ("Zapier",            "zapier"),            #  25 jobs — automation

    # ========== Wave-2B Phase 2 (FIX 8, 2026-05-30) — 21 verified-live ==
    # Sourced from deep-dive workflow synthesis. Each verified to return
    # active postings at probe time. If any 404 in production, the existing
    # _fetch_company_jobs except branch swallows them gracefully and the
    # orchestrator's dead_tenants tracker logs them for pruning.
    ("Saronic",           "saronic"),           # ~276 — defense / autonomy
    ("Applied Intuition", "appliedintuition"),  # ~213 — autonomous vehicles
    ("ElevenLabs",        "elevenlabs"),        # ~149 — voice AI
    ("Lovable",           "lovable"),           #  ~81 — AI app builder
    ("Deepgram",          "deepgram"),          #  ~60 — speech-to-text
    ("Watershed",         "watershed"),         #  ~38 — climate
    ("Vapi",              "vapi"),              #  ~26 — voice agents
    ("Cube",              "cube"),              #  ~18 — analytics
    ("Mintlify",          "mintlify"),          #  ~14 — dev docs
    ("Magic School",      "magicschool"),       #   ~3 — edtech
    ("Airbyte",           "airbyte"),           # data pipelines
    ("Neon",              "neon"),              # serverless postgres
    ("Prefect",           "prefect"),           # data orchestration
    ("Weaviate",          "weaviate"),          # vector DB
    ("Jamf",              "jamf"),              # device management
    ("Gong",              "gong"),              # revenue intelligence
    ("Klaviyo",           "klaviyo"),           # email marketing
    ("Brex",              "brex"),              # fintech
    ("Faire",             "faire"),             # wholesale marketplace
    ("Lattice",           "lattice"),           # HR tech
    ("Rippling",          "rippling"),          # HRIS
]


class AshbyScraper(BaseScraper):
    source_name = "Ashby"
    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            if posted_within_days else None
        )
        keywords_lower = [k.lower() for k in keywords]

        # v0.2.1: cap concurrent in-flight requests at 20 (same rationale
        # as Greenhouse). ScraperClient paces per-domain delay but doesn't
        # bound concurrent count. With 44 deduped Ashby tenants, fan-out
        # is small but bounded burst is still better hygiene.
        sem = asyncio.Semaphore(20)
        async def _bounded(slug: str, display_name: str) -> list[Role]:
            async with sem:
                return await self._fetch_company_jobs(slug, display_name)
        tasks = [
            _bounded(slug, display_name)
            for display_name, slug in ASHBY_COMPANIES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_roles: list[Role] = []
        seen_url: set[str] = set()
        seen_title_company: set[tuple[str, str]] = set()

        for result in results:
            if isinstance(result, Exception):
                continue
            for role in result:
                if role.job_url and role.job_url in seen_url:
                    continue
                key = (
                    (role.job_title or "").strip().lower(),
                    (role.company or "").strip().lower(),
                )
                if key[0] and key in seen_title_company:
                    continue
                if cutoff and role.posted_date:
                    posted = _parse_iso_date(role.posted_date)
                    if posted and posted < cutoff:
                        continue
                # Token-overlap match (shared via _keyword_match.py).
                # Replaces substring matching — Ashby went from 12 to 50+
                # matches per run.
                if not _kw_match.matches_any_keyword(
                    role.job_title or "",
                    role.job_description_full or "",
                    keywords_lower,
                ):
                    continue
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_title_company.add(key)
                all_roles.append(role)

        return all_roles

    async def _fetch_company_jobs(self, slug: str, display_name: str) -> list[Role]:
        url = f"{self.BASE_URL}/{slug}"
        try:
            data = await self.client.get_json(url, params={"includeCompensation": "true"})
        except Exception as _e:
            # Wave-2B Phase 2 (visibility): surface per-tenant failures so the
            # orchestrator's dead_tenants tracker can prune them. Previously
            # this bare except hid 404s on retired tenants.
            status = getattr(getattr(_e, "response", None), "status_code", None)
            if status == 404:
                if not hasattr(self, "dead_tenants"):
                    self.dead_tenants = {}
                self.dead_tenants[slug] = "404 (tenant retired or migrated)"
            elif status in (429, 403, 503):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Ashby tenant '{slug}' HTTP {status}"
                )
            return []
        jobs = data.get("jobs") or []
        roles: list[Role] = []
        for j in jobs:
            try:
                roles.append(self._job_to_role(j, display_name))
            except Exception:
                continue
        return roles

    def _job_to_role(self, job: dict[str, Any], company: str) -> Role:
        title = job.get("title") or ""
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        location_text = job.get("locationName") or ""
        location_type_raw = (job.get("workplaceType") or job.get("employmentType") or "").lower()

        # Ashby has explicit workplaceType: "Remote", "Hybrid", "On-Site"
        if "remote" in location_type_raw:
            location_type = "Remote"
            # Wave-2B Phase 2 (FIX 24, 2026-05-30): pure-remote Ashby roles
            # often have empty locationName. Populate with "Remote" so
            # the dashboard displays SOMETHING instead of blank. Mirrors
            # the same fix applied to Findwork. Display-only — doesn't
            # affect hard_filter (which treats remote roles as
            # location-agnostic).
            if not location_text:
                location_text = "Remote"
        elif "hybrid" in location_type_raw:
            location_type = "Hybrid"
        elif location_text:
            location_type, _ = self._classify_location(location_text)
        else:
            location_type = "On-site"

        jd_html = job.get("descriptionHtml") or job.get("description") or ""
        jd_text = _strip_html(jd_html) if jd_html else (job.get("descriptionPlain") or "")

        # Compensation
        salary_text = None
        salary_min = None
        salary_max = None
        comp = job.get("compensation") or {}
        if comp:
            sr = comp.get("compensationTierSummary") or ""
            if sr:
                salary_text = sr
            import re as _re
            def _parse_amounts(text: str) -> list[int]:
                if not text:
                    return []
                nums = _re.findall(r"\$?(\d{2,3}(?:,\d{3})+|\d{2,3}[Kk])", text)
                out: list[int] = []
                for n in nums[:2]:
                    n2 = n.replace(",", "")
                    if n2.lower().endswith("k"):
                        out.append(int(float(n2[:-1]) * 1000))
                    else:
                        out.append(int(float(n2)))
                return out

            # Try component summaries first (more granular)
            for tier in (comp.get("compensationTiers") or []):
                parsed = _parse_amounts(tier.get("componentSummary") or "")
                if parsed:
                    salary_min = salary_min or parsed[0]
                    if len(parsed) > 1:
                        salary_max = salary_max or parsed[1]

            # Fallback — parse the human-readable tier summary
            # (some Ashby tenants only populate the summary, not tiers)
            if salary_min is None and salary_max is None and sr:
                parsed = _parse_amounts(sr)
                if parsed:
                    salary_min = parsed[0]
                    if len(parsed) > 1:
                        salary_max = parsed[1]

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location_text or None,
            location_type=location_type,
            job_description_full=jd_text,
            jd_completeness="Full" if jd_text else "Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=job.get("publishedAt") or job.get("updatedAt"),
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            response = await self.client.get(role.job_url)
            return _strip_html(response.text)
        except Exception:
            return ""
