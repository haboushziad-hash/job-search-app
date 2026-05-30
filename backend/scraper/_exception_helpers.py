"""Shared exception-visibility helper for scrapers.

Wave-2B Phase 2 (FIX 15, 2026-05-30) — cross-cutting fix.

Background: the deep-dive workflow found that 9 of 14 scrapers had bare
`except Exception: return []` swallows. Each one hid a real regression
when Phase 1 changed pacing / timeouts / endpoints (RemoteOK Cloudflare
burst, HigherEdJobs DataDome challenges, BuiltIn timeout, etc.). Without
observability we kept flying blind and shipping regressions.

This module provides one helper called from every scraper's except
branches. Behavior: logs the failure with class + URL + first 160 chars,
then sets `quota_exhausted` on 429/403/503 transport failures so the
orchestrator's audit JSON reflects what happened.

Pure additive — no behavior change other than visibility + correct
quota_exhausted classification. Adopt incrementally; scrapers can keep
their existing except branches if they already have explicit telemetry
(BuiltIn 403 handler, SmartRecruiters non-200 mapping, etc.).

Usage:
    from backend.scraper._exception_helpers import handle_scraper_exception
    ...
    try:
        resp = await self.client._client.get(url, headers=headers)
    except Exception as _e:
        handle_scraper_exception(self, "RemoteOK", _e, context=f"tag='{tag}'")
        return []
"""
from __future__ import annotations

from typing import Any


def _extract_status(exception: BaseException) -> int | None:
    """Best-effort extract HTTP status code from common exception shapes.

    Handles httpx.HTTPStatusError (response.status_code), tenacity wrappers
    that carry the response on the cause chain, and the fallback case where
    the exception has a `.response.status_code` attr regardless of class.
    """
    # Direct attribute path (httpx.HTTPStatusError and most wrappers)
    resp = getattr(exception, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            return status

    # Walk __cause__ chain (tenacity sometimes re-raises wrapping the
    # original HTTPStatusError). One level is enough — beyond that we'd
    # be guessing.
    cause = getattr(exception, "__cause__", None)
    if cause is not None:
        resp = getattr(cause, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
            if isinstance(status, int):
                return status
    return None


def handle_scraper_exception(
    scraper_instance: Any,
    source_name: str,
    exception: BaseException,
    *,
    context: str = "",
) -> None:
    """Log + classify a scraper exception.

    Args:
        scraper_instance: the scraper class instance (sets
            quota_exhausted/quota_exhausted_reason on it for 429/403/503).
        source_name: human-readable scraper name (e.g. "RemoteOK") for
            the log prefix.
        exception: the caught exception.
        context: free-form note about what was failing (e.g.
            "tag='marketing'", "tenant='anthropic'", "category=22").
            Joined to the log line so future regressions are debuggable
            without diff'ing the codebase.

    Always logs (best-effort, swallows print errors).
    Sets quota_exhausted only when the status code maps to a soft-ban
    pattern (429 rate limit, 403 anti-bot, 503 service unavailable).
    """
    err_class = type(exception).__name__
    err_msg = str(exception)[:160]
    ctx_suffix = f" {context}" if context else ""
    try:
        print(
            f"[{source_name.lower()}] {err_class}{ctx_suffix}: {err_msg}",
            flush=True,
        )
    except Exception:
        # Logging must never break the caller — swallow any print failure
        # (closed stdout in tests, etc.) and continue with classification.
        pass

    status = _extract_status(exception)
    if status in (429, 403, 503):
        try:
            scraper_instance.quota_exhausted = True
            scraper_instance.quota_exhausted_reason = (
                f"{source_name} HTTP {status}{ctx_suffix}"
            )
        except Exception:
            # If the scraper doesn't expose these attrs (shouldn't happen
            # with BaseScraper), silently swallow rather than crash the
            # caller. The log line above still went through.
            pass
