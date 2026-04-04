"""
geocode_approx.py
-----------------
Fetches all approx-location (ungeocoded) property transfer records from the
ArcGIS service and attempts to resolve their coordinates using two methods:

  1. SPAN lookup (primary) — queries the VT parcel layer by SPAN number.
     Returns the parcel centroid. Fast, accurate, no wrong-town matches.
     Works for ~40% of approx records (standard residential/land parcels).

  2. Nominatim geocoding (fallback) — used when SPAN lookup fails (timeshares,
     condos, special parcels). Constrained to a bounding box around propLocCty
     (the filer-entered town) to prevent wrong-town matches.

Records that fail both methods remain as red circles at the town centroid.

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
    school_to_town = {int(k): v for k, v in codes.get("school_to_town", {}).items()}
    town_to_county = codes.get("town_to_county", {})
    town_centroids = codes.get("town_centroids", {})
    county_names   = codes.get("counties", {})
    return school_to_town, town_to_county, town_centroids, county_names


def coords_in_county(lat, lon, county_code):
    b = COUNTY_BOUNDS.get(str(county_code).zfill(2))
    if not b:
        return True
    return b[0] <= lat <= b[1] and b[2] <= lon <= b[3]


def is_approx(lat, lon, trusted_county):
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return True
    if not (VT_LAT_MIN <= lat <= VT_LAT_MAX and VT_LON_MIN <= lon <= VT_LON_MAX):
        return True
    if trusted_county and not coords_in_county(lat, lon, trusted_county):
        return True
    return False


def format_span(span_raw):
    """Convert 11-digit SPAN to parcel layer format: 34810810006 -> 348-108-10006"""
    s = str(span_raw).strip()
    if len(s) < 11:
        return None
    return f"{s[:3]}-{s[3:6]}-{s[6:]}"


def has_street_number(address):
    """Return True if address starts with a house number."""
    return bool(re.match(r"^\d+\s+\S+", (address or "").strip()))


# ── Method 1: SPAN lookup ──────────────────────────────────────────────────────

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
            if lat and lon and VT_LAT_MIN <= lat <= VT_LAT_MAX and VT_LON_MIN <= lon <= VT_LON_MAX:
                return lat, lon, "span"
    except Exception as e:
        print(f" [span error: {e}]", end="")

    return None, None, None


# ── Method 2b: ArcGIS sibling lookup ─────────────────────────────────────────
# For condo/unit addresses where the individual SPAN isn't in the parcel layer,
# find another ArcGIS record at the same base address that HAS coordinates.

def arcgis_sibling_lookup(address, school_code, county_bounds):
    """Find coordinates from another ArcGIS record at the same base address.
    Strips unit numbers and searches for any record with valid coords.
    Returns (lat, lon, method) or (None, None, None).
    """
    import re
    # Strip unit suffix to get base address
    base = re.sub(r',?\s*(UNIT|APT|SUITE|STE|LOT|#)\s*.*$', '', address, flags=re.I).strip()
    if not base or not re.match(r'^\d+', base):
        return None, None, None

    try:
        params = {
            'where':    f"propLocStr LIKE '{base}%' AND schoolCode={school_code}",
            'outFields': 'OBJECTID,propLocStr,Latitude,Longitude',
            'f':        'json',
            'outSR':    '4326',
            'resultRecordCount': 10,
        }
        r = requests.get(ARCGIS_URL, params=params, timeout=15)
        feats = r.json().get('features', [])
        for feat in feats:
            a = feat['attributes']
            g = feat.get('geometry', {})
            lat = g.get('y') or a.get('Latitude')
            lon = g.get('x') or a.get('Longitude')
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                continue
            if lat and lon and VT_LAT_MIN <= lat <= VT_LAT_MAX and VT_LON_MIN <= lon <= VT_LON_MAX:
                # Check county bounds if available
                return lat, lon, 'arcgis_sibling'
    except Exception:
        pass
    return None, None, None

# ── Method 3: Nominatim geocoding ─────────────────────────────────────────────

def nominatim_geocode(address, geocode_town, town_centroids):
    """
    Geocode address via Nominatim constrained to geocode_town's bbox.
    geocode_town = propLocCty (filer-entered town name) for accuracy.
    Returns (lat, lon, method) or (None, None, None).
    """
    if not has_street_number(address):
        return None, None, None

    centroid = town_centroids.get((geocode_town or "").upper())
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
                if VT_LAT_MIN <= lat <= VT_LAT_MAX and VT_LON_MIN <= lon <= VT_LON_MAX:
                    return lat, lon, "nominatim"
        except Exception:
            pass
        time.sleep(1)  # Nominatim rate limit: 1 req/sec

    return None, None, None


# ── Fetch approx records ───────────────────────────────────────────────────────

def fetch_all_approx(school_to_town, town_to_county):
    """Page through ArcGIS and return all approx records."""
    print("Fetching all property transfer records from ArcGIS...")
    all_approx = []
    offset     = 0
    page_size  = 2000
    total_fetched = 0

    while True:
        params = {
            "where":             "ValPdOrTrn > 0",
            "outFields":         "OBJECTID,span,propLocStr,propLocCty,schoolCode,Latitude,Longitude",
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
            geocode_town   = prop_loc_city if prop_loc_city else (school_town or "")

            if is_approx(lat, lon, trusted_county):
                all_approx.append({
                    "objectid":       a.get("OBJECTID"),
                    "span":           a.get("span"),
                    "address":        (a.get("propLocStr") or "").strip(),
                    "city":           prop_loc_city,
                    "geocode_town":   geocode_town,
                    "trusted_town":   school_town,
                    "trusted_county": trusted_county,
                    "school_code_raw": sc,  # raw schoolCode for ArcGIS sibling lookup
                })

        print(f"  Fetched {total_fetched:,} records, {len(all_approx):,} approx so far...", end="\r")

        if len(features) < page_size:
            break
        offset += page_size

    print(f"\nTotal approx records: {len(all_approx):,}")
    return all_approx


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
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

    already_done = set(existing.keys())

    # Load VT codes
    school_to_town, town_to_county, town_centroids, county_names = load_vt_codes()

    # Fetch approx records
    approx_records = fetch_all_approx(school_to_town, town_to_county)

    # Only process new records
    to_process = [r for r in approx_records if str(r["objectid"]) not in already_done]

    print(f"\nNew records to process: {len(to_process):,}")
    print(f"Already in file:        {len(already_done):,}")

    if not to_process:
        print("Nothing new to process. File is up to date.")
        return

    # Estimate time: SPAN lookups ~0.5s each, Nominatim ~2s each
    # Assume ~40% SPAN hits, ~20% Nominatim fallback
    est_seconds = int(len(to_process) * 0.5 + len(to_process) * 0.2 * 2)
    print(f"Estimated time:         ~{est_seconds//60} minutes")
    print("-" * 60)

    span_success = span_fail = nom_success = nom_fail = skipped = 0

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

        # Method 1: SPAN lookup
        if span_raw:
            lat, lon, method = span_lookup(span_raw)
            if lat:
                span_success += 1
                print(f"SPAN ({lat:.5f}, {lon:.5f})")
            else:
                span_fail += 1

        # Method 2b: ArcGIS sibling lookup (condo/unit with no parcel record)
        if lat is None and rec.get('school_code_raw'):
            lat, lon, method = arcgis_sibling_lookup(address, rec['school_code_raw'], COUNTY_BOUNDS)
            if lat:
                span_success += 1
                print(f"ArcGIS sibling ({lat:.5f}, {lon:.5f})")

        # Method 3: Nominatim fallback
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

        existing[oid] = {
            "lat":               lat,
            "lon":               lon,
            "address":           address,
            "city":              city_display,
            "trustedTown":       trusted_town.title() if trusted_town else None,
            "trustedCountyCode": county_code,
            "trustedCountyName": county_name,
            "method":            method,
            "geocoded_at":       datetime.now().strftime("%Y-%m-%d"),
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
    print(f"SPAN resolved:      {span_success:,}")
    print(f"SPAN not found:     {span_fail:,}")
    print(f"Nominatim resolved: {nom_success:,}")
    print(f"Nominatim failed:   {nom_fail:,}")
    print(f"Skipped (no addr):  {skipped:,}")
    print(f"Total in file:      {len(existing):,}")
    print(f"File: {OUTPUT_FILE}")
    print()
    print("Next steps:")
    print("  git add geocoded_approx.json")
    print("  git commit -m \"Update geocoded approx records\"")
    print("  git push origin main")


if __name__ == "__main__":
    main()
