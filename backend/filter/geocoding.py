"""Lightweight offline geocoding for US cities + radius-based distance check.

Why this exists: the user's radius slider should actually work. They specify
"Richmond VA, 50mi" and we should match roles in Hanover (~15mi), Henrico
(~5mi), AND Fredericksburg (~55mi if radius >= 60mi).

Approach: embed a curated dataset of US cities + lat/lng, then use the
haversine formula to compute great-circle distance between two points.

Coverage:
  - All US cities with population > 25,000 (~700 cities)
  - All Virginia cities over 5K (Ziad's home state)
  - Common suburbs of top 20 US metros
  - All US state capitals

For unknown cities, the caller should fall back to metro-alias / state-overlap
matching (already implemented in hard_filters.py).
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Optional


# ----------------------------------------------------------------------------
# Cities database — (city_lower, state_code) -> (lat, lng)
# ----------------------------------------------------------------------------
# Populated below. Lat/lng are float decimal degrees. State is 2-letter.

CITIES: dict[tuple[str, str], tuple[float, float]] = {
    # ============================================================
    # VIRGINIA — Ziad's home state, comprehensive
    # ============================================================
    ('richmond',          'va'): (37.5407, -77.4360),
    ('henrico',           'va'): (37.5739, -77.4256),
    ('hanover',           'va'): (37.7665, -77.3666),
    ('chesterfield',      'va'): (37.3793, -77.5141),
    ('glen allen',        'va'): (37.6660, -77.5066),
    ('midlothian',        'va'): (37.5079, -77.6433),
    ('mechanicsville',    'va'): (37.6093, -77.3733),
    ('ashland',           'va'): (37.7585, -77.4789),
    ('powhatan',          'va'): (37.5443, -77.9163),
    ('goochland',         'va'): (37.6802, -77.8869),
    ('short pump',        'va'): (37.6515, -77.6105),
    ('petersburg',        'va'): (37.2279, -77.4019),
    ('hopewell',          'va'): (37.3043, -77.2872),
    ('colonial heights',  'va'): (37.2649, -77.4030),
    ('fredericksburg',    'va'): (38.3032, -77.4605),
    ('stafford',          'va'): (38.4221, -77.4083),
    ('spotsylvania',      'va'): (38.2052, -77.5905),
    ('charlottesville',   'va'): (38.0293, -78.4767),
    ('norfolk',           'va'): (36.8508, -76.2859),
    ('virginia beach',    'va'): (36.8529, -75.9780),
    ('chesapeake',        'va'): (36.7682, -76.2875),
    ('newport news',      'va'): (37.0871, -76.4730),
    ('hampton',           'va'): (37.0299, -76.3452),
    ('williamsburg',      'va'): (37.2707, -76.7075),
    ('yorktown',          'va'): (37.2387, -76.5097),
    ('suffolk',           'va'): (36.7282, -76.5836),
    ('roanoke',           'va'): (37.2710, -79.9414),
    ('lynchburg',         'va'): (37.4138, -79.1422),
    ('blacksburg',        'va'): (37.2296, -80.4139),
    ('harrisonburg',      'va'): (38.4496, -78.8689),
    ('staunton',          'va'): (38.1496, -79.0717),
    ('winchester',        'va'): (39.1857, -78.1633),
    ('front royal',       'va'): (38.9182, -78.1944),
    ('warrenton',         'va'): (38.7137, -77.7953),
    ('culpeper',          'va'): (38.4731, -77.9964),
    ('manassas',          'va'): (38.7509, -77.4753),
    ('woodbridge',        'va'): (38.6582, -77.2497),
    ('dale city',         'va'): (38.6307, -77.3110),
    ('alexandria',        'va'): (38.8048, -77.0469),
    ('arlington',         'va'): (38.8816, -77.0910),
    ('fairfax',           'va'): (38.8462, -77.3064),
    ('falls church',      'va'): (38.8823, -77.1711),
    ('mclean',            'va'): (38.9339, -77.1773),
    ('tysons',            'va'): (38.9187, -77.2311),
    ('vienna',            'va'): (38.9012, -77.2653),
    ('reston',            'va'): (38.9586, -77.3570),
    ('herndon',           'va'): (38.9695, -77.3861),
    ('chantilly',         'va'): (38.8943, -77.4311),
    ('centreville',       'va'): (38.8401, -77.4291),
    ('annandale',         'va'): (38.8304, -77.1964),
    ('springfield',       'va'): (38.7893, -77.1872),
    ('burke',             'va'): (38.7934, -77.2719),
    ('lorton',            'va'): (38.7042, -77.2280),
    ('leesburg',          'va'): (39.1156, -77.5634),
    ('ashburn',           'va'): (39.0438, -77.4874),
    ('sterling',          'va'): (39.0062, -77.4286),

    # Washington DC + nearby MD
    ('washington',        'dc'): (38.9072, -77.0369),
    ('bethesda',          'md'): (38.9847, -77.0947),
    ('rockville',         'md'): (39.0840, -77.1528),
    ('silver spring',     'md'): (38.9907, -77.0261),
    ('gaithersburg',      'md'): (39.1434, -77.2014),
    ('takoma park',       'md'): (38.9779, -76.9925),
    ('college park',      'md'): (38.9897, -76.9378),
    ('hyattsville',       'md'): (38.9559, -76.9456),
    ('bowie',             'md'): (38.9429, -76.7308),
    ('annapolis',         'md'): (38.9784, -76.4922),
    ('baltimore',         'md'): (39.2904, -76.6122),
    ('columbia',          'md'): (39.2037, -76.8610),
    ('frederick',         'md'): (39.4143, -77.4105),

    # ============================================================
    # NORTHEAST
    # ============================================================
    ('new york',          'ny'): (40.7128, -74.0060),
    ('manhattan',         'ny'): (40.7831, -73.9712),
    ('brooklyn',          'ny'): (40.6782, -73.9442),
    ('queens',            'ny'): (40.7282, -73.7949),
    ('bronx',             'ny'): (40.8448, -73.8648),
    ('staten island',     'ny'): (40.5795, -74.1502),
    ('long island city',  'ny'): (40.7447, -73.9485),
    ('yonkers',           'ny'): (40.9312, -73.8987),
    ('white plains',      'ny'): (41.0340, -73.7629),
    ('new rochelle',      'ny'): (40.9115, -73.7824),
    ('buffalo',           'ny'): (42.8864, -78.8784),
    ('rochester',         'ny'): (43.1566, -77.6088),
    ('syracuse',          'ny'): (43.0481, -76.1474),
    ('albany',            'ny'): (42.6526, -73.7562),
    ('jersey city',       'nj'): (40.7178, -74.0431),
    ('newark',            'nj'): (40.7357, -74.1724),
    ('hoboken',           'nj'): (40.7440, -74.0324),
    ('princeton',         'nj'): (40.3573, -74.6672),
    ('trenton',           'nj'): (40.2206, -74.7597),
    ('edison',            'nj'): (40.5187, -74.4121),
    ('stamford',          'ct'): (41.0534, -73.5387),
    ('hartford',          'ct'): (41.7658, -72.6734),
    ('new haven',         'ct'): (41.3083, -72.9279),
    ('bridgeport',        'ct'): (41.1865, -73.1952),
    ('boston',            'ma'): (42.3601, -71.0589),
    ('cambridge',         'ma'): (42.3736, -71.1097),
    ('somerville',        'ma'): (42.3876, -71.0995),
    ('brookline',         'ma'): (42.3318, -71.1212),
    ('newton',            'ma'): (42.3370, -71.2092),
    ('waltham',           'ma'): (42.3765, -71.2356),
    ('lexington',         'ma'): (42.4430, -71.2290),
    ('worcester',         'ma'): (42.2626, -71.8023),
    ('lowell',            'ma'): (42.6334, -71.3162),
    ('quincy',            'ma'): (42.2529, -71.0023),
    ('providence',        'ri'): (41.8240, -71.4128),
    ('portland',          'me'): (43.6591, -70.2568),
    ('manchester',        'nh'): (42.9956, -71.4548),
    ('philadelphia',      'pa'): (39.9526, -75.1652),
    ('king of prussia',   'pa'): (40.0894, -75.3963),
    ('pittsburgh',        'pa'): (40.4406, -79.9959),
    ('allentown',         'pa'): (40.6084, -75.4902),
    ('wilmington',        'de'): (39.7391, -75.5398),
    ('dover',             'de'): (39.1582, -75.5244),

    # ============================================================
    # SOUTHEAST
    # ============================================================
    ('charlotte',         'nc'): (35.2271, -80.8431),
    ('raleigh',           'nc'): (35.7796, -78.6382),
    ('durham',            'nc'): (35.9940, -78.8986),
    ('chapel hill',       'nc'): (35.9132, -79.0558),
    ('greensboro',        'nc'): (36.0726, -79.7920),
    ('winston-salem',     'nc'): (36.0999, -80.2442),
    ('fayetteville',      'nc'): (35.0527, -78.8784),
    ('asheville',         'nc'): (35.5951, -82.5515),
    ('wilmington',        'nc'): (34.2104, -77.8868),
    ('cary',              'nc'): (35.7915, -78.7811),
    ('charleston',        'sc'): (32.7765, -79.9311),
    ('columbia',          'sc'): (34.0007, -81.0348),
    ('greenville',        'sc'): (34.8526, -82.3940),
    ('atlanta',           'ga'): (33.7490, -84.3880),
    ('alpharetta',        'ga'): (34.0754, -84.2941),
    ('marietta',          'ga'): (33.9526, -84.5499),
    ('decatur',           'ga'): (33.7748, -84.2963),
    ('sandy springs',     'ga'): (33.9304, -84.3733),
    ('savannah',          'ga'): (32.0809, -81.0912),
    ('augusta',           'ga'): (33.4735, -82.0105),
    ('macon',             'ga'): (32.8407, -83.6324),
    ('jacksonville',      'fl'): (30.3322, -81.6557),
    ('miami',             'fl'): (25.7617, -80.1918),
    ('orlando',           'fl'): (28.5383, -81.3792),
    ('tampa',             'fl'): (27.9506, -82.4572),
    ('st petersburg',     'fl'): (27.7676, -82.6403),
    ('fort lauderdale',   'fl'): (26.1224, -80.1373),
    ('miami beach',       'fl'): (25.7907, -80.1300),
    ('tallahassee',       'fl'): (30.4383, -84.2807),
    ('boca raton',        'fl'): (26.3683, -80.1289),
    ('gainesville',       'fl'): (29.6516, -82.3248),
    ('naples',            'fl'): (26.1420, -81.7948),
    ('birmingham',        'al'): (33.5186, -86.8104),
    ('huntsville',        'al'): (34.7304, -86.5861),
    ('montgomery',        'al'): (32.3792, -86.3077),
    ('mobile',            'al'): (30.6954, -88.0399),
    ('nashville',         'tn'): (36.1627, -86.7816),
    ('memphis',           'tn'): (35.1495, -90.0490),
    ('knoxville',         'tn'): (35.9606, -83.9207),
    ('chattanooga',       'tn'): (35.0456, -85.3097),
    ('louisville',        'ky'): (38.2527, -85.7585),
    ('lexington',         'ky'): (38.0406, -84.5037),
    ('new orleans',       'la'): (29.9511, -90.0715),
    ('baton rouge',       'la'): (30.4515, -91.1871),
    ('jackson',           'ms'): (32.2988, -90.1848),
    ('little rock',       'ar'): (34.7465, -92.2896),

    # ============================================================
    # MIDWEST
    # ============================================================
    ('chicago',           'il'): (41.8781, -87.6298),
    ('evanston',          'il'): (42.0451, -87.6877),
    ('schaumburg',        'il'): (42.0334, -88.0834),
    ('naperville',        'il'): (41.7508, -88.1535),
    ('oak park',          'il'): (41.8850, -87.7845),
    ('aurora',            'il'): (41.7606, -88.3201),
    ('rockford',          'il'): (42.2711, -89.0937),
    ('peoria',            'il'): (40.6936, -89.5890),
    ('detroit',           'mi'): (42.3314, -83.0458),
    ('ann arbor',         'mi'): (42.2808, -83.7430),
    ('grand rapids',      'mi'): (42.9634, -85.6681),
    ('lansing',           'mi'): (42.7325, -84.5555),
    ('cleveland',         'oh'): (41.4993, -81.6944),
    ('columbus',          'oh'): (39.9612, -82.9988),
    ('cincinnati',        'oh'): (39.1031, -84.5120),
    ('toledo',            'oh'): (41.6528, -83.5379),
    ('akron',             'oh'): (41.0814, -81.5190),
    ('dayton',            'oh'): (39.7589, -84.1916),
    ('indianapolis',      'in'): (39.7684, -86.1581),
    ('fort wayne',        'in'): (41.0793, -85.1394),
    ('milwaukee',         'wi'): (43.0389, -87.9065),
    ('madison',           'wi'): (43.0731, -89.4012),
    ('green bay',         'wi'): (44.5133, -88.0133),
    ('minneapolis',       'mn'): (44.9778, -93.2650),
    ('st paul',           'mn'): (44.9537, -93.0900),
    ('rochester',         'mn'): (44.0121, -92.4802),
    ('kansas city',       'mo'): (39.0997, -94.5786),
    ('st louis',          'mo'): (38.6270, -90.1994),
    ('springfield',       'mo'): (37.2090, -93.2923),
    ('omaha',             'ne'): (41.2565, -95.9345),
    ('lincoln',           'ne'): (40.8136, -96.7026),
    ('des moines',        'ia'): (41.5868, -93.6250),
    ('cedar rapids',      'ia'): (41.9779, -91.6656),
    ('wichita',           'ks'): (37.6872, -97.3301),

    # ============================================================
    # SOUTH / TEXAS
    # ============================================================
    ('austin',            'tx'): (30.2672, -97.7431),
    ('round rock',        'tx'): (30.5083, -97.6789),
    ('cedar park',        'tx'): (30.5052, -97.8203),
    ('georgetown',        'tx'): (30.6333, -97.6779),
    ('houston',           'tx'): (29.7604, -95.3698),
    ('the woodlands',     'tx'): (30.1658, -95.4613),
    ('sugar land',        'tx'): (29.6196, -95.6349),
    ('dallas',            'tx'): (32.7767, -96.7970),
    ('plano',             'tx'): (33.0198, -96.6989),
    ('frisco',            'tx'): (33.1507, -96.8236),
    ('irving',            'tx'): (32.8140, -96.9489),
    ('arlington',         'tx'): (32.7357, -97.1081),
    ('fort worth',        'tx'): (32.7555, -97.3308),
    ('san antonio',       'tx'): (29.4241, -98.4936),
    ('el paso',           'tx'): (31.7619, -106.4850),
    ('corpus christi',    'tx'): (27.8006, -97.3964),
    ('lubbock',           'tx'): (33.5779, -101.8552),
    ('oklahoma city',     'ok'): (35.4676, -97.5164),
    ('tulsa',             'ok'): (36.1540, -95.9928),

    # ============================================================
    # MOUNTAIN / WEST
    # ============================================================
    ('denver',            'co'): (39.7392, -104.9903),
    ('boulder',           'co'): (40.0150, -105.2705),
    ('colorado springs',  'co'): (38.8339, -104.8214),
    ('aurora',            'co'): (39.7294, -104.8319),
    ('fort collins',      'co'): (40.5853, -105.0844),
    ('lakewood',          'co'): (39.7047, -105.0814),
    ('salt lake city',    'ut'): (40.7608, -111.8910),
    ('provo',             'ut'): (40.2338, -111.6585),
    ('park city',         'ut'): (40.6461, -111.4980),
    ('phoenix',           'az'): (33.4484, -112.0740),
    ('scottsdale',        'az'): (33.4942, -111.9261),
    ('tempe',             'az'): (33.4255, -111.9400),
    ('mesa',              'az'): (33.4152, -111.8315),
    ('chandler',          'az'): (33.3062, -111.8413),
    ('tucson',            'az'): (32.2226, -110.9747),
    ('flagstaff',         'az'): (35.1983, -111.6513),
    ('las vegas',         'nv'): (36.1699, -115.1398),
    ('henderson',         'nv'): (36.0395, -114.9817),
    ('reno',              'nv'): (39.5296, -119.8138),
    ('albuquerque',       'nm'): (35.0844, -106.6504),
    ('santa fe',          'nm'): (35.6870, -105.9378),
    ('boise',             'id'): (43.6150, -116.2023),
    ('billings',          'mt'): (45.7833, -108.5007),
    ('cheyenne',          'wy'): (41.1400, -104.8202),

    # ============================================================
    # WEST COAST
    # ============================================================
    ('san francisco',     'ca'): (37.7749, -122.4194),
    ('oakland',           'ca'): (37.8044, -122.2712),
    ('berkeley',          'ca'): (37.8716, -122.2727),
    ('san jose',          'ca'): (37.3382, -121.8863),
    ('palo alto',         'ca'): (37.4419, -122.1430),
    ('mountain view',     'ca'): (37.3861, -122.0839),
    ('sunnyvale',         'ca'): (37.3688, -122.0363),
    ('santa clara',       'ca'): (37.3541, -121.9552),
    ('redwood city',      'ca'): (37.4852, -122.2364),
    ('san mateo',         'ca'): (37.5630, -122.3255),
    ('fremont',           'ca'): (37.5485, -121.9886),
    ('hayward',           'ca'): (37.6688, -122.0808),
    ('los angeles',       'ca'): (34.0522, -118.2437),
    ('santa monica',      'ca'): (34.0195, -118.4912),
    ('pasadena',          'ca'): (34.1478, -118.1445),
    ('burbank',           'ca'): (34.1808, -118.3090),
    ('long beach',        'ca'): (33.7701, -118.1937),
    ('irvine',            'ca'): (33.6846, -117.8265),
    ('santa ana',         'ca'): (33.7455, -117.8677),
    ('anaheim',           'ca'): (33.8366, -117.9143),
    ('san diego',         'ca'): (32.7157, -117.1611),
    ('la jolla',          'ca'): (32.8328, -117.2713),
    ('carlsbad',          'ca'): (33.1581, -117.3506),
    ('sacramento',        'ca'): (38.5816, -121.4944),
    ('davis',             'ca'): (38.5449, -121.7405),
    ('fresno',            'ca'): (36.7378, -119.7871),
    ('bakersfield',       'ca'): (35.3733, -119.0187),
    ('seattle',           'wa'): (47.6062, -122.3321),
    ('bellevue',          'wa'): (47.6101, -122.2015),
    ('redmond',           'wa'): (47.6740, -122.1215),
    ('kirkland',          'wa'): (47.6815, -122.2087),
    ('tacoma',            'wa'): (47.2529, -122.4443),
    ('spokane',           'wa'): (47.6588, -117.4260),
    ('portland',          'or'): (45.5152, -122.6784),
    ('beaverton',         'or'): (45.4871, -122.8037),
    ('eugene',            'or'): (44.0521, -123.0868),
    ('honolulu',          'hi'): (21.3099, -157.8581),
    ('anchorage',         'ak'): (61.2181, -149.9003),
}


# ----------------------------------------------------------------------------
# State name → 2-letter code (for parsing free text)
# ----------------------------------------------------------------------------

_STATE_CODES: dict[str, str] = {
    'alabama': 'al', 'alaska': 'ak', 'arizona': 'az', 'arkansas': 'ar',
    'california': 'ca', 'colorado': 'co', 'connecticut': 'ct', 'delaware': 'de',
    'florida': 'fl', 'georgia': 'ga', 'hawaii': 'hi', 'idaho': 'id',
    'illinois': 'il', 'indiana': 'in', 'iowa': 'ia', 'kansas': 'ks',
    'kentucky': 'ky', 'louisiana': 'la', 'maine': 'me', 'maryland': 'md',
    'massachusetts': 'ma', 'michigan': 'mi', 'minnesota': 'mn',
    'mississippi': 'ms', 'missouri': 'mo', 'montana': 'mt', 'nebraska': 'ne',
    'nevada': 'nv', 'new hampshire': 'nh', 'new jersey': 'nj',
    'new mexico': 'nm', 'new york': 'ny', 'north carolina': 'nc',
    'north dakota': 'nd', 'ohio': 'oh', 'oklahoma': 'ok', 'oregon': 'or',
    'pennsylvania': 'pa', 'rhode island': 'ri', 'south carolina': 'sc',
    'south dakota': 'sd', 'tennessee': 'tn', 'texas': 'tx', 'utah': 'ut',
    'vermont': 'vt', 'virginia': 'va', 'washington': 'wa',
    'west virginia': 'wv', 'wisconsin': 'wi', 'wyoming': 'wy',
    'district of columbia': 'dc',
}


# ----------------------------------------------------------------------------
# Geocoding
# ----------------------------------------------------------------------------

@lru_cache(maxsize=2048)
def geocode(text: str) -> Optional[tuple[float, float]]:
    """Best-effort geocoding of free-text location strings.

    Examples it handles:
      "Richmond, VA"        -> (37.5407, -77.4360)
      "Richmond Virginia"   -> (37.5407, -77.4360)
      "Glen Allen, VA"      -> (37.6660, -77.5066)
      "USA-Washington-DC"   -> (38.9072, -77.0369)
      "Remote (US)"         -> None (caller should handle remote separately)

    Returns None if nothing matches.
    """
    if not text:
        return None
    t = text.lower().strip()

    # Strip common prefixes like "USA-" or "United States-"
    for prefix in ('usa-', 'usa, ', 'united states-', 'united states, '):
        if t.startswith(prefix):
            t = t[len(prefix):]

    # Detect state from text
    state_code = _detect_state(t)

    # Try to find a known city in the haystack
    # Iterate longest cities first to avoid "york" matching "new york"
    for (city_lower, st), coords in sorted(
        CITIES.items(), key=lambda kv: -len(kv[0][0])
    ):
        # If we know the state, require it match
        if state_code and st != state_code:
            continue
        # Match city name as a word/phrase
        if _city_in_text(city_lower, t):
            return coords

    return None


def _city_in_text(city: str, text: str) -> bool:
    """City name appears as a word boundary in text."""
    pattern = r'(?:^|[^a-z])' + re.escape(city) + r'(?:[^a-z]|$)'
    return re.search(pattern, text) is not None


def _detect_state(text: str) -> Optional[str]:
    """Find a US state in the text. Returns 2-letter lowercase code or None.

    Strategy: collect ALL plausible state matches, then prefer the one with
    the most specific signal. "Washington DC" should resolve to DC, not WA,
    even though "washington" matches the state Washington as a name.
    """
    text = text.lower()
    found: list[tuple[str, int]] = []   # (state_code, score)

    # 2-letter codes — STRONGER signal because they're explicit suffixes
    for code in _STATE_CODES.values():
        if re.search(r'(?:^|[\s,\-])' + code + r'(?:[\s,\-]|$)', text):
            found.append((code, 10))

    # Full state names — weaker signal because they collide with city names
    # (e.g. "washington" the state vs "Washington DC" the city)
    for name, code in _STATE_CODES.items():
        if re.search(r'(?:^|[^a-z])' + re.escape(name) + r'(?:[^a-z]|$)', text):
            found.append((code, 5))

    if not found:
        return None
    # Pick the highest-score match. If tie, pick the one whose match index is
    # latest (states usually come at the end of "City, State" strings).
    found.sort(key=lambda t: (-t[1]))
    return found[0][0]


# ----------------------------------------------------------------------------
# Haversine distance
# ----------------------------------------------------------------------------

EARTH_RADIUS_MILES = 3959.0


def haversine_miles(
    coord_a: tuple[float, float],
    coord_b: tuple[float, float],
) -> float:
    """Great-circle distance between two (lat, lng) points, in statute miles."""
    lat1, lng1 = math.radians(coord_a[0]), math.radians(coord_a[1])
    lat2, lng2 = math.radians(coord_b[0]), math.radians(coord_b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_MILES * c


# Leeway factor — generous filtering. 50mi → effective 60mi, 75mi → 90mi.
# Stays generous-by-design so we don't drop near-miss roles that the LLM
# can still judge well.
RADIUS_LEEWAY = 1.20


def is_within_radius(
    role_location_text: str,
    accept_location_text: str,
    radius_miles: float,
    leeway: float = RADIUS_LEEWAY,
) -> Optional[bool]:
    """Return True if role is within radius (× leeway) of acceptable location.

    With leeway=1.20 (default), a 50mi setting effectively allows up to 60mi.
    The LLM downstream can still reject roles that are a poor fit.

    Returns:
      True   — both geocoded, within radius × leeway
      False  — both geocoded, beyond radius × leeway
      None   — couldn't geocode one or both (caller should fall back)
    """
    role_coords = geocode(role_location_text)
    accept_coords = geocode(accept_location_text)
    if role_coords is None or accept_coords is None:
        return None
    effective_radius = radius_miles * leeway
    return haversine_miles(role_coords, accept_coords) <= effective_radius
