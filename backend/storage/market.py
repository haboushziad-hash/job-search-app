"""Cross-tester market intelligence — automatic via shared cloud-synced folder.

Architecture:
  - Each tester's audit folder contains a `market_contributions.jsonl`
    that records every (company, title, score, profile_tags, date) tuple
    from their qualifying roles.
  - Before keyword generation, the app scans SIBLING folders inside the
    same parent directory (i.e. other testers whose folders are in the
    same shared cloud-drive parent) and reads their .jsonl files.
  - The combined corpus is filtered to entries where the contributing
    user's profile_tags overlap with the current candidate's by ≥ 20%.
  - Top 50 matching titles get injected into the keyword-gen prompt as
    "real titles seen in market for similar candidates."

No server. No manual sync. Cloud drive is the infrastructure — it
distributes everyone's contributions to everyone within ~30-60 sec
of each write. Works for any number of testers up to maybe 15-20.

Folder layout the user is expected to set up:
    <SHARED_PARENT>/
      ├── ziad/
      │   └── runs.db, market_contributions.jsonl, runs/, diffs/
      ├── zach/
      │   └── ...
      └── tester3/
          └── ...
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


# Filename inside each tester's audit folder
CONTRIBUTIONS_FILENAME = "market_contributions.jsonl"

# Minimum profile-tag overlap to use another tester's contributions
DEFAULT_MIN_OVERLAP = 0.20

# Max market titles injected into the keyword-gen prompt
DEFAULT_MAX_INJECTED = 50


def export_contributions(
    audit_folder: Path,
    *,
    contributions: Iterable[dict],
) -> int:
    """Append our own qualifying-role contributions to local
    market_contributions.jsonl. Each contribution is a dict with
    company, job_title, score, profile_tags. Returns the number written.

    Idempotent: writes only one line per (company, title) pair within
    the existing file (existing entries are not overwritten unless score
    has changed by more than 5 points, in which case the new entry
    replaces the old)."""
    audit_folder = Path(audit_folder)
    audit_folder.mkdir(parents=True, exist_ok=True)
    path = audit_folder / CONTRIBUTIONS_FILENAME

    # Read existing entries to dedupe + check for score changes
    existing: dict[tuple[str, str], dict] = {}
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    key = (
                        (entry.get("company") or "").strip().lower(),
                        (entry.get("job_title") or "").strip().lower(),
                    )
                    existing[key] = entry
                except Exception:
                    continue
        except Exception:
            pass

    new_count = 0
    for c in contributions:
        company = (c.get("company") or "").strip()
        title = (c.get("job_title") or "").strip()
        if not company or not title:
            continue
        key = (company.lower(), title.lower())
        prev = existing.get(key)
        score = int(c.get("score", 0))
        # Skip rewrite if existing entry has same score (within 5 points)
        if prev and abs(int(prev.get("score", 0)) - score) <= 5:
            continue
        existing[key] = {
            "company": company,
            "job_title": title,
            "score": score,
            "profile_tags": list(c.get("profile_tags") or []),
            "contributed_date": c.get("contributed_date") or datetime.now(timezone.utc).isoformat(),
        }
        new_count += 1

    # Atomic-ish rewrite (sort for stable diffs in cloud-synced files)
    sorted_entries = sorted(existing.values(), key=lambda e: (e["company"], e["job_title"]))
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in sorted_entries:
            f.write(json.dumps(e) + "\n")
    tmp.replace(path)

    return new_count


def read_sibling_contributions(audit_folder: Path) -> list[dict]:
    """Scan sibling tester folders (same parent dir as this audit folder)
    for their market_contributions.jsonl files. Returns the union of all
    entries across siblings. Excludes our own contributions (the caller
    already has those)."""
    audit_folder = Path(audit_folder).expanduser().resolve()
    parent = audit_folder.parent
    if not parent.exists():
        return []

    out: list[dict] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        # Skip ourselves
        try:
            if child.resolve() == audit_folder:
                continue
        except Exception:
            continue
        sibling_file = child / CONTRIBUTIONS_FILENAME
        if not sibling_file.exists():
            continue
        try:
            for line in sibling_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry["_source_user"] = child.name
                    out.append(entry)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def filter_by_tag_overlap(
    entries: list[dict],
    *,
    candidate_tags: list[str],
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> list[dict]:
    """Keep entries where the contributing user's profile_tags overlap
    with the candidate_tags by at least min_overlap (default 20%).

    Overlap = |intersection| / |union| (Jaccard) — symmetric and stable."""
    candidate_set = {t.lower().strip() for t in candidate_tags if t}
    if not candidate_set:
        return []
    out: list[dict] = []
    for e in entries:
        e_tags = {t.lower().strip() for t in (e.get("profile_tags") or []) if t}
        if not e_tags:
            continue
        intersection = candidate_set & e_tags
        union = candidate_set | e_tags
        overlap = len(intersection) / max(1, len(union))
        if overlap >= min_overlap:
            e["_overlap"] = overlap
            out.append(e)
    return out


def read_seed_contributions() -> list[dict]:
    """Read the bundled seed market_contributions.jsonl that ships with
    the .msi/.dmg. Populated from the dev's pre-launch validation runs
    (Ziad/Zach/Ryan) so a fresh tester install has cross-tester learning
    available on day one — before they've synced any audit folder.

    Returns [] if the seed file isn't present (dev environment without
    the bundled data file)."""
    # Resolve relative to this module so it works both in dev and inside
    # the PyInstaller bundle (where __file__ resolves to a temp dir).
    seed_path = Path(__file__).resolve().parent.parent / "data" / "seed_market_contributions.jsonl"
    if not seed_path.exists():
        return []
    out: list[dict] = []
    try:
        for line in seed_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry["_source_user"] = "_seed"
                out.append(entry)
            except Exception:
                continue
    except Exception:
        pass
    return out


def market_titles_for_keyword_prompt(
    audit_folder: Path,
    *,
    candidate_tags: list[str],
    own_contributions: Optional[list[dict]] = None,
    max_titles: int = DEFAULT_MAX_INJECTED,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
) -> list[dict]:
    """Build the list of "real market titles" to inject into the keyword
    generator's prompt. Combines:
      1. OWN past contributions (no overlap filter — they're us)
      2. Sibling tester contributions (cloud-synced — ≥ min_overlap filter)
      3. Bundled seed contributions from pre-launch validation runs
         (≥ min_overlap filter, same as siblings)
    Returns top max_titles entries sorted by score desc.
    """
    pool: list[dict] = []
    # Own contributions: include all (no overlap filter — they're us)
    for e in own_contributions or []:
        pool.append({**e, "_overlap": 1.0})
    # Sibling contributions: filter by overlap
    siblings = read_sibling_contributions(audit_folder)
    pool.extend(filter_by_tag_overlap(
        siblings,
        candidate_tags=candidate_tags,
        min_overlap=min_overlap,
    ))
    # Bundled seed contributions: filter by overlap (same threshold as siblings).
    # This ensures Day-1 testers benefit from pre-launch validation runs
    # without leaking unrelated profile data — only matching-tag entries
    # surface. Seed entries are always relevant when overlap meets threshold.
    seed = read_seed_contributions()
    pool.extend(filter_by_tag_overlap(
        seed,
        candidate_tags=candidate_tags,
        min_overlap=min_overlap,
    ))
    # Dedupe by (company, title) — keep entry with highest score
    by_key: dict[tuple[str, str], dict] = {}
    for e in pool:
        key = (
            (e.get("company") or "").strip().lower(),
            (e.get("job_title") or "").strip().lower(),
        )
        if not key[0] or not key[1]:
            continue
        prev = by_key.get(key)
        if prev is None or e.get("score", 0) > prev.get("score", 0):
            by_key[key] = e
    # Top N by score
    return sorted(by_key.values(), key=lambda e: -e.get("score", 0))[:max_titles]
