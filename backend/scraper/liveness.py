"""Liveness verification — drops dead role URLs before they reach scoring.

Mechanism:
  1. HEAD request on each role URL
  2. Status 200 → keep, mark verified_alive
  3. Status 404/410/403 → drop (dead/forbidden)
  4. Redirect to /expired or careers home → drop
  5. Status 200 + "no longer accepting applications" detected → drop

Cost: ~$0 — pure HTTP requests via Cloudflare or our scraper client.
Speed: ~30-60 seconds for 1000 roles with concurrency=20.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from backend.models import Role
from backend.scraper.client import ScraperClient


# URLs that indicate the role is filled/expired
EXPIRED_URL_PATTERNS = [
    re.compile(r"/expired", re.IGNORECASE),
    re.compile(r"/closed", re.IGNORECASE),
    re.compile(r"/filled", re.IGNORECASE),
    re.compile(r"/jobs?/?$", re.IGNORECASE),  # redirect to jobs index
    re.compile(r"/careers?/?$", re.IGNORECASE),  # redirect to careers home
]


async def _verify_one(
    role: Role,
    client: ScraperClient,
    semaphore: asyncio.Semaphore,
    fetch_body_check: bool = False,
) -> Role:
    """Check liveness of a single role. Mutates role's verification fields."""
    if not role.job_url:
        role.last_verified_result = "no_url"
        role.verification_confidence = "Unknown"
        return role

    async with semaphore:
        try:
            # NOTE: must use the ScraperClient wrapper (.head), NOT the raw
            # client._client.head — the wrapper applies the browser-like headers
            # Workday and other ATS hosts require. A/B (2026-06-03) showed the
            # raw client marks 400/400 ACTIVE roles "dead" (404 on header-less
            # HEAD) — a total false-positive. Speed of this phase is addressed
            # via concurrency, not by bypassing the wrapper.
            response = await client.head(role.job_url, follow_redirects=True)
            status = response.status_code
            final_url = str(response.url)

            role.last_verified_attempt = datetime.now(timezone.utc).isoformat()

            # Status code rules
            if status in (404, 410):
                role.last_verified_result = "dead"
                role.verification_confidence = "High"
                return role
            if status == 403:
                # 403 might be anti-bot; uncertain
                role.last_verified_result = "blocked"
                role.verification_confidence = "Low"
                return role
            if status >= 500:
                role.last_verified_result = "server_error"
                role.verification_confidence = "Low"
                return role

            # Redirect to expired/closed/careers home
            if final_url != role.job_url:
                for pattern in EXPIRED_URL_PATTERNS:
                    if pattern.search(final_url):
                        role.last_verified_result = "expired_redirect"
                        role.verification_confidence = "High"
                        return role

            # Status 200 — looks alive
            role.last_verified_result = "alive"
            role.verification_confidence = "High"

            # Optional: fetch body and look for "no longer accepting" text
            if fetch_body_check:
                body_response = await client.get(role.job_url)
                text_lower = body_response.text.lower()[:5000]
                kill_phrases = [
                    "no longer accepting applications",
                    "this position is no longer available",
                    "this position has been filled",
                    "we are no longer hiring",
                    "applications are no longer being accepted",
                ]
                for phrase in kill_phrases:
                    if phrase in text_lower:
                        role.last_verified_result = "marked_filled"
                        role.verification_confidence = "High"
                        return role

            return role

        except httpx.HTTPStatusError as e:
            role.last_verified_attempt = datetime.now(timezone.utc).isoformat()
            if e.response.status_code in (404, 410):
                role.last_verified_result = "dead"
                role.verification_confidence = "High"
            else:
                role.last_verified_result = f"http_{e.response.status_code}"
                role.verification_confidence = "Low"
            return role
        except Exception as e:
            # Transient failure — don't mark as dead, just unverified
            role.last_verified_attempt = datetime.now(timezone.utc).isoformat()
            role.last_verified_result = f"error: {type(e).__name__}"
            role.verification_confidence = "Unknown"
            return role


async def verify_liveness(
    roles: list[Role],
    *,
    concurrency: int = 60,
    fetch_body_check: bool = False,
    drop_dead: bool = True,
    log: bool = True,
) -> list[Role]:
    """Run liveness check on every role.

    drop_dead: if True (default), filter out roles confirmed dead.
               If False, return all roles with verification fields populated
               (useful for the dashboard's freshness indicator).
    fetch_body_check: also do a GET and scan the body for "no longer accepting"
               text. Slower but catches more dead roles. Use sparingly.
    """
    if not roles:
        return roles

    semaphore = asyncio.Semaphore(concurrency)
    async with ScraperClient() as client:
        tasks = [_verify_one(r, client, semaphore, fetch_body_check) for r in roles]
        verified = await asyncio.gather(*tasks)

    # Compute freshness scores
    for r in verified:
        r.freshness_score = _compute_freshness_score(r)

    if log:
        from collections import Counter
        results = Counter((r.last_verified_result or "unknown") for r in verified)
        print(f"[liveness] verified {len(verified)} roles: {dict(results)}")

    if drop_dead:
        # "blocked" (403) is usually anti-bot, NOT actually dead — keep these.
        # Confirmed dead: 404/410, redirects to expired, marked-filled body text.
        confirmed_dead = {"dead", "expired_redirect", "marked_filled"}
        kept = [r for r in verified if (r.last_verified_result or "") not in confirmed_dead]
        if log:
            print(f"[liveness] dropped {len(verified) - len(kept)} confirmed dead/expired roles")
        return kept
    return verified


def _compute_freshness_score(role: Role) -> int:
    """0-100 freshness score based on posted_date age + verification result."""
    score = 50  # baseline

    # Age component
    if role.posted_date:
        from backend.filter.hard_filters import _parse_date_lenient
        posted = _parse_date_lenient(role.posted_date)
        if posted:
            age_days = (datetime.now(timezone.utc) - posted).days
            if age_days <= 3:
                score = 95
            elif age_days <= 7:
                score = 85
            elif age_days <= 14:
                score = 70
            elif age_days <= 30:
                score = 55
            elif age_days <= 60:
                score = 35
            else:
                score = 20

    # Verification component
    if role.last_verified_result == "alive":
        score = min(100, score + 5)
    elif role.last_verified_result in ("dead", "expired_redirect", "marked_filled"):
        score = 0

    return score
