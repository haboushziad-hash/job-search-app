"""Probe a large candidate list of Greenhouse slugs to find new working ones.

Targets sectors currently under-represented in our coverage:
  - Healthcare / biotech / pharma
  - Climate / clean energy / sustainability
  - Manufacturing / industrial
  - Real estate / proptech
  - Consumer / retail / CPG
  - Government / civic / nonprofit
  - Defense / aerospace
  - Education / edtech
  - Logistics / supply chain
  - Energy / utilities
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from backend.scraper.greenhouse import GREENHOUSE_COMPANIES


# ============================================================================
# Candidate slugs across underserved sectors
# ============================================================================

CANDIDATES = [
    # === Healthcare / biotech / pharma ===
    ("Forge Biologics", "forgebiologics"),
    ("Insitro", "insitro"),
    ("Quanterix", "quanterix"),
    ("Tessera Therapeutics", "tessera"),
    ("Beam Therapeutics", "beamtherapeutics"),
    ("Editas Medicine", "editasmedicine"),
    ("Sana Biotechnology", "sanabiotechnology"),
    ("Verily", "verily"),
    ("Function Health", "functionhealth"),
    ("Ro Health", "rohealth"),
    ("Lyra Health", "lyrahealth"),
    ("Modern Health", "modernhealth"),
    ("Spring Health", "springhealth"),
    ("Headspace Health", "headspacehealth"),
    ("Eko Health", "ekohealth"),
    ("Suki", "sukiai"),
    ("Notable Health", "notablehealth"),
    ("Iterative Health", "iterativehealth"),
    ("Twin Health", "twinhealth"),
    ("Hone Health", "honehealth"),
    ("Strive Health", "strivehealth"),
    ("Nuna Health", "nunahealth"),
    ("Zus Health", "zushealth"),
    ("Medable", "medable"),
    ("Wellsky", "wellsky"),

    # === Climate / clean energy / sustainability ===
    ("Form Energy", "formenergy"),
    ("Helion Energy", "helionenergy"),
    ("Commonwealth Fusion", "commonwealthfusionsystems"),
    ("Sila", "silanano"),
    ("ESS", "esstech"),
    ("Stem", "stem"),
    ("Octopus Energy", "octopusenergy"),
    ("ZeroAvia", "zeroavia"),
    ("Beyond Meat", "beyondmeat"),
    ("Impossible Foods", "impossiblefoods"),
    ("Apeel Sciences", "apeelsciences"),
    ("Plenty", "plenty"),
    ("PivotBio", "pivotbio"),
    ("Indigo Ag", "indigoag"),
    ("Pachama", "pachama"),
    ("Charm Industrial", "charmindustrial"),
    ("Climeworks", "climeworks"),
    ("Aclima", "aclima"),
    ("Remora", "remoracarbon"),
    ("Heirloom", "heirloom"),
    ("Living Carbon", "livingcarbon"),
    ("Twelve", "twelve"),
    ("Fervo Energy", "fervoenergy"),

    # === Manufacturing / industrial ===
    ("Shield AI", "shieldai"),
    ("Anduril", "andurilindustries"),
    ("Skydio", "skydio"),
    ("Saildrone", "saildrone"),
    ("Starfish Space", "starfishspace"),
    ("Astranis", "astranis"),
    ("Stoke Space", "stokespace"),
    ("Hadrian", "hadrian"),
    ("KoBold Metals", "koboldmetals"),
    ("Pivotal Commware", "pivotalcommware"),

    # === Real estate / proptech ===
    ("VTS", "vts"),
    ("Redfin", "redfin"),
    ("Pacaso", "pacaso"),
    ("Procore Tech", "procoretech"),
    ("Rent the Runway", "renttherunway"),

    # === Consumer / retail / CPG ===
    ("Chewy", "chewy"),
    ("Wayfair", "wayfair"),
    ("Stitch Fix", "stitchfix"),
    ("Allbirds", "allbirdsinc"),
    ("Casper", "caspersleep"),
    ("Warby Parker", "warbyparker"),
    ("Glossier Cosmetics", "glossiercosmetics"),
    ("Outdoor Voices", "outdoorvoices"),
    ("Ritual Vitamins", "ritualvitamins"),
    ("Kettle & Fire", "kettleandfire"),
    ("Magic Spoon", "magicspoon"),
    ("Athletic Greens", "athleticgreensag1"),
    ("Hims Hers", "himsandhers"),

    # === Government / civic / nonprofit ===
    ("Thorn", "thorn"),
    ("Code for America", "codeforamerica"),
    ("Charity: Water", "charitywater"),
    ("Khan Academy", "khanacademy-jobs"),
    ("New York Public Library", "nypl"),
    ("Wikimedia", "wikimediafoundation"),
    ("Mozilla", "mozilla"),
    ("Internet Archive", "internetarchive"),
    ("ACLU", "aclu"),
    ("RAND Corp", "randcorporation"),
    ("PNC Bank", "pncbank"),

    # === Defense / aerospace ===
    ("Palantir", "palantir"),
    ("Booz Allen Hamilton", "boozallen"),
    ("Anduril Industries", "anduril"),
    ("Kratos Defense", "kratosdefense"),
    ("Leidos Innovations", "leidosinnovations"),

    # === Education / edtech ===
    ("Coursera", "coursera"),
    ("edX", "edx"),
    ("Pluralsight", "pluralsight"),
    ("Outschool", "outschool"),
    ("Quizlet", "quizlet"),
    ("Brainly", "brainly"),
    ("Newsela", "newsela"),
    ("Kahoot", "kahoot"),
    ("Photomath", "photomath"),

    # === Logistics / supply chain ===
    ("Flexport", "flexport"),
    ("Convoy", "convoyinc"),
    ("project44", "project44"),
    ("FourKites", "fourkites"),
    ("Loop Returns", "loopreturns"),
    ("Loop Inc", "loop"),
    ("Stord", "stord"),
    ("ShipBob", "shipbob"),

    # === Energy / utilities ===
    ("Sunrun", "sunrun"),
    ("Tesla", "tesla"),
    ("Rivian", "rivianautomotive"),
    ("Lucid Motors", "lucid"),
    ("AECOM", "aecom"),
    ("Siemens Energy", "siemensenergy"),
    ("First Solar", "firstsolar"),
    ("EVgo", "evgo"),
    ("ChargePoint", "chargepoint"),
    ("Enphase", "enphase"),

    # === Tech expansion ===
    ("Stripe Press", "stripepress"),
    ("Replit", "replithq"),
    ("Bolt", "bolt"),
    ("Nylas", "nylas"),
    ("Webflow Inc", "webflowinc"),
    ("Hightouch", "hightouch"),
    ("Census", "getcensus"),
    ("Fivetran", "fivetran"),
    ("Hex Tech", "hextech"),
    ("Mode", "modeanalytics"),
    ("Hightouch", "hightouchio"),
    ("Maven", "mavenagi"),

    # === FinTech expansion ===
    ("Cross River Bank", "crossriverbank"),
    ("Alloy", "alloy"),
    ("Unit", "unit"),
    ("Modern Treasury Inc", "moderntreasuryinc"),
    ("Bond Financial", "bondfinancial"),
    ("Pile", "pile"),
    ("Fundera", "fundera"),
    ("LendingClub", "lendingclub"),
    ("Earnin", "earnin"),
    ("Tally", "tally"),
    ("Albert", "albert"),
    ("Acorns", "acorns"),

    # === Insurance / Insuretech ===
    ("Lemonade Insurance", "lemonadeinsurance"),
    ("Hippo Insurance", "hippoinsurance"),
    ("Newfront", "newfront"),
    ("Coalition", "coalitioninc"),
    ("Vouch", "vouch"),

    # === Mid-market companies ===
    ("Rappi", "rappi"),
    ("Fanatics", "fanatics"),
    ("Postmates", "postmates"),
    ("Patreon", "patreonhq"),
    ("Upwork", "upwork"),
    ("Cameo", "cameo"),
    ("OnDeck Capital", "ondeck"),
]


async def main():
    sem = asyncio.Semaphore(15)
    existing = {t for _, t in GREENHOUSE_COMPANIES}
    new_only = [(n, t) for n, t in CANDIDATES if t not in existing]
    print(f"Probing {len(new_only)} NEW candidate slugs (skipping {len(CANDIDATES) - len(new_only)} already in list)...")

    async with httpx.AsyncClient(timeout=10) as c:
        async def probe(name, token):
            async with sem:
                try:
                    r = await c.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
                    if r.status_code == 200:
                        n = len(r.json().get("jobs", []))
                        if n > 0:
                            return name, token, n
                except Exception:
                    pass
                return None

        results = await asyncio.gather(*[probe(n, t) for n, t in new_only])

    live = sorted([r for r in results if r], key=lambda x: -x[2])
    print(f"\nNEW WORKING: {len(live)} of {len(new_only)} probed")
    print(f"Total new jobs: {sum(c for _, _, c in live)}")
    print()
    for n, t, c in live:
        print(f'    ("{n}", "{t}"),   # {c} jobs')


if __name__ == "__main__":
    asyncio.run(main())
