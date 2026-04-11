"""
geocode_approx.py
-----------------
Fetches all approx-location (ungeocoded) property transfer records from the
ArcGIS service and attempts to resolve their coordinates using these methods
in priority order:

  1. E911 address point (VCGI) — exact Vermont address database. Most accurate.
     Queries by house number + street name + town. Returns rooftop coords.

  2. SPAN lookup — queries VT parcel layer by SPAN number.
     Returns parcel centroid. Fast, accurate, no wrong-town matches.

  3. ArcGIS sibling lookup — for condo/unit addresses where the individual SPAN
     isn't in the parcel layer. Finds another record at the same base address.

  4. Nominatim geocoding — used when all above fail. Constrained to a bounding
     box around propLocCty to prevent wrong-town matches.

Records that fail all methods remain with null coords and are served at the
town centroid fallback in app.py.

Run from c:\\vt_app:
    python geocode_approx.py

First run: processes all approx records (~3,952, ~1-2 hours).
Subsequent runs: skips OBJECTIDs already in file, only processes new ones.

After running:
    git add geocoded_approx.json
    git commit -m "Update geocoded approx records"
    git push origin main
"""

import requests
import json
import time
import os
import re
import argparse
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE   = os.path.join(SCRIPT_DIR, "geocoded_approx.json")
VT_CODES_FILE = os.path.join(SCRIPT_DIR, "vt_codes.json")

# ── Service URLs ───────────────────────────────────────────────────────────────
PTT_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"
)
PARCEL_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/ArcGIS/rest/services/"
    "FS_VCGI_VTPARCELS_WM_NOCACHE_v2/FeatureServer/1/query"
)
E911_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Emergency_ESITE_point_SP_v1/FeatureServer/0/query"
)

# ── Vermont bounds ─────────────────────────────────────────────────────────────
VT_LAT_MIN, VT_LAT_MAX =  42.7,  45.1
VT_LON_MIN, VT_LON_MAX = -73.5, -71.5
TOWN_BBOX_PAD = 0.15  # ~10 miles around town centroid for Nominatim

