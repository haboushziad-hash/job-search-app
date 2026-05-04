#!/usr/bin/env python3
"""Build a self-contained audit report from a run's audit JSON.

Output is a single Markdown file that another Claude (or any auditor) can
read end-to-end to verify scoring quality, hard-filter behaviour, scraper
funnel health, and per-tier role placements.

Usage:
    python scripts/build_audit_report.py <audit.json> [<output.md>]

If output path is omitted, the report is written next to the audit JSON
with `_audit_report.md` suffix.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JD_SNIPPET_CHARS = 2000   # how much JD body each role gets in the report
REASONING_CHAR_CAP = 4000  # safety cap on each reasoning blob


def _trim(s: Any, cap: int) -> str:
    if not s:
        return "—"
    text = str(s).replace("\r\n", "\n").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + f" …[truncated; full was {len(text)} chars]"


def _money(role: dict) -> str:
    if role.get("salary_text"):
        return str(role["salary_text"])
    smin = role.get("salary_min")
    smax = role.get("salary_max")
    if smin and smax:
        return f"${smin:,} – ${smax:,}"
    if smin:
        return f"${smin:,}+"
    return "—"


def _fmt_pct(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def render_run_metadata(meta: dict) -> str:
    rows = [
        ("Run ID", meta.get("run_id")),
        ("Date", meta.get("started_at") or meta.get("date")),
        ("Duration (sec)", meta.get("duration_seconds")),
        ("Cost (USD)", meta.get("total_cost_usd") or meta.get("cost_usd")),
        ("App version", meta.get("app_version")),
        ("Cache hit", meta.get("cache_hit")),
    ]
    rows = [(k, v) for k, v in rows if v is not None]
    body = "| Field | Value |\n|---|---|\n"
    for k, v in rows:
        body += f"| {k} | `{v}` |\n"
    return body


def render_profile(profile: dict) -> str:
    if not profile:
        return "_(missing)_"
    lines = []
    lines.append(f"- **Headline:** {profile.get('headline', '—')}")
    lines.append(f"- **Years of experience:** {profile.get('years_of_experience', '—')}")
    lines.append(f"- **Seniority:** {profile.get('seniority', '—')}")

    target_funcs = profile.get("target_functions") or []
    if target_funcs:
        lines.append(f"- **Target functions:** {', '.join(target_funcs)}")

    target_inds = profile.get("target_industries") or []
    if target_inds:
        lines.append(f"- **Target industries:** {', '.join(target_inds)}")

    locs = profile.get("locations") or profile.get("preferred_locations") or []
    if locs:
        loc_strs = [
            f"{l.get('city','')}, {l.get('state','')}".strip(", ")
            if isinstance(l, dict) else str(l)
            for l in locs
        ]
        lines.append(f"- **Preferred locations:** {', '.join(loc_strs)}")

    lines.append(f"- **Salary minimum:** ${profile.get('salary_minimum', '—'):,}"
                 if isinstance(profile.get('salary_minimum'), int)
                 else f"- **Salary minimum:** {profile.get('salary_minimum', '—')}")

    arr = profile.get("acceptable_arrangements") or []
    if arr:
        lines.append(f"- **Acceptable arrangements:** {', '.join(arr)}")

    tags = profile.get("profile_tags") or []
    if tags:
        lines.append(f"- **Profile tags:** {', '.join(tags)}")

    sterms = profile.get("search_terms") or []
    if sterms:
        lines.append(f"\n**Generated search terms ({len(sterms)}):**")
        lines.append("")
        for t in sterms:
            lines.append(f"  - `{t}`")

    kws = profile.get("keywords") or []
    if kws:
        # Keywords might be {text, tier} dicts or plain strings
        lines.append(f"\n**Generated target-title keywords ({len(kws)}):**")
        lines.append("")
        for k in kws:
            if isinstance(k, dict):
                lines.append(f"  - `{k.get('text','')}` (tier {k.get('tier','?')})")
            else:
                lines.append(f"  - `{k}`")

    nogo = profile.get("nogo_signals") or []
    if nogo:
        lines.append(f"\n**No-go signals:** {', '.join(nogo)}")

    return "\n".join(lines)


def render_pipeline_funnel(funnel: dict) -> str:
    out = []
    out.append("### Overall funnel\n")
    out.append("| Stage | Count |")
    out.append("|---|---|")
    out.append(f"| Total scraped | {funnel.get('total_scraped', '—'):,}"
               if isinstance(funnel.get('total_scraped'), int)
               else f"| Total scraped | {funnel.get('total_scraped','—')} |")
    out.append(f"| After hard filters | {funnel.get('after_hard_filters', '—'):,}"
               if isinstance(funnel.get('after_hard_filters'), int)
               else f"| After hard filters | {funnel.get('after_hard_filters','—')} |")
    out.append(f"| Final qualifying | {funnel.get('qualifying_final', '—'):,}"
               if isinstance(funnel.get('qualifying_final'), int)
               else f"| Final qualifying | {funnel.get('qualifying_final','—')} |")

    tiers = funnel.get("tier_breakdown") or {}
    if tiers:
        out.append("\n### Tier breakdown\n")
        out.append("| Tier | Count |")
        out.append("|---|---|")
        for tier in ("STRONG", "GOOD", "MAYBE", "STRETCH"):
            out.append(f"| {tier} | {tiers.get(tier, 0)} |")

    cov = funnel.get("coverage") or {}
    if cov:
        out.append("\n### Coverage\n")
        out.append(f"- **JD coverage:** {_fmt_pct(cov.get('jd_coverage_pct'))}")
        out.append(f"- **Salary coverage:** {_fmt_pct(cov.get('salary_coverage_pct'))}")
        out.append(f"- **Location coverage:** {_fmt_pct(cov.get('location_coverage_pct'))}")

    per_src = funnel.get("per_source_funnel") or {}
    if per_src:
        out.append("\n### Per-source funnel\n")
        out.append("| Source | Raw | Dedup | Post-filter | Post-liveness | Qualifying |")
        out.append("|---|---|---|---|---|---|")
        for src, d in per_src.items():
            out.append(
                f"| {src} | "
                f"{d.get('raw_scraped','—')} | "
                f"{d.get('after_dedup','—')} | "
                f"{d.get('after_hard_filters','—')} | "
                f"{d.get('after_liveness_and_deadlist','—')} | "
                f"{d.get('qualifying_final','—')} |"
            )

    return "\n".join(out)


def render_per_source_health(health: dict) -> str:
    if not health:
        return "_(none reported)_"
    out = []
    out.append("| Source | Status | Quota? | Reason |")
    out.append("|---|---|---|---|")
    for src, d in health.items():
        if not isinstance(d, dict):
            continue
        out.append(
            f"| {src} | {d.get('status', '—')} | "
            f"{'⚠️ YES' if d.get('quota_exhausted') else 'no'} | "
            f"{d.get('quota_exhausted_reason') or d.get('error_message') or '—'} |"
        )
    return "\n".join(out)


def render_keyword_performance(keyword_eff: list, keyword_cov: list) -> str:
    if not keyword_eff and not keyword_cov:
        return "_(none reported)_"

    out = []
    out.append("### Keyword effectiveness (raw match counts)\n")
    out.append("| Keyword | Tier | Raw matches | Qualifying matches |")
    out.append("|---|---|---|---|")
    for k in keyword_eff or []:
        out.append(
            f"| `{k.get('keyword','')}` | "
            f"{k.get('tier','—')} | "
            f"{k.get('raw_matches', '—')} | "
            f"{k.get('qualifying_matches', '—')} |"
        )

    if keyword_cov:
        out.append("\n### Keyword coverage (which keyword found each role)\n")
        out.append("| Keyword | Tier | Roles surfaced |")
        out.append("|---|---|---|")
        for k in keyword_cov:
            out.append(
                f"| `{k.get('keyword','')}` | "
                f"{k.get('tier','—')} | "
                f"{k.get('roles_surfaced', '—')} |"
            )
    return "\n".join(out)


def render_role_dossier(role: dict, idx: int) -> str:
    """One detailed block per role — for tier sections."""
    out = []
    out.append(f"### {idx}. [{role.get('score','?')}] {role.get('company','—')} — {role.get('title','—')}")
    out.append("")
    out.append(f"- **URL:** {role.get('url','—')}")
    out.append(f"- **Source:** `{role.get('source','—')}`")
    out.append(f"- **Matched keyword:** `{role.get('matched_keyword','—')}`")
    out.append(f"- **Industry:** {role.get('industry','—')}")
    out.append(f"- **Location:** {role.get('location','—')} ({role.get('location_type','—')})")
    out.append(f"- **Salary:** {_money(role)}")
    out.append(f"- **Posted:** {role.get('posted_date','—')}")
    out.append(f"- **Embedding similarity:** "
               f"{role.get('embedding_similarity', '—'):.4f}"
               if isinstance(role.get('embedding_similarity'), (int, float))
               else f"- **Embedding similarity:** {role.get('embedding_similarity', '—')}")
    out.append(f"- **Stage 2 (triage Flash) score:** {role.get('stage2_score','—')}")
    out.append(f"- **Stage 3 (deep-eval Pro) score:** {role.get('stage3_score','—')}")
    out.append(f"- **Final score:** {role.get('score','—')} → **{role.get('tier','—')}**")

    if role.get("summary"):
        out.append(f"\n**Summary (synthesized):**\n\n> {_trim(role['summary'], REASONING_CHAR_CAP)}")
    if role.get("stage2_reasoning"):
        out.append(f"\n**Stage 2 reasoning:**\n\n> {_trim(role['stage2_reasoning'], REASONING_CHAR_CAP)}")
    if role.get("stage3_analysis"):
        out.append(f"\n**Stage 3 analysis:**\n\n> {_trim(role['stage3_analysis'], REASONING_CHAR_CAP)}")
    if role.get("stage3_application_strategy"):
        out.append(f"\n**Stage 3 application strategy:**\n\n> {_trim(role['stage3_application_strategy'], REASONING_CHAR_CAP)}")

    jd = role.get("full_job_description") or ""
    if jd:
        out.append(f"\n**Job description ({len(jd):,} chars total, first {JD_SNIPPET_CHARS:,}):**\n")
        out.append(f"```\n{_trim(jd, JD_SNIPPET_CHARS)}\n```")

    return "\n".join(out)


def render_qualifying_roles(roles: list) -> str:
    """Group qualifying roles by tier, sorted by score desc within each tier."""
    if not roles:
        return "_(no qualifying roles)_"

    by_tier = defaultdict(list)
    for r in roles:
        by_tier[r.get("tier", "UNKNOWN")].append(r)

    out = []
    out.append(f"_{len(roles)} total qualifying roles, broken down by tier._\n")

    # Quick at-a-glance index first
    out.append("### At-a-glance index\n")
    out.append("| # | Score | Tier | Source | Company | Title | Salary |")
    out.append("|---|---|---|---|---|---|---|")
    sorted_all = sorted(roles, key=lambda r: r.get("score", 0), reverse=True)
    for i, r in enumerate(sorted_all, 1):
        title_short = (r.get("title") or "—")[:60]
        out.append(
            f"| {i} | {r.get('score','—')} | {r.get('tier','—')} | "
            f"{r.get('source','—')} | "
            f"{(r.get('company') or '—')[:30]} | "
            f"{title_short} | {_money(r)} |"
        )

    # Then per-tier deep dossiers
    for tier in ("STRONG", "GOOD", "MAYBE", "STRETCH", "UNKNOWN"):
        roles_in_tier = sorted(by_tier.get(tier, []),
                               key=lambda r: r.get("score", 0), reverse=True)
        if not roles_in_tier:
            continue
        out.append(f"\n---\n\n## Tier: {tier} ({len(roles_in_tier)} roles)\n")
        for i, r in enumerate(roles_in_tier, 1):
            out.append(render_role_dossier(r, i))
            out.append("\n---\n")

    return "\n".join(out)


def render_near_misses(near_misses: list) -> str:
    if not near_misses:
        return "_(none reported — every viable role made it through)_"
    out = []
    out.append(f"_{len(near_misses)} roles that almost qualified — useful for "
               "spotting false negatives in scoring or hard-filter cuts._\n")
    for i, r in enumerate(near_misses, 1):
        out.append(render_role_dossier(r, i))
        out.append("\n---\n")
    return "\n".join(out)


def render_company_distribution(dist: list) -> str:
    if not dist:
        return "_(none)_"
    out = ["| Company | Total roles | Qualifying | Top score |",
           "|---|---|---|---|"]
    for d in dist:
        out.append(
            f"| {d.get('company','—')} | "
            f"{d.get('total_roles', '—')} | "
            f"{d.get('qualifying_roles', '—')} | "
            f"{d.get('top_score', '—')} |"
        )
    return "\n".join(out)


def render_coverage_gap(gap: dict) -> str:
    if not gap:
        return "_(none)_"
    out = []
    out.append(f"- **Severity:** {gap.get('severity', '—')}")
    out.append(f"- **Target match %:** {gap.get('target_match_pct', '—')}")
    out.append(f"- **Tester message:** {gap.get('tester_message', '—')}")
    op = gap.get("operator_note") or ""
    if op:
        out.append(f"\n**Operator note:** {op}")
    return "\n".join(out)


def render_comparison(cmp: dict) -> str:
    if not cmp:
        return "_(no prior run)_"
    out = []
    out.append(f"- **New roles:** {cmp.get('new_roles', '—')}")
    out.append(f"- **Disappeared:** {cmp.get('disappeared_roles', '—')}")
    out.append(f"- **Score changes:** {cmp.get('score_changes', '—')}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

AUDIT_PREAMBLE = """\
# Job Search Run — Full Audit Report

