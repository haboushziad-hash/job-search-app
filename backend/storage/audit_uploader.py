"""Upload completed audit JSON to Ziad's central Worker.

Fires after each run completes (when AUDIT_UPLOAD_URL is configured).
Failures are non-fatal — local audit folder still has the data.

The upload includes:
  - The full audit JSON dict
  - X-Tester-UUID header (anonymous identifier, generated on first launch)

Privacy: the user's resume content is in the audit JSON via
profile_snapshot. By uploading, the tester implicitly agrees to share their
search results with Ziad for development. The first-launch UI should
disclose this clearly.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from backend.config import config


async def upload_audit(
    audit_dict: dict,
    *,
    timeout_seconds: float = 30.0,
) -> Optional[dict]:
    """POST the audit JSON to AUDIT_UPLOAD_URL. Returns server response dict
    or None on any failure.

    Failures are intentionally silent — the local audit folder always has
    the data, this is just a best-effort centralized copy.
    """
    if not config.AUDIT_UPLOAD_URL:
        return None  # central-server mode disabled
    uuid = config.TESTER_UUID or ""
    if not uuid:
        return None  # no UUID, can't identify the tester

    body = json.dumps(audit_dict, default=str)
    headers = {
        "Content-Type": "application/json",
        "X-Tester-UUID": uuid,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            r = await client.post(config.AUDIT_UPLOAD_URL, content=body, headers=headers)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return {"ok": True}
            return None
    except Exception:
        return None