COUNTY_BOUNDS = {
    "01": (43.60, 44.55, -73.50, -72.60),
    "02": (42.70, 43.50, -73.50, -72.70),
    "03": (44.10, 45.05, -72.55, -71.40),
    "04": (44.10, 44.90, -73.50, -72.65),
    "05": (44.20, 45.05, -72.35, -71.40),
    "06": (44.45, 45.05, -73.40, -72.35),
    "07": (44.45, 45.05, -73.55, -72.95),
    "08": (44.25, 44.95, -73.15, -72.20),
    "09": (43.60, 44.40, -72.90, -71.80),
    "10": (44.40, 45.05, -72.75, -71.70),
    "11": (43.10, 44.10, -73.55, -72.35),
    "12": (43.85, 44.70, -73.10, -72.05),
    "13": (42.65, 43.50, -73.05, -71.85),
    "14": (43.10, 44.25, -72.95, -71.95),
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_vt_codes():
    with open(VT_CODES_FILE) as f:
        codes = json.load(f)
    school_to_town   = {int(k): v for k, v in codes.get("school_to_town", {}).items()}
    town_to_county   = codes.get("town_to_county", {})
    town_centroids   = codes.get("town_centroids", {})
    county_names     = codes.get("counties", {})
    span_prefix_map  = codes.get("span_prefix_to_town", {})
    return school_to_town, town_to_county, town_centroids, county_names, span_prefix_map


def span_prefix_town(span_raw, span_prefix_map):
    """Extract the 6-digit SPAN prefix and look up the town.
    Returns the town name or None if not found.
    The SPAN prefix is the most reliable town indicator since it is
    mathematically assigned per town and cannot be entered incorrectly.
    """
    if not span_raw:
        return None
    s = str(span_raw).strip().replace('-', '').replace(' ', '')
    if len(s) < 6 or not s.isdigit():
        return None
    prefix = f"{s[:3]}-{s[3:6]}"
    return span_prefix_map.get(prefix)


def coords_in_county(lat, lon, county_code):
    b = COUNTY_BOUNDS.get(str(county_code).zfill(2))
    if not b:
        return True
    return b[0] <= lat <= b[1] and b[2] <= lon <= b[3]


def coords_in_vt(lat, lon):
    return (lat is not None and lon is not None and
            VT_LAT_MIN <= lat <= VT_LAT_MAX and
            VT_LON_MIN <= lon <= VT_LON_MAX)


# MatchMthod values (mirrors app.py)
_GOOD_MATCH_METHODS = {
    'property address (esite)',
    'property address (composite)',
    'span (esite)',
}
_APPROX_MATCH_METHODS = {
    'span (parcel centroid)',
}


# sUsePr codes that indicate vacant/unimproved land (Section H of PTT-172)
_VACANT_LAND_USE_CODES = {'8', '08', '9', '09'}  # Open Land, Timberland


def is_approx(lat, lon, match_method=None, trusted_county=None,
              suse_pr=None, blcn1=None):
    """Return True if this record needs geocoding (approx cache treatment).

    F3 (blCn1) is the primary vacant land indicator. If null, zero,
    or '01'/'1' (None declared), the property has no building and gets
    SPAN-first placement regardless of MatchMthod. ESITE coordinates
    can be wrong for unaddressed parcels so we override with parcel centroid.
    """
    mm = (match_method or '').strip().lower()

    # F3 = null/zero/'01'/'1' means no building declared by preparer.
    # Both padded ('01') and unpadded ('1') forms handled.
    b1 = str(blcn1 or '').strip().lstrip('0') or '0'
    is_vacant = b1 in ('0', '1')  # null/zero/01/1 = no building

    # All vacant land -> approx cache for SPAN-first placement.
    # Overrides even ESITE coords which can be wrong for bare parcels.
    if is_vacant:
        return True

    if mm in _GOOD_MATCH_METHODS:
        if coords_in_vt(lat, lon) and lat != 0 and lon != 0:
            return False

    if mm in _APPROX_MATCH_METHODS:
        if coords_in_vt(lat, lon):
            return False

    return True


def format_span(span_raw):
    """Convert 11-digit SPAN to parcel layer format: 34810810006 -> 348-108-10006"""
    s = str(span_raw).strip()
    if len(s) < 11:
        return None
    return f"{s[:3]}-{s[3:6]}-{s[6:]}"


def has_street_number(address):
    """Return True if address starts with a house number."""
    return bool(re.match(r"^\d+\s+\S+", (address or "").strip()))


def parse_street_number(address):
    """Extract the numeric house number from the start of an address string."""
    m = re.match(r"^(\d+)", (address or "").strip())
    return int(m.group(1)) if m else None


def normalize_street_name(address):
    """
    Extract and normalize the street name portion of an address for E911 matching.
    Strips house number, unit suffixes, and common abbreviations.
    Returns (house_number_str, street_name_str) or (None, None).

    E911 uses abbreviated street types (RD, ST, DR, LN, AVE etc.)
    and uppercase names. We normalize both sides to uppercase with
    common expansions so PTT addresses like "SOUTH HILL ROAD" match
    E911's "SOUTH HILL RD".
    """
    addr = (address or "").strip().upper()
    # Strip unit suffixes
    addr = re.sub(r',?\s*(UNIT|APT|SUITE|STE|LOT|#)\s*.*$', '', addr, flags=re.I).strip()

    m = re.match(r'^(\d+)\s+(.+)$', addr)
    if not m:
        return None, None

    house_num = m.group(1)
    street    = m.group(2).strip()

    # Expand common full-word street types to E911 abbreviations
    TYPE_MAP = {
        r'\bROAD\b':    'RD',
        r'\bSTREET\b':  'ST',
        r'\bAVENUE\b':  'AVE',
        r'\bDRIVE\b':   'DR',
        r'\bLANE\b':    'LN',
        r'\bCOURT\b':   'CT',
        r'\bCIRCLE\b':  'CIR',
        r'\bBOULEVARD\b': 'BLVD',
        r'\bHIGHWAY\b': 'HWY',
        r'\bTERRACE\b': 'TER',
        r'\bPLACE\b':   'PL',
        r'\bTRAIL\b':   'TRL',
        r'\bWAY\b':     'WAY',
        # Vermont Route variants — normalize to "VT RTE" which E911 uses
        r'\bVERMONT\s+ROUTE\b': 'VT RTE',
        r'\bVT\s+ROUTE\b':      'VT RTE',
        r'\bSTATE\s+ROUTE\b':   'VT RTE',
        r'\bROUTE\b':            'RTE',
        r'\bUS\s+ROUTE\b':      'US RTE',
        r'\bUS\s+RT\b':         'US RTE',
    }
    for pattern, replacement in TYPE_MAP.items():
        street = re.sub(pattern, replacement, street, flags=re.I)

    return house_num, street.strip()


# ── Method 1: E911 address point lookup ───────────────────────────────────────

def e911_lookup(address, geocode_town, town_centroids):
    """
    Look up exact coordinates from the VCGI E911 address point layer.
    Queries by house number + street name + town.

    This is the most accurate method for Vermont addresses — VCGI maintains
    E911 address points statewide. Results are rooftop or driveway-level.

    Returns (lat, lon, method) or (None, None, None).
    """
    house_num, street = normalize_street_name(address)
    if not house_num or not street:
        return None, None, None

    # Try the geocode_town first, then the resolved town if different
    towns_to_try = []
    town_upper = (geocode_town or '').strip().upper()
    if town_upper:
        towns_to_try.append(town_upper)
    # Also try village->town mapping
    resolved = VILLAGE_TO_TOWN.get(town_upper)
    if resolved and resolved != town_upper:
        towns_to_try.append(resolved)

    for town in towns_to_try:
        # E911 PRIMARYADDRESS is "{house_num} {street_name} {street_type}"
        # We query by house number and town, then filter by street name match
        # since PTT street names may differ slightly from E911 (ROAD vs RD etc.)
        try:
            params = {
                "where":             f"HOUSE_NUMBER={house_num} AND TOWNNAME='{town}'",
                "outFields":         "PRIMARYADDRESS,TOWNNAME,GPSX,GPSY,SPAN",
                "f":                 "json",
                "outSR":             "4326",
                "resultRecordCount": 10,
            }
            r = requests.get(E911_URL, params=params, timeout=15)
            feats = r.json().get("features", [])

            for feat in feats:
                a = feat["attributes"]
                g = feat.get("geometry", {})
                e911_addr = (a.get("PRIMARYADDRESS") or "").upper().strip()

                # Extract street name from E911 address for fuzzy matching
                e911_m = re.match(r'^\d+\s+(.+)$', e911_addr)
                if not e911_m:
                    continue
                e911_street = e911_m.group(1).strip()

                # Check if key words of the PTT street match E911's street
                # Use word-overlap: at least the significant words must match
                if not _street_names_match(street, e911_street):
                    continue

                lat = g.get("y") or a.get("GPSY")
                lon = g.get("x") or a.get("GPSX")
                try:
                    lat = float(lat) if lat is not None else None
                    lon = float(lon) if lon is not None else None
                except (TypeError, ValueError):
                    continue

                if coords_in_vt(lat, lon):
                    return lat, lon, "e911"

        except Exception as e:
            print(f" [e911 error: {e}]", end="")

    return None, None, None


def _street_names_match(ptt_street, e911_street):
    """
    Fuzzy match between a PTT street name and an E911 street name.
    Both are already uppercase. Returns True if they refer to the same road.

    Strategy: extract significant words (not type abbreviations, not
    directionals) and check that they all appear in both strings.
    """
    SKIP_WORDS = {
        'RD','ST','AVE','DR','LN','CT','CIR','BLVD','HWY','TER','PL',
        'TRL','WAY','ROAD','STREET','AVENUE','DRIVE','LANE','COURT',
        'CIRCLE','BOULEVARD','HIGHWAY','TERRACE','PLACE','TRAIL',
        'N','S','E','W','NORTH','SOUTH','EAST','WEST',
        'VT','RTE','ROUTE','US','RT',
    }
    def sig_words(s):
        return {w for w in re.findall(r'\w+', s.upper()) if w not in SKIP_WORDS}

    ptt_words  = sig_words(ptt_street)
    e911_words = sig_words(e911_street)

    if not ptt_words or not e911_words:
        return False

    # All significant PTT words must appear in E911 (or vice versa for short names)
    return ptt_words <= e911_words or e911_words <= ptt_words


# ── Method 2: SPAN lookup ──────────────────────────────────────────────────────

def span_lookup(span_raw):
    """
    Look up parcel centroid by SPAN in the VT parcel layer.
    Returns (lat, lon, method) or (None, None, None).
    """
    span_fmt = format_span(span_raw)
    if not span_fmt:
        return None, None, None

    # Skip SPANs that are clearly non-unique (timeshare master SPANs)
    if span_fmt.endswith("-00000"):
        return None, None, None

    try:
        params = {
            "where":           f"SPAN='{span_fmt}'",
            "outFields":       "SPAN,TNAME,E911ADDR",
            "f":               "json",
            "outSR":           "4326",
            "resultRecordCount": 1,
            "returnCentroid":  "true",
        }
        r = requests.get(PARCEL_URL, params=params, timeout=15)
        d = r.json()
        feats = d.get("features", [])
        if feats:
            c = feats[0].get("centroid", {})
            lat = c.get("y")
            lon = c.get("x")
            if lat and lon and coords_in_vt(lat, lon):
                return lat, lon, "span"
    except Exception as e:
        print(f" [span error: {e}]", end="")

    return None, None, None


# ── Method 3: ArcGIS sibling lookup ──────────────────────────────────────────

def arcgis_sibling_lookup(address, school_code, county_bounds):
    """Find coordinates from another ArcGIS record at the same base address.
    Strips unit numbers and searches for any record with valid coords.
    Returns (lat, lon, method) or (None, None, None).
    """
    base = re.sub(r',?\s*(UNIT|APT|SUITE|STE|LOT|#)\s*.*$', '', address, flags=re.I).strip()
    if not base or not re.match(r'^\d+', base):
        return None, None, None

    base_norm = re.sub(r'\bVERMONT\s+', '', base, flags=re.I)
    base_norm = re.sub(r'\bVT\s+', '', base_norm, flags=re.I).strip()
    street_num = base_norm.split()[0] if base_norm else ''
    words = base_norm.split()
    partial = ' '.join(words[:2]) if len(words) >= 2 else base_norm

    try:
        try:
            sc_num = int(float(str(school_code)))
        except (TypeError, ValueError):
            return None, None, None
        params = {
            'where':    f"propLocStr LIKE '{street_num} %' AND schoolCode={sc_num}",
            'outFields': 'OBJECTID,propLocStr,Latitude,Longitude',
            'f':        'json',
            'outSR':    '4326',
            'resultRecordCount': 10,
        }
        r = requests.get(PTT_URL, params=params, timeout=15)
        feats = r.json().get('features', [])
        for feat in feats:
            a = feat['attributes']
            g = feat.get('geometry', {})
            sibling_addr = (a.get('propLocStr') or '').upper()
            sibling_norm = re.sub(r'\bVERMONT\s+', '', sibling_addr, flags=re.I)
            sibling_norm = re.sub(r'\bVT\s+', '', sibling_norm, flags=re.I)
            sibling_norm = re.sub(r',?\s*(UNIT|APT|SUITE|STE|LOT|#)\s*.*$', '', sibling_norm, flags=re.I).strip()
            if sibling_norm.upper() != base_norm.upper():
                continue
            lat = g.get('y') or a.get('Latitude')
            lon = g.get('x') or a.get('Longitude')
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                continue
            if lat and lon and coords_in_vt(lat, lon):
                return lat, lon, 'arcgis_sibling'
    except Exception:
        pass
    return None, None, None


# ── Method 4: Nominatim geocoding ─────────────────────────────────────────────

VILLAGE_TO_TOWN = {
    'BELLOWS FALLS':         'ROCKINGHAM',
    'MORRISVILLE':           'MORRISTOWN',
    'LYNDONVILLE':           'LYNDON',
    'ISLAND POND':           'BRIGHTON',
    'PROCTORSVILLE':         'CAVENDISH',
    'PERKINSVILLE':          'WEATHERSFIELD',
    'NORTH SPRINGFIELD':     'SPRINGFIELD',
    'NORTH MONTPELIER':      'CALAIS',
    'EAST MONTPELIER VILLAGE': 'EAST MONTPELIER',
    'WEST BURKE':            'BURKE',
    'EAST BURKE':            'BURKE',
    'BARTON VILLAGE':        'BARTON',
    'ORLEANS VILLAGE':       'BARTON',
    'WEST PAWLET':           'PAWLET',
    'NORTH BENNINGTON':      'BENNINGTON',
    'READSBORO':             'READSBORO',
    'JACKSONVILLE':          'WHITINGHAM',
    'NORTH TROY':            'TROY',
    'EAST HARDWICK':         'HARDWICK',
    'GREENSBORO BEND':       'GREENSBORO',
    'CRAFTSBURY COMMON':     'CRAFTSBURY',
    'EAST CRAFTSBURY':       'CRAFTSBURY',
    'NORTH HYDE PARK':       'HYDE PARK',
    'JEFFERSONVILLE':        'CAMBRIDGE',
    'SMUGGLERS NOTCH':       'CAMBRIDGE',
    'WATERBURY CENTER':      'WATERBURY',
    'STOWE HOLLOW':          'STOWE',
    'MOSCOW':                'STOWE',
    'NORTH POMFRET':         'POMFRET',
    'SOUTH POMFRET':         'POMFRET',
    'TAFTSVILLE':            'WOODSTOCK',
    'QUECHEE':               'HARTFORD',
    'WILDER':                'HARTFORD',
    'WHITE RIVER JUNCTION':  'HARTFORD',
    'WEST LEBANON':          'HARTFORD',
    'ASCUTNEY':              'WEATHERSFIELD',
    'BROWNSVILLE':           'WEST WINDSOR',
    'NORTH HARTLAND':        'HARTLAND',
    'NORTH THETFORD':        'THETFORD',
    'POST MILLS':            'THETFORD',
    'EAST THETFORD':         'THETFORD',
    'FAIRLEE VILLAGE':       'FAIRLEE',
    'WELLS RIVER':           'NEWBURY',
    'GROTON':                'GROTON',
    'MARSHFIELD VILLAGE':    'MARSHFIELD',
}


def resolve_geocode_town(geocode_town, town_centroids):
    key = (geocode_town or '').strip().upper()
    if key in town_centroids:
        return geocode_town
    mapped = VILLAGE_TO_TOWN.get(key)
    if mapped and mapped in town_centroids:
        return mapped.title()
    return geocode_town


def nominatim_geocode(address, geocode_town, town_centroids):
    """
    Geocode address via Nominatim constrained to geocode_town's bbox.
    Only used when E911, SPAN, and sibling lookups all fail.
    Returns (lat, lon, method) or (None, None, None).
    """
    if not has_street_number(address):
        return None, None, None

    resolved_town = resolve_geocode_town(geocode_town, town_centroids)
    centroid = town_centroids.get((resolved_town or "").upper())
    if centroid:
        clat, clon = centroid["lat"], centroid["lon"]
        bbox = (
            f"{clon - TOWN_BBOX_PAD},"
            f"{clat + TOWN_BBOX_PAD},"
            f"{clon + TOWN_BBOX_PAD},"
            f"{clat - TOWN_BBOX_PAD}"
        )
        city_str = geocode_town.title() if geocode_town else ""
    else:
        bbox     = f"{VT_LON_MIN},{VT_LAT_MAX},{VT_LON_MAX},{VT_LAT_MIN}"
        city_str = geocode_town.title() if geocode_town else ""

    queries = [
        f"{address}, {city_str}, VT",
        f"{address}, {city_str}, Vermont",
        f"{address}, Vermont",
    ]

    _DIR_EXPAND = {'N': 'North', 'S': 'South', 'E': 'East', 'W': 'West'}

    route_bare = re.search(r'(.*\bROUTE\s+(\d+))\s*$', address.strip(), re.I)
    if route_bare:
        base = route_bare.group(1)
        for full, abbr in [('North','N'),('South','S'),('East','E'),('West','W')]:
            queries.append(f"{base} {full}, {city_str}, Vermont")
            queries.append(f"{base} {full}, Vermont")
            queries.append(f"{base}{abbr}, {city_str}, Vermont")
            queries.append(f"{base} {abbr}, {city_str}, Vermont")

    route_abbr = re.search(
        r'(.*\bROUTE\s+(\d+))\s*([NSEW])\s*$',
        address.strip(),
        re.I
    )
    if route_abbr:
        base = route_abbr.group(1)
        abbr = route_abbr.group(3).upper()
        full = _DIR_EXPAND.get(abbr, abbr)
        queries.append(f"{base} {full}, {city_str}, Vermont")
        queries.append(f"{base} {full}, Vermont")

    for q in queries:
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?format=json&limit=1&countrycodes=us"
            f"&viewbox={bbox}&bounded=1"
            f"&q={requests.utils.quote(q)}"
        )
        try:
            r = requests.get(
                url,
                headers={"User-Agent": "VTPropertySales/1.0"},
                timeout=10
            )
            results = r.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                if coords_in_vt(lat, lon):
                    return lat, lon, "nominatim"
        except Exception:
            pass
        time.sleep(1)  # Nominatim rate limit: 1 req/sec

    return None, None, None


