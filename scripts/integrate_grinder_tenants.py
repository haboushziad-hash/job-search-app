"""Integrate validated tenants from grinder runs into the main Workday
scraper directory.

Reads grinder output JSONs, filters to high-signal tenants (>= 50 active
roles), and writes each as backend/scraper/workday_tenants/<id>.json
matching the existing schema (merck.json format).

Curation rules:
  - Active role count >= 50 (filters out tiny/closed tenants)
  - Tenant ID isn't a duplicate of existing tenants
  - Display name applies known overrides for opaque IDs (ngc → Northrop Grumman)

Usage:
  python scripts/integrate_grinder_tenants.py \
      --input scripts/tenant_grinder_results_dmv.json \
      --min-roles 50 \
      --dry-run

Then drop --dry-run to actually write the files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKDAY_TENANTS_DIR = REPO_ROOT / "backend" / "scraper" / "workday_tenants"

# Known display-name overrides for opaque tenant IDs.
# Sourced from corporate research (these are public Workday tenants for
# household-name companies; ID is shorthand the company chose for Workday).
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
    "wwwinc": "Worthington Industries",
    "moog": "Moog Inc",
    "mymvw": "Marriott Vacations Worldwide",
    "geico": "GEICO",
    "warnerbros": "Warner Bros Discovery",
    "fanniemae": "Fannie Mae",
    "freddiemac": "Freddie Mac",
    "intel": "Intel",
    "cisco": "Cisco",
    "rand": "RAND Corporation",
    "washpost": "The Washington Post",
    "atlanticmedia": "The Atlantic",
    "mwaa": "Metropolitan Washington Airports Authority",
    "erickson": "Erickson Senior Living",
    "tapestry": "Tapestry (Coach/Kate Spade/Stuart Weitzman)",
    "wawa": "Wawa",
    "carmax": "CarMax",
    "shm": "Sentara Healthcare",
    "exactcare": "ExactCare Pharmacy",
    "bmc": "Boston Medical Center",
    "lvhn": "Lehigh Valley Health Network",
    "highmarkhealth": "Highmark Health",
    "sluhn": "St Luke's University Health Network",
    "carilionclinic": "Carilion Clinic",
    "wellstar": "Wellstar Health",
    "ummh": "UMass Memorial Health",
    "memorialhermann": "Memorial Hermann Health",
    "elevancehealth": "Elevance Health",
    "trinityhealth": "Trinity Health",
    "massgeneralbrigham": "Mass General Brigham",
    "wvumedicine": "WVU Medicine",
    "jeffersonhealth": "Jefferson Health",
    "guidehouse": "Guidehouse",
    "medline": "Medline Industries",
    "choa": "Children's Healthcare of Atlanta",
    "virtua": "Virtua Health",
    "capitalhealth": "Capital Health",
    "asmglobal": "ASM Global",
    "dickssportinggoods": "Dick's Sporting Goods",
    "cw": "Coast Wholesale",
    "redrobin": "Red Robin",
    "flextronics": "Flex (Flextronics)",
    "bjswholesaleclub": "BJ's Wholesale Club",
    "signetjewelers": "Signet Jewelers",
    "fifththird": "Fifth Third Bank",
    "bdx": "Becton Dickinson",
    "snc": "Sierra Nevada Corporation",
    "avav": "AeroVironment",
    "ultra": "Ultra Electronics",
    "townepark": "Towne Park",
    "premierinc": "Premier Inc",
    "calistacorp": "Calista Corporation",
    "hcmportal": "Workday HCM Portal (multi-tenant)",
    "skookum": "Skookum",
    "outrigger": "Outrigger Hotels",
    "rwlasvegas": "Resorts World Las Vegas",
    "shawinc": "Shaw Industries",
    "sierraspace": "Sierra Space",
    "ngs": "NGS",
    "groundswell": "Groundswell",
    "marymount": "Marymount University",
    "tsc": "Tractor Supply Company",
    "saif": "SAIF Corporation",
    "seic": "SEI Investments",
    "oregon": "State of Oregon",
    "aero": "Aerospace Corporation",
    "pg": "Procter & Gamble",
    "calwatergroup": "California Water Service Group",
    "cartech": "Carpenter Technology",
    "performant": "Performant Financial",
    "ntst": "Netsmart Technologies",
    "mosaic": "Mosaic Co",
    "acxiomllc": "Acxiom",
    "gaig": "Great American Insurance Group",
    "orix": "ORIX USA",
    "kmkp": "Kentucky Machine & Welding",
    "tel": "TE Connectivity",
    "nghs": "Northeast Georgia Health System",
    "phoebehealth": "Phoebe Putney Health System",
    "ambgroup": "Arthur M. Blank Family of Businesses",
    "wmeimg": "WME-IMG",
    "reitmr": "REIT (Mid-Atlantic Realty)",
    "calix": "Calix Inc",
    "web": "Web.com",
    "hhc": "Hartford HealthCare",
    "crhc": "Cooper-Riverside Healthcare Cooperative",
    "biospace": "BioSpace",
    "patientfirst": "Patient First",
    "sasllc": "SAS Institute LLC",
    "pinerest": "Pine Rest Christian Mental Health Services",
    "gohealthuc": "GoHealth Urgent Care",
    "axosbank": "Axos Bank",
    "cresta": "Cresta",
    "sharecare": "Sharecare",
    "caresource": "CareSource",
    "hollandhospital": "Holland Hospital",
    "bcbsa": "BCBS Association",
    "bcbsnj": "Horizon BCBS NJ",
    "solutionhealth": "SolutionHealth",
    "usacs": "US Acute Care Solutions",
    "choicehotels": "Choice Hotels",
    "bmo": "BMO Bank",
    "myhrabc": "ABC HR Portal",
    "path": "Pathstone (multi-tenant)",
    "gen": "Gen Digital (NortonLifeLock)",
    "martin": "Martin Health (Cleveland Clinic)",
}


def derive_display_name(tenant_id: str, fallback: str) -> str:
    if tenant_id in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[tenant_id]
    return fallback


def build_tenant_record(grinder_record: dict) -> dict:
    """Convert a grinder ValidatedTenant dict into a Workday-tenant JSON
    matching the existing scraper schema (merck.json format)."""
    tenant_id = grinder_record["tenant_id"]
    display_name = derive_display_name(
        tenant_id, grinder_record.get("display_name", tenant_id.title()),
    )
    return {
        "display_name": display_name,
        "careers_url": grinder_record["careers_url"],
        "status": "OK",
        "reason": "",
        "elapsed": grinder_record.get("elapsed_seconds", 0.0),
        "captured": {
            "url": grinder_record["cxs_url"],
            "method": grinder_record["cxs_method"],
            "request_body": grinder_record["cxs_request_body"],
            "headers": grinder_record["cxs_headers"],
            "input_selector_used": "",  # not captured by grinder
        },
        "_grinder_meta": {
            "discovered_at": grinder_record.get("discovered_at"),
            "industry_hint": grinder_record.get("industry_hint"),
            "active_role_count_at_discovery": grinder_record.get("active_role_count"),
            "discovered_via": grinder_record.get("discovered_via"),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path,
                   help="Grinder results JSON")
    p.add_argument("--min-roles", type=int, default=50,
                   help="Minimum active_role_count to include (default 50)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be written without writing")
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found")
        return

    with open(args.input, encoding="utf-8") as f:
        grinder_data = json.load(f)

    workday_tenants = grinder_data.get("workday_new", [])
    print(f"Grinder found: {len(workday_tenants)} Workday tenants")

    # Existing tenants — skip duplicates
    existing = set()
    if WORKDAY_TENANTS_DIR.exists():
        for f in WORKDAY_TENANTS_DIR.glob("*.json"):
            existing.add(f.stem.lower())
    print(f"Existing tenants: {len(existing)}")

    # Filter
    eligible = [
        t for t in workday_tenants
        if (t.get("active_role_count") or 0) >= args.min_roles
        and t["tenant_id"].lower() not in existing
    ]
    print(f"Eligible (>= {args.min_roles} roles, not duplicate): {len(eligible)}")

    # Sort by role count descending so the dashboard shows biggest finds first
    eligible.sort(key=lambda t: -(t.get("active_role_count") or 0))

    if args.dry_run:
        print("\n--- DRY RUN: would write these files ---")
        for t in eligible[:30]:
            print(f"  {t['active_role_count']:>5}  {t['tenant_id']:25}  → {derive_display_name(t['tenant_id'], t.get('display_name',''))}")
        if len(eligible) > 30:
            print(f"  ... and {len(eligible) - 30} more")
        print(f"\nTotal: {len(eligible)} tenant files would be created")
        return

    # Write
    WORKDAY_TENANTS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for t in eligible:
        record = build_tenant_record(t)
        out_path = WORKDAY_TENANTS_DIR / f"{t['tenant_id']}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        written += 1

    print(f"\nWrote {written} tenant files to {WORKDAY_TENANTS_DIR}")


if __name__ == "__main__":
    main()
