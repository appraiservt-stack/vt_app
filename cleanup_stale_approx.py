"""
cleanup_stale_approx.py
-----------------------
Removes records from geocoded_approx.json whose OBJECTID no longer exists
in the ArcGIS PTT layer. This prevents ghost dots for sales that have been
retired, corrected, or re-issued with a new OBJECTID in ArcGIS.

Also removes records with manual_* methods that are no longer in ArcGIS
(those should never be removed -- manual fixes are kept regardless).

Run from c:\\vt_app:
    python cleanup_stale_approx.py

After running:
    git add geocoded_approx.json
    git commit -m "Remove stale approx records no longer in ArcGIS"
    git push origin main
"""

import requests
import json
import os
import time

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "geocoded_approx.json")

PTT_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"
)

MANUAL_METHODS = {'manual_nominatim', 'manual_sibling'}
CHUNK_SIZE = 500  # OBJECTIDs per ArcGIS query (safe limit)


def fetch_existing_objectids(objectids):
    """
    Given a list of OBJECTIDs, return the subset that still exist in ArcGIS.
    Uses POST requests to avoid URL length limits (GET with 500 IDs exceeds
    ArcGIS server limits and returns an empty response, not an error).
    """
    existing = set()
    total = len(objectids)

    for i in range(0, total, CHUNK_SIZE):
        chunk = objectids[i:i + CHUNK_SIZE]
        id_list = ','.join(str(x) for x in chunk)
        # POST avoids URL length limits that silently break large GET requests
        payload = {
            "where":         f"OBJECTID IN ({id_list})",
            "outFields":     "OBJECTID",
            "returnIdsOnly": "true",
            "f":             "json",
        }
        try:
            r = requests.post(PTT_URL, data=payload, timeout=30)
            data = r.json()
            if data.get("error"):
                print(f"  ArcGIS error on chunk {i//CHUNK_SIZE + 1}: {data['error']}")
                continue
            ids = data.get("objectIds") or []
            existing.update(ids)
            print(f"  Checked {min(i+CHUNK_SIZE, total):,}/{total:,} — {len(existing):,} still exist", end="\r")
        except Exception as e:
            print(f"\n  Error on chunk {i//CHUNK_SIZE + 1}: {e} — skipping")
        time.sleep(0.2)

    print()
    return existing


def main():
    print("=" * 60)
    print("VT Property Sales — Stale Approx Record Cleanup")
    print("=" * 60)

    with open(OUTPUT_FILE) as f:
        data = json.load(f)

    print(f"Loaded {len(data):,} records from {OUTPUT_FILE}")

    # Separate manual records (always keep) from auto-geocoded
    manual_keys = {k for k, v in data.items()
                   if (v.get('method') or '') in MANUAL_METHODS}
    auto_keys   = [k for k in data if k not in manual_keys]

    print(f"  Manual records (kept regardless): {len(manual_keys):,}")
    print(f"  Auto-geocoded records to verify:  {len(auto_keys):,}")
    print()

    # Check which auto-geocoded OBJECTIDs still exist in ArcGIS
    objectids = [int(k) for k in auto_keys]
    print(f"Querying ArcGIS for {len(objectids):,} OBJECTIDs in chunks of {CHUNK_SIZE}...")
    still_exist = fetch_existing_objectids(objectids)

    # Find stale records
    stale_keys = [k for k in auto_keys if int(k) not in still_exist]
    print(f"\nStale records (no longer in ArcGIS): {len(stale_keys):,}")

    if not stale_keys:
        print("Nothing to remove — file is clean.")
        return

    print("Stale records being removed:")
    for k in stale_keys:
        v = data[k]
        print(f"  OBJECTID {k}: {v.get('address')!r}, {v.get('city')!r}  method={v.get('method')!r}")

    # Remove stale records
    for k in stale_keys:
        del data[k]

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nRemoved {len(stale_keys):,} stale records.")
    print(f"Remaining records: {len(data):,}")
    print(f"File saved: {OUTPUT_FILE}")
    print()
    print("Next steps:")
    print("  git add geocoded_approx.json")
    print('  git commit -m "Remove stale approx records no longer in ArcGIS"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