# ── Fetch approx records ───────────────────────────────────────────────────────

def fetch_all_approx(school_to_town, town_to_county, span_prefix_map=None, date_from=None):
    """Page through ArcGIS and return all approx records."""
    span_prefix_map = span_prefix_map or {}
    date_from_filter = date_from  # passed into WHERE clause below
    print("Fetching all property transfer records from ArcGIS...")
    all_approx = []
    offset     = 0
    page_size  = 2000
    total_fetched = 0

    while True:
        params = {
            "where":             f"ValPdOrTrn > 0" + (
                f" AND closeDate >= date '{date_from_filter}'" if date_from_filter else ""
            ),
            "outFields":         "OBJECTID,span,propLocStr,propLocCty,TOWNNAME,schoolCode,Latitude,Longitude,MatchMthod,"
                                 "intPrpType,blCn1,blCn2,blCn3,TownGlCat,sUsePr,bUsePr,prTxEx,landSize,closeDate,ValPdOrTrn",
            "f":                 "json",
            "outSR":             "4326",
            "resultRecordCount": page_size,
            "resultOffset":      offset,
        }
        try:
            r = requests.get(PTT_URL, params=params, timeout=30)
            data = r.json()
        except Exception as e:
            print(f"\n  ERROR at offset {offset}: {e} — retrying in 10s")
            time.sleep(10)
            continue

        if data.get("error"):
            print(f"\n  ArcGIS error: {data['error']}")
            break

        features = data.get("features", [])
        total_fetched += len(features)

        for feat in features:
            a   = feat["attributes"]
            g   = feat.get("geometry", {})
            lat = g.get("y") or a.get("Latitude")
            lon = g.get("x") or a.get("Longitude")
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None

            sc = a.get("schoolCode")
            try:
                sc_int = int(float(str(sc)))
            except (TypeError, ValueError):
                sc_int = None

            school_town    = school_to_town.get(sc_int)
            trusted_county = town_to_county.get(school_town) if school_town else None
            prop_loc_city  = (a.get("propLocCty") or "").strip().title()
            arcgis_townname = (a.get("TOWNNAME") or "").strip().title()
            span_raw       = a.get("span") or a.get("TownSpan")

            # Town resolution priority (most to least reliable):
            # 1. SPAN prefix — mathematically assigned, cannot be a typo
            # 2. TOWNNAME — set by VCGI from GPS coordinates
            # 3. propLocCty — entered by preparer, may use village names
            # 4. schoolCode lookup — least reliable, often entered wrong
            span_town = span_prefix_town(span_raw, span_prefix_map)
            if span_town and town_to_county.get(span_town):
                school_town    = span_town
                trusted_county = town_to_county[span_town]
            elif arcgis_townname and town_to_county.get(arcgis_townname):
                school_town    = arcgis_townname
                trusted_county = town_to_county[arcgis_townname]
            elif prop_loc_city and town_to_county.get(prop_loc_city):
                school_town    = prop_loc_city
                trusted_county = town_to_county[prop_loc_city]
            # else: keep schoolCode-derived school_town as last resort

            geocode_town   = prop_loc_city if prop_loc_city else (school_town or "")
            match_method   = a.get("MatchMthod") or ""

            if is_approx(lat, lon, match_method=match_method, trusted_county=trusted_county,
                         suse_pr=a.get('sUsePr'), blcn1=a.get('blCn1')):
                all_approx.append({
                    "objectid":        a.get("OBJECTID"),
                    "span":            a.get("span"),
                    "address":         (a.get("propLocStr") or "").strip(),
                    "city":            prop_loc_city,
                    "geocode_town":    geocode_town,
                    "trusted_town":    school_town,
                    "trusted_county":  trusted_county,
                    "school_code_raw": sc,
                    # Filterable fields — stored so client-side filter works
                    # without needing a separate ArcGIS fetch per approx dot.
                    "intPrpType":  a.get("intPrpType"),
                    "blCn1":       a.get("blCn1"),
                    "blCn2":       a.get("blCn2"),
                    "blCn3":       a.get("blCn3"),
                    "TownGlCat":   a.get("TownGlCat"),
                    "sUsePr":      a.get("sUsePr"),
                    "bUsePr":      a.get("bUsePr"),
                    "prTxEx":      a.get("prTxEx"),
                    "landSize":    a.get("landSize"),
                    "closeDate":   a.get("closeDate"),   # epoch ms
                    "ValPdOrTrn":  a.get("ValPdOrTrn"),
                })

        print(f"  Fetched {total_fetched:,} records, {len(all_approx):,} approx so far...", end="\r")

        if len(features) < page_size:
            break
        offset += page_size

    print(f"\nTotal approx records: {len(all_approx):,}")
    return all_approx


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Geocode approx VT property records')
    parser.add_argument('--date-from', default=None,
        help='Only process records with closeDate >= this date (YYYY-MM-DD). '
             'Use for test runs, e.g. --date-from 2025-04-11 for last 12 months.')
    args = parser.parse_args()
    print("=" * 60)
    print("VT Property Sales — Approx Record Geocoder")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Load existing results
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        print(f"Loaded {len(existing):,} existing records from {OUTPUT_FILE}")
    else:
        existing = {}
        print("No existing file — starting fresh.")

    # Records with null coords are eligible for retry — E911 may now resolve
    # addresses that previously failed SPAN and Nominatim.
    # Records with manual_* methods are kept as-is (hand-entered coords).
    MANUAL_METHODS = {'manual_nominatim', 'manual_sibling'}
    # A record is "done" only if it has coords AND all 11 filterable fields.
    # Records missing any filterable field are re-processed to add them.
    # Manual records keep their coords but still get filterable fields updated.
    FILTER_FIELDS = ('intPrpType','blCn1','blCn2','blCn3','TownGlCat',
                     'sUsePr','bUsePr','prTxEx','landSize','closeDate','ValPdOrTrn')
    def _is_done(k, v):
        """Return True if this cached record should be skipped (already resolved)."""
        if v.get('lat') is None: return False          # no coords — re-process
        if not all(f in v for f in FILTER_FIELDS): return False  # missing fields
        if (v.get('method') or '') in MANUAL_METHODS: return True # manual — keep
        if (v.get('method') or '') == 'span_vacant': return True  # already SPAN-placed
        # Vacant land (blCn1 null/01): re-process to get SPAN coords
        b1 = str(v.get('blCn1') or '').strip().lstrip('0') or '0'
        if b1 in ('0', '1'): return False
        return True  # all other resolved records — skip

    already_done = set(k for k, v in existing.items() if _is_done(k, v))

    null_count = sum(1 for v in existing.values() if v.get('lat') is None
                     and (v.get('method') or '') not in MANUAL_METHODS)
    print(f"Records with null coords (will retry): {null_count:,}")

    # Load VT codes
    school_to_town, town_to_county, town_centroids, county_names, span_prefix_map = load_vt_codes()

    # Fetch approx records
    date_from_filter = args.date_from
    if date_from_filter:
        print(f"Date filter: only processing records with closeDate >= {date_from_filter}")
    approx_records = fetch_all_approx(school_to_town, town_to_county, span_prefix_map,
                                       date_from=date_from_filter)

    # Process new records AND null-coord records (retry with E911)
    to_process = [r for r in approx_records if str(r["objectid"]) not in already_done]

    print(f"\nRecords to process: {len(to_process):,}  (new + null-coord retries)")
    print(f"Already resolved:   {len(already_done):,}")

    if not to_process:
        print("Nothing new to process. File is up to date.")
        return

    print("-" * 60)

    e911_success = span_success = sibling_success = nom_success = nom_fail = skipped = 0

    for i, rec in enumerate(to_process, 1):
        oid          = str(rec["objectid"])
        span_raw     = rec.get("span")
        address      = rec["address"]
        geocode_town = rec.get("geocode_town", "")
        trusted_town = rec.get("trusted_town")
        city_display = rec.get("city") or (trusted_town.title() if trusted_town else "")
        county_code  = str(rec["trusted_county"]).zfill(2) if rec["trusted_county"] else None
        county_name  = county_names.get(county_code) if county_code else None

        print(f"[{i}/{len(to_process)}] {address}, {geocode_town} ... ", end="", flush=True)

        lat = lon = method = None

        # Detect vacant land: F3 (blCn1) null/zero/01/1 = no building declared
        b1 = str(rec.get('blCn1') or '').strip().lstrip('0') or '0'
        is_vacant_land = b1 in ('0', '1')

        # For vacant land: try SPAN parcel centroid FIRST.
        # Vacant land has no house number so E911 won't find it.
        # The SPAN parcel centroid places the dot inside the actual parcel
        # boundary which is more accurate than any address-based geocoding.
        if is_vacant_land and span_raw:
            lat, lon, _m = span_lookup(span_raw)
            if lat:
                method = 'span_vacant'  # distinct method for tracking
                span_success += 1
                print(f"SPAN/vacant ({lat:.5f}, {lon:.5f})")

        # Method 1: E911 address point lookup (most accurate for addressed properties)
        if lat is None and has_street_number(address):
            lat, lon, method = e911_lookup(address, geocode_town, town_centroids)
            if lat:
                e911_success += 1
                print(f"E911 ({lat:.5f}, {lon:.5f})")

        # Method 2: SPAN lookup (parcel centroid) for non-vacant land
        if lat is None and span_raw:
            lat, lon, method = span_lookup(span_raw)
            if lat:
                span_success += 1
                print(f"SPAN ({lat:.5f}, {lon:.5f})")

        # Method 3: ArcGIS sibling (condo/unit, no parcel record)
        if lat is None and has_street_number(address):
            sc_raw = rec.get('school_code_raw')
            try:
                sc_for_lookup = int(float(str(sc_raw))) if sc_raw is not None else 0
            except (TypeError, ValueError):
                sc_for_lookup = 0
            if sc_for_lookup > 0:
                lat, lon, method = arcgis_sibling_lookup(address, sc_for_lookup, COUNTY_BOUNDS)
                if lat:
                    sibling_success += 1
                    print(f"ArcGIS sibling ({lat:.5f}, {lon:.5f})")

        # Method 4: Nominatim (last resort for addressed properties)
        if lat is None:
            lat, lon, method = nominatim_geocode(address, geocode_town, town_centroids)
            if lat:
                nom_success += 1
                print(f"Nominatim ({lat:.5f}, {lon:.5f})")
            elif has_street_number(address):
                nom_fail += 1
                print("not found")
            else:
                skipped += 1
                print("skipped (no street number)")

        # For manual records already in the file, preserve the existing
        # coords and method — only update the filterable fields.
        prev = existing.get(oid, {})
        is_manual = (prev.get('method') or '') in MANUAL_METHODS
        existing[oid] = {
            "lat":               prev['lat'] if is_manual else lat,
            "lon":               prev['lon'] if is_manual else lon,
            "address":           address,
            "city":              city_display,
            "span":              str(rec.get('span') or '').strip() or None,
            "trustedTown":       trusted_town.title() if trusted_town else None,
            "trustedCountyCode": county_code,
            "trustedCountyName": county_name,
            "method":            prev['method'] if is_manual else method,
            "geocoded_at":       prev.get('geocoded_at', datetime.now().strftime("%Y-%m-%d")),
            # Filterable fields from ArcGIS — used by client-side filter
            "intPrpType":  rec.get("intPrpType"),
            "blCn1":       rec.get("blCn1"),
            "blCn2":       rec.get("blCn2"),
            "blCn3":       rec.get("blCn3"),
            "TownGlCat":   rec.get("TownGlCat"),
            "sUsePr":      rec.get("sUsePr"),
            "bUsePr":      rec.get("bUsePr"),
            "prTxEx":      rec.get("prTxEx"),
            "landSize":    rec.get("landSize"),
            "closeDate":   rec.get("closeDate"),
            "ValPdOrTrn":  rec.get("ValPdOrTrn"),
        }

        # Save every 25 records
        if i % 25 == 0:
            with open(OUTPUT_FILE, "w") as f:
                json.dump(existing, f, indent=2)
            print(f"  [saved {len(existing):,} records]")

    # Final save
    with open(OUTPUT_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    print("-" * 60)
    print(f"E911 resolved:      {e911_success:,}")
    print(f"SPAN resolved:      {span_success:,}")
    print(f"Sibling resolved:   {sibling_success:,}")
    print(f"Nominatim resolved: {nom_success:,}")
    print(f"Nominatim failed:   {nom_fail:,}")
    print(f"Skipped (no addr):  {skipped:,}")
    print(f"Total in file:      {len(existing):,}")
    print(f"File: {OUTPUT_FILE}")
    print()
    print("Next steps:")
    print("  git add geocoded_approx.json")
    print('  git commit -m "Update geocoded approx records"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