**Purpose.** This file is a self-contained audit dossier for one search
run. It packages every piece of context an auditing reviewer (human or
LLM) would need to sanity-check the pipeline:

1. The candidate profile the search ran against
2. The full pipeline funnel (raw scrape → hard filters → embedding →
   Stage 1 anti-pattern → Stage 2 triage Flash → Stage 3 deep-eval Pro)
3. Per-source health (was any scraper rate-limited or quota-exhausted?)
4. Keyword effectiveness (which generated keywords surfaced which roles)
5. **Every qualifying role**, grouped by tier, with full reasoning
   blobs (Stage 2 + Stage 3 + application strategy + JD snippet).
6. Near-miss roles — what almost qualified and didn't.
7. Company distribution and coverage gap analysis.
8. Comparison to the prior run (if one existed).

**How to use this file.** Audit the role placements per-tier. Spot-check
the Stage 3 reasoning against the JD snippet. Verify the matched
keyword makes sense for the role. Watch for false positives in STRONG
(too-good-to-be-true scores) and false negatives in near-misses
(roles that should have qualified).

---
"""


def build_report(audit_json_path: Path, output_path: Path) -> None:
    with audit_json_path.open("r", encoding="utf-8") as f:
        a = json.load(f)

    parts = [AUDIT_PREAMBLE]

    parts.append(_section("Run metadata", render_run_metadata(a.get("run_metadata", {}))))
    parts.append(_section("Profile snapshot", render_profile(a.get("profile_snapshot", {}))))
    parts.append(_section("Pipeline funnel", render_pipeline_funnel(a.get("pipeline_funnel", {}))))
    parts.append(_section("Per-source health", render_per_source_health(a.get("per_source_health", {}))))
    parts.append(_section("Keyword performance",
                          render_keyword_performance(
                              a.get("keyword_effectiveness", []),
                              a.get("keyword_coverage", []))))
    parts.append(_section("Coverage gap analysis", render_coverage_gap(a.get("coverage_gap_analysis", {}))))
    parts.append(_section("Comparison to prior run", render_comparison(a.get("comparison_to_prior_run", {}))))
    parts.append(_section("Company distribution (top 64)",
                          render_company_distribution(a.get("company_distribution", []))))
    parts.append(_section("Near-miss roles (almost qualified)",
                          render_near_misses(a.get("near_miss_roles_for_audit", []))))
    parts.append(_section("Qualifying roles — full dossiers by tier",
                          render_qualifying_roles(a.get("all_qualifying_roles", []))))

    output_path.write_text("\n".join(parts), encoding="utf-8")

    size_kb = output_path.stat().st_size / 1024
    print(f"Wrote audit report -> {output_path}")
    print(f"Size: {size_kb:.1f} KB")
    print(f"Roles audited: {len(a.get('all_qualifying_roles', []))}")
    print(f"Near-misses: {len(a.get('near_miss_roles_for_audit', []))}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    in_path = Path(sys.argv[1]).expanduser().resolve()
    if not in_path.exists():
        print(f"Audit JSON not found: {in_path}")
        return 1

    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2]).expanduser().resolve()
    else:
        stem = in_path.stem.replace("_audit", "")
        out_path = in_path.with_name(f"{stem}_audit_report.md")

    build_report(in_path, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
