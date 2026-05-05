"""Regenerate the desktop_app/src/data/us-cities.ts mirror from
backend/filter/geocoding.py CITIES dict.

Run this whenever you add/edit cities in geocoding.py to keep the
frontend autocomplete in sync. The frontend list is purely a UX
aid — backend has the actual lat/lng coordinates the radius filter
uses, but the autocomplete should suggest the same set of cities
the backend can geocode.

Usage:
    python scripts/regen_city_lists.py

If you also want to expand the backend list with NEW cities from
geonamescache (e.g., to lower the population threshold), see the
inline EXPAND_FROM_GEONAMES block below — comment in to use.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Ensure we can import the backend package
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ----------------------------------------------------------------------------
# Optional: expand backend with new cities from geonamescache (>=N pop).
# Uncomment + set EXPAND_THRESHOLD to add more cities to backend dict.
# Requires: pip install geonamescache  (dev dep — not shipped in backend.exe)
# ----------------------------------------------------------------------------
EXPAND_FROM_GEONAMES = False  # Set True to expand backend
EXPAND_THRESHOLD = 25_000     # Minimum population

if EXPAND_FROM_GEONAMES:
    import geonamescache
    from backend.filter import geocoding as g

    EXISTING = dict(g.CITIES)

    def normalize_name(name: str) -> str:
        n = name.lower().replace("st.", "st").replace("saint ", "st ")
        n = n.replace(".", "")
        if n.endswith(" city") and (n[:-5], "ny") in EXISTING:
            n = n[:-5]
        return " ".join(n.split())

    gc = geonamescache.GeonamesCache()
    new = []
    for c in gc.get_cities().values():
        if c.get("countrycode") != "US":
            continue
        if c.get("population", 0) < EXPAND_THRESHOLD:
            continue
        key = (normalize_name(c["name"]), c["admin1code"].lower())
        if key in EXISTING:
            continue
        new.append((key, (c["latitude"], c["longitude"]), c["population"]))
    new.sort(key=lambda x: (x[0][1], -x[2]))
    print(f"Would add {len(new)} new cities to backend (threshold {EXPAND_THRESHOLD:,})")

    # Splice into geocoding.py just before the closing brace of CITIES dict
    geo_path = ROOT / "backend" / "filter" / "geocoding.py"
    src = geo_path.read_text(encoding="utf-8").splitlines(keepends=True)
    cities_start = next(i for i, l in enumerate(src) if "CITIES: dict[" in l)
    cities_end = next(i for i in range(cities_start, len(src)) if src[i].rstrip() == "}")
    additions = []
    additions.append("    # ============================================================\n")
    additions.append(f"    # Expansion ({len(new)} cities, geonamescache, >={EXPAND_THRESHOLD:,} pop).\n")
    additions.append("    # ============================================================\n")
    last_state = None
    for (city, state), (lat, lng), _ in new:
        if state != last_state:
            additions.append(f"    # {state.upper()}\n")
            last_state = state
        additions.append(f"    ({city!r:<30}, {state!r}): ({lat:.4f}, {lng:.4f}),\n")
    geo_path.write_text("".join(src[:cities_end] + additions + src[cities_end:]), encoding="utf-8")
    print(f"Backend expanded.")

# ----------------------------------------------------------------------------
# Always: regenerate frontend us-cities.ts from current backend dict
# ----------------------------------------------------------------------------
# Re-import in case we just expanded
import importlib
if "backend.filter.geocoding" in sys.modules:
    del sys.modules["backend.filter.geocoding"]
import backend.filter.geocoding as g

formatted = sorted({
    f"{' '.join(w.capitalize() for w in city.split())} {state.upper()}"
    for (city, state), _ in g.CITIES.items()
})

ts_path = ROOT / "desktop_app" / "src" / "data" / "us-cities.ts"
header = """/**
 * US cities autocomplete database.
 *
 * Auto-generated mirror of backend/filter/geocoding.py CITIES dict.
 * Single source of truth lives in the backend so autocomplete suggestions
 * exactly match the cities the radius filter knows how to geocode.
 *
 * Coverage (v0.2.0):
 *   - All US cities with population > 25,000 (~2,100 cities)
 *   - Comprehensive Virginia (test cohort lives there)
 *   - Common suburbs of top 20 US metros
 *   - All US state capitals
 *   - Plus "Remote" / "Remote (US)" sentinels
 *
 * Regenerate: python scripts/regen_city_lists.py (run from project root).
 */

export const US_CITIES: string[] = [
"""
# Use JSON-style double-quoted strings so cities with apostrophes
# (Coeur D'Alene, Town 'n' Country, etc.) emit valid TypeScript without
# breaking the surrounding quote context.
import json
body = "".join(f"  {json.dumps(c)},\n" for c in formatted)
footer = """  // Remote sentinels — accepted by the location filter as 'any remote role'
  'Remote',
  'Remote (US)',
]
"""
ts_path.write_text(header + body + footer, encoding="utf-8")
print(f"Wrote {len(formatted)} cities to {ts_path.relative_to(ROOT)}")
