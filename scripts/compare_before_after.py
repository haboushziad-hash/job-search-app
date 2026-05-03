"""Compare prior run vs latest run for both Ziad and Zach.

Reads the two newest audit JSONs per profile and reports:
  - Tier counts before/after
  - Coverage % before/after (JD, salary)
  - New roles that weren't in the prior run
  - Score deltas on overlapping roles (>= 5pt change)
  - Industry distribution change
  - Coverage gap signal change
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


RUNS_DIR = Path(__file__).resolve().parent / "full_search_output" / "runs"


def load_audits_by_profile() -> dict[str, list[Path]]:
    """Group audit JSONs by profile (using headline as the grouping key)."""
    audits = sorted(RUNS_DIR.glob("*_audit.json"))
    by_profile: dict[str, list[Path]] = {}
    for path in audits:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            headline = (data.get("profile_snapshot") or {}).get("headline") or "?"
            # Crude profile-key: first 3 words of headline
            key = " ".join(headline.split()[:3]).lower()
        except Exception:
            key = "?"
        by_profile.setdefault(key, []).append(path)
    return by_profile


def summarize(audit: dict) -> dict:
    funnel = audit.get("pipeline_funnel") or {}
    tier = funnel.get("tier_breakdown") or {}
    cov = funnel.get("coverage") or {}
    qual = audit.get("all_qualifying_roles") or []
    industries = Counter((r.get("industry") or "Unknown") for r in qual)
    company_count = len({(r.get("company") or "").lower() for r in qual})
    salary_filled = sum(1 for r in qual if r.get("salary_min") or r.get("salary_max"))
    summary_filled = sum(1 for r in qual if r.get("summary"))
    return {
        "scraped": funnel.get("total_scraped"),
        "after_filter": funnel.get("after_hard_filters"),
        "qualifying": funnel.get("qualifying_final"),
        "STRONG": tier.get("STRONG"),
        "GOOD": tier.get("GOOD"),
        "MAYBE": tier.get("MAYBE"),
        "STRETCH": tier.get("STRETCH"),
        "jd_pct": cov.get("jd_coverage_pct"),
        "sal_pct": cov.get("salary_coverage_pct"),
        "loc_pct": cov.get("location_coverage_pct"),
        "industries": dict(industries),
        "distinct_companies": company_count,
        "qualifying_with_salary": salary_filled,
        "qualifying_with_summary": summary_filled,
        "gap": (audit.get("coverage_gap_analysis") or {}),
    }


def diff_runs(prior: dict, latest: dict) -> dict:
    prior_keys = {(r.get("company", "") + "::" + r.get("title", "")).lower(): r for r in (prior.get("all_qualifying_roles") or [])}
    latest_keys = {(r.get("company", "") + "::" + r.get("title", "")).lower(): r for r in (latest.get("all_qualifying_roles") or [])}
    new = [r for k, r in latest_keys.items() if k not in prior_keys]
    gone = [r for k, r in prior_keys.items() if k not in latest_keys]
    score_changes = []
    for k in latest_keys:
        if k not in prior_keys:
            continue
        ps = prior_keys[k].get("score") or 0
        ls = latest_keys[k].get("score") or 0
        if abs(ls - ps) >= 5:
            score_changes.append((prior_keys[k].get("company"), prior_keys[k].get("title"), ps, ls))
    return {
        "new_count": len(new),
        "gone_count": len(gone),
        "score_change_count": len(score_changes),
        "new_top": [(r.get("score"), r.get("company"), r.get("title")) for r in sorted(new, key=lambda r: -(r.get("score") or 0))[:8]],
        "gone_top": [(r.get("score"), r.get("company"), r.get("title")) for r in sorted(gone, key=lambda r: -(r.get("score") or 0))[:5]],
        "score_changes": sorted(score_changes, key=lambda t: -abs(t[3] - t[2]))[:10],
    }


def main():
    by_profile = load_audits_by_profile()
    if not by_profile:
        print("No audit JSONs found.")
        return

    for key, paths in by_profile.items():
        if len(paths) < 2:
            print(f"\n=== {key} === only {len(paths)} run(s), need 2 to diff")
            continue
        prior = json.loads(paths[-2].read_text(encoding="utf-8"))
        latest = json.loads(paths[-1].read_text(encoding="utf-8"))
        ps = summarize(prior)
        ls = summarize(latest)
        d = diff_runs(prior, latest)

        print(f"\n{'#'*70}")
        print(f"# {key.upper()} — {paths[-2].name} -> {paths[-1].name}")
        print(f"{'#'*70}")

        def fmt(v):
            if v is None: return "?"
            if isinstance(v, float): return f"{v:.0f}"
            return str(v)

        print(f"\n{'metric':<20} {'before':>12} {'after':>12} {'delta':>8}")
        print("-" * 60)
        for k in ("scraped","after_filter","qualifying","STRONG","GOOD","MAYBE","STRETCH","jd_pct","sal_pct","distinct_companies","qualifying_with_salary","qualifying_with_summary"):
            pv, lv = ps.get(k), ls.get(k)
            try:
                delta = (lv or 0) - (pv or 0)
                delta_s = f"{delta:+}" if delta else ""
            except Exception:
                delta_s = ""
            print(f"{k:<20} {fmt(pv):>12} {fmt(lv):>12} {delta_s:>8}")

        print(f"\nIndustries (after):")
        for ind, n in sorted(ls['industries'].items(), key=lambda x: -x[1])[:8]:
            print(f"  {n:>3}  {ind}")

        gap_now = ls.get("gap") or {}
        if gap_now:
            print(f"\nCoverage gap analysis (after):")
            print(f"  severity:        {gap_now.get('gap_severity')}")
            print(f"  target_match_pct:{gap_now.get('target_match_pct')}")

        print(f"\nDIFF:")
        print(f"  new roles:     {d['new_count']}")
        print(f"  disappeared:   {d['gone_count']}")
        print(f"  score changes: {d['score_change_count']}")

        if d["new_top"]:
            print(f"\n  Top new roles:")
            for score, comp, title in d["new_top"]:
                print(f"    {score:>3}  {(comp or '')[:25]:25}  {(title or '')[:50]}")
        if d["score_changes"]:
            print(f"\n  Top score changes:")
            for comp, title, p, l in d["score_changes"][:5]:
                print(f"    {p:>3}->{l:>3}  {(comp or '')[:25]:25}  {(title or '')[:45]}")


if __name__ == "__main__":
    main()
