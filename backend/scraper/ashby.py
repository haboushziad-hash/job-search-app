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


# Curated list of orgs known to use Ashby. Each entry maps display name → org slug.
ASHBY_COMPANIES: list[tuple[str, str]] = [
    ("Linear", "linear"),
    ("Ramp", "ramp"),
    ("Mercury", "Mercury"),
    ("Notion", "notion"),
    ("Anthropic", "anthropic"),
    ("Perplexity", "perplexity"),
    ("Modal", "modal"),
    ("Replit", "replit"),
    ("Vercel", "vercel"),
    ("Linear", "linear"),
    ("Pinecone", "Pinecone"),
    ("Sierra", "sierra"),
    ("Cresta", "cresta"),
    ("Glean", "glean"),
    ("Hex", "hex"),
    ("Decagon", "decagon"),
    ("Augment", "augment"),
    ("Codeium", "codeium"),
    ("Cursor", "cursor"),
    ("Sana", "sana"),
    ("Reka", "reka"),
    ("Adept", "adept"),
    ("Imbue", "imbue"),
    ("Together AI", "togetherai"),
    ("Anyscale", "anyscale"),
    ("Datadog", "datadog"),
    ("Wiz", "wiz"),
    ("Snyk", "snyk"),
    ("Vanta", "vanta"),
    ("Drata", "drata"),
    ("Persona", "persona"),
    ("Stytch", "stytch"),
    ("WorkOS", "workos"),
    ("Modern Treasury", "moderntreasury"),
    ("Increase", "increase"),
    ("Unit", "unit"),
    ("Plaid", "plaid"),
    ("Brex", "brex"),
    ("Pulley", "pulley"),
    ("AngelList", "angellist"),
    ("Zip", "ziphq"),
    ("Pavilion", "pavilion"),
    ("Census", "census"),
    ("Hightouch", "hightouch"),
    ("Whatnot", "whatnot"),
    ("Stedi", "stedi"),
    ("Ashby", "ashbyhq"),

    # ========== Phase 1 expansion (2026-05-03) — AI-native + dev-tools ==========
    ("EvenUp", "evenup"),
    ("Dust", "dustai"),
    ("Adept", "adept"),
    ("Character AI", "character"),
    ("Inflection", "inflection"),
    ("Mistral AI", "mistral"),
    ("Together AI", "together"),
    ("Pinecone", "pinecone"),
    ("Weights & Biases", "wandb"),
    ("Hugging Face", "huggingface"),
    ("Cohere", "cohere"),
    ("Replicate", "replicate"),
    ("Supabase", "supabase"),
    ("Tigris Data", "tigrisdata"),
    ("Substack", "substack"),
    ("Discord", "discord"),
    ("PostHog", "posthog"),
    ("LaunchDarkly", "launchdarkly"),
    ("Speak", "speak"),
    ("Decagon", "decagon"),
    ("Sierra", "sierra"),
    ("Cresta", "cresta"),
    ("Sana", "sana"),
    ("Augment", "augment"),
    ("Codeium", "codeium"),
    ("Cursor", "cursorai"),
    ("Notion", "notion"),
    ("Linear", "linear"),
    ("Mercury", "mercury"),
    # Phase C1 (verified live 2026-05-03)
    ("Browserbase",       "browserbase"),       #  11 jobs — browser-automation infra
    # Phase C1 v2 — broader employer pool
    ("Mistral",           "mistral"),           # 174 jobs — AI / European
    ("Modal Labs",        "modal"),             #  28 jobs — ML infra
    ("Atomic Industries", "atomic"),            #  10 jobs — manufacturing / industrial

    # Phase C2 — companies that MIGRATED FROM GREENHOUSE TO ASHBY
    # Discovered 2026-05-03 via probe_all_companies_health.py — these were
    # 404'ing on our Greenhouse list because they moved their job boards.
    # Net coverage gain: ~2,289 jobs across 19 tenants.
    ("OpenAI",            "openai"),            # 671 jobs — AI lab
    ("Snowflake",         "snowflake"),         # 421 jobs — data cloud
    ("Harvey",            "harvey"),            # 261 jobs — AI for law
    ("Vanta",             "vanta"),             # 146 jobs — compliance automation
    ("Notion",            "notion"),            # 140 jobs (replaces stub above)
    ("Cohere",            "cohere"),            # 124 jobs — AI lab
    ("Ramp",              "ramp"),              # 116 jobs — fintech / spend mgmt
    ("Plaid",             "plaid"),             #  96 jobs — fintech infra
    ("Perplexity",        "perplexity"),        #  68 jobs — AI search
    ("Writer",            "writer"),            #  48 jobs — enterprise AI writing
    ("SentiLink",         "sentilink"),         #  44 jobs — fraud detection
    ("Dust",              "dust"),              #  25 jobs — AI agents platform
    ("Zapier",            "zapier"),            #  25 jobs — automation
    ("Drata",             "drata"),             #  24 jobs — compliance automation
    ("Linear",            "linear"),            #  23 jobs (replaces stub above)
    ("Character.ai",      "character"),         #  17 jobs — AI chat platform
    ("Pinecone",          "pinecone"),          #   8 jobs — vector DB
    ("Modern Treasury",   "moderntreasury"),    #   5 jobs — payments infra
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

        tasks = [
            self._fetch_company_jobs(slug, display_name)
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
                searchable = (
                    (role.job_title or "") + " " +
                    (role.job_description_full or "")[:2000]
                ).lower()
                if not any(kw in searchable for kw in keywords_lower):
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
        except Exception:
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
            for tier in (comp.get("compensationTiers") or []):
                comp_data = tier.get("componentSummary") or ""
                # Try to extract numbers
                import re as _re
                nums = _re.findall(r"\$?(\d{2,3}(?:,\d{3})+|\d{2,3}[Kk])", comp_data)
                if nums:
                    parsed = []
                    for n in nums[:2]:
                        n2 = n.replace(",", "")
                        if n2.lower().endswith("k"):
                            parsed.append(int(float(n2[:-1]) * 1000))
                        else:
                            parsed.append(int(float(n2)))
                    if parsed:
                        salary_min = salary_min or parsed[0]
                        if len(parsed) > 1:
                            salary_max = salary_max or parsed[1]

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
