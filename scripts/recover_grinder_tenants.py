"""Recover validated tenants from a grinder bash log when the grinder
exited before writing its final JSON output.

Use case: the 30-min grinder run on 2026-05-07 hit its wall-clock
deadline mid-keyword and was killed before _write_results() ran. The
244 validated tenants from the run live only in the bash output log
as `[OK] Workday: <id> (<jobs>) [<industry>]` lines. Recovery here
re-validates each tenant via HTTP probe (same logic the grinder uses)
and writes proper JSON tenant records.

Usage:
  python scripts/recover_grinder_tenants.py --log <path> --min-roles 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKDAY_TENANTS_DIR = REPO_ROOT / "backend" / "scraper" / "workday_tenants"

LOG_LINE_RE = re.compile(
    r"\[OK\] Workday: ([a-z0-9_-]+) \((\d+) jobs\) \[([^\]]+)\]",
    re.IGNORECASE,
)


# Same display name overrides as integrate_grinder_tenants.py
DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "ngc": "Northrop Grumman",
    "bah": "Booz Allen Hamilton",
    "capitalone": "Capital One",
    "leidos": "Leidos",
    "kbr": "KBR",
    "petco": "Petco",
    "ms": "MicroStrategy",
    "vca": "VCA Animal Hospitals",
    "geaerospace": "GE Aerospace",
    "mars": "Mars Inc",
    "takeda": "Takeda Pharmaceuticals",
    "regeneron": "Regeneron Pharmaceuticals",
    "catalent": "Catalent Pharma Solutions",
    "medtronic": "Medtronic",
    "danaher": "Danaher Corporation",
    "rollsroyce": "Rolls-Royce",
    "bilh": "Beth Israel Lahey Health",
    "jll": "Jones Lang LaSalle",
    "drivenbrands": "Driven Brands",
    "maurices": "Maurices",
    "slihrms": "Sutherland Healthcare HRMS",
    "pfchangs": "P.F. Chang's",
    "gfs": "Gordon Food Service",
    "wk": "Wolters Kluwer",
    "hdsupply": "HD Supply",
    "biotechne": "Bio-Techne Corporation",
    "biibhr": "Biogen HR",
    "askbio": "AskBio (Asklepios BioPharmaceutical)",
    "biocryst": "BioCryst Pharmaceuticals",
    "javararesearch": "Javara Research",
    "agiliti": "Agiliti Health",
    "orthofix": "Orthofix Medical",
    "sumitomopharma": "Sumitomo Pharma",
    "crinetics": "Crinetics Pharmaceuticals",
    "inizio": "Inizio Group",
    "primetherapeutics": "Prime Therapeutics",
    "premierresearch": "Premier Research",
    "worldwide": "Worldwide Clinical Trials",
    "integer": "Integer Holdings",
    "memorialhermann": "Memorial Hermann Health",
    "waynefarms": "Wayne Farms",
    "veradigm": "Veradigm",
    "vizient": "Vizient",
    "veritone": "Veritone",
    "virtus": "Virtus Investment Partners",
    "ntst": "Netsmart Technologies",
    "sailpoint": "SailPoint",
    "odfl": "Old Dominion Freight Line",
    "smucker": "J.M. Smucker Company",
    "williams": "Williams Companies",
    "mpc": "Marathon Petroleum Corporation",
    "nwis": "Northwest IS",
    "fanniemae": "Fannie Mae",
    "freddiemac": "Freddie Mac",
    "intel": "Intel",
    "memorialhermann": "Memorial Hermann Health",
    "townepark": "Towne Park",
    "reitmr": "REIT MR",
    "rwlasvegas": "Resorts World Las Vegas",
    "outrigger": "Outrigger Hotels",
    "bdx": "Becton Dickinson",
    "mymvw": "Marriott Vacations Worldwide",
    "wmeimg": "WME-IMG",
    "cw": "Camping World",
    "web": "Web.com",
    "orix": "ORIX USA",
    "cartech": "Carpenter Technology",
    "hcmportal": "Workday HCM Portal",
}


def derive_display_name(tenant_id: str) -> str:
    if tenant_id in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[tenant_id]
    return tenant_id.replace("-", " ").replace("_", " ").title()


async def validate_workday_tenant(tenant: str) -> dict | None:
    """Same logic as the grinder's validate_workday_tenant — try common
    job_site path variations. Returns the captured config or None."""
    paths_to_try = [
        "External", "Careers", "SearchJobs", "Jobs", "careers", "external",
        "ExternalSite", "External_Career", "us-careers", "global-careers",
    ]
    request_body = '{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # Try common Workday subdomains
    subdomains = ["wd1", "wd2", "wd3", "wd5", "wd103", "wd106"]

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for sub in subdomains:
            host = f"{tenant}.{sub}.myworkdayjobs.com"
            for path in paths_to_try:
                cxs_url = f"https://{host}/wday/cxs/{tenant}/{path}/jobs"
                try:
                    r = await client.post(cxs_url, content=request_body, headers=headers)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                jobs = data.get("jobPostings") or data.get("jobs") or []
                total = data.get("total") or len(jobs)
                if not jobs and total == 0:
                    continue
                return {
                    "host": host,
                    "path": path,
                    "cxs_url": cxs_url,
                    "request_body": request_body,
                    "headers": headers,
                    "active_role_count": int(total),
                }
    return None


def build_tenant_record(tenant_id: str, validation: dict, industry_hint: str) -> dict:
    return {
        "display_name": derive_display_name(tenant_id),
        "careers_url": f"https://{validation['host']}/{validation['path']}",
        "status": "OK",
        "reason": "",
        "elapsed": 0.0,
        "captured": {
            "url": validation["cxs_url"],
            "method": "POST",
            "request_body": validation["request_body"],
            "headers": validation["headers"],
            "input_selector_used": "",
        },
        "_grinder_meta": {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "industry_hint": industry_hint,
            "active_role_count_at_discovery": validation["active_role_count"],
            "discovered_via": "log-recovery",
        },
    }


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--min-roles", type=int, default=50)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.log.exists():
        print(f"Log not found: {args.log}")
        return

    # Parse log
    log_text = args.log.read_text(encoding="utf-8", errors="ignore")
    matches = LOG_LINE_RE.findall(log_text)
    unique = {m[0].lower(): (int(m[1]), m[2]) for m in matches}
    print(f"Parsed {len(unique)} unique Workday tenants from log")

    # Existing tenants
    existing = set()
    if WORKDAY_TENANTS_DIR.exists():
        for f in WORKDAY_TENANTS_DIR.glob("*.json"):
            existing.add(f.stem.lower())
    print(f"Already integrated: {len(existing)}")

    # NEW = in log but not yet integrated
    new_candidates = [
        (tid, jobs, ind)
        for tid, (jobs, ind) in unique.items()
        if tid not in existing and jobs >= args.min_roles
    ]
    print(f"NEW tenants to recover (>= {args.min_roles} jobs): {len(new_candidates)}")
    new_candidates.sort(key=lambda x: -x[1])  # by jobs desc

    if args.dry_run:
        for tid, jobs, ind in new_candidates[:30]:
            print(f"  {jobs:>5}  {tid:25}  → {derive_display_name(tid)}")
        if len(new_candidates) > 30:
            print(f"  ... and {len(new_candidates) - 30} more")
        return

    print()
    print("Re-validating each tenant via HTTP probe (this will take ~3-5 min)...")

    sem = asyncio.Semaphore(10)
    results: dict[str, tuple[dict, int, str]] = {}
    failed: list[str] = []

    async def validate_one(tid: str, jobs: int, ind: str):
        async with sem:
            v = await validate_workday_tenant(tid)
            if v:
                results[tid] = (v, jobs, ind)
                print(f"  [OK] {tid:25} {v['active_role_count']:>5} jobs")
            else:
                failed.append(tid)
                print(f"  [FAIL] {tid:25} (could not re-validate)")

    await asyncio.gather(*[validate_one(tid, jobs, ind) for tid, jobs, ind in new_candidates])

    # Write JSON files
    WORKDAY_TENANTS_DIR.mkdir(parents=True, exist_ok=True)
    for tid, (validation, jobs, ind) in results.items():
        record = build_tenant_record(tid, validation, ind)
        out = WORKDAY_TENANTS_DIR / f"{tid}.json"
        out.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print()
    print(f"Re-validated + wrote: {len(results)}")
    print(f"Failed re-validation: {len(failed)} (likely changed URL or went down between log time and now)")
    if failed:
        print(f"  Failed: {failed[:20]}{'...' if len(failed) > 20 else ''}")
    print()
    print(f"Total Workday tenants now: {len(list(WORKDAY_TENANTS_DIR.glob('*.json')))}")


if __name__ == "__main__":
    asyncio.run(main())
