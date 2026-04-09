"""
patch_missing_fields.py
-----------------------
One-time patch: finds approx cache records missing any of the 11 filterable
fields and fetches them from ArcGIS to fill the gaps.

Run from c:\\vt_app:
    python patch_missing_fields.py

After running:
    git add geocoded_approx.json
    git commit -m "Patch missing filterable fields in approx cache"
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

FILTER_FIELDS = (
    "intPrpType", "blCn1", "blCn2", "blCn3", "TownGlCat",
    "sUsePr", "bUsePr", "prTxEx", "landSize", "closeDate", "ValPdOrTrn"
)

FETCH_FIELDS = (
    "OBJECTID,intPrpType,blCn1,blCn2,blCn3,TownGlCat,"
    "sUsePr,bUsePr,prTxEx,landSize,closeDate,ValPdOrTrn"
)


def main():
    print("=" * 60)
    print("VT Property Sales — Patch Missing Filterable Fields")
    print("=" * 60)

    with open(OUTPUT_FILE) as f:
        data = json.load(f)
    print(f"Loaded {len(data):,} records from {OUTPUT_FILE}")

    # Find records missing any filterable field
    missing = [k for k, v in data.items()
               if any(field not in v for field in FILTER_FIELDS)]
    print(f"Records missing filterable fields: {len(missing)}")

    if not missing:
        print("Nothing to patch — all records have all fields.")
        return

    # Fetch the missing fields from ArcGIS in one POST request
    print(f"Fetching {len(missing)} records from ArcGIS...")
    id_list = ",".join(missing)

    r = requests.post(PTT_URL, data={
        "where":             f"OBJECTID IN ({id_list})",
        "outFields":         FETCH_FIELDS,
        "f":                 "json",
        "resultRecordCount": len(missing) + 10,
    }, timeout=30)

    features = r.json().get("features", [])
    print(f"ArcGIS returned {len(features)} records")

    patched = 0
    not_found = []

    for feat in features:
        a   = feat["attributes"]
        oid = str(a.get("OBJECTID"))
        if oid not in data:
            continue
        for field in FILTER_FIELDS:
            data[oid][field] = a.get(field)
        patched += 1

    # Records not returned by ArcGIS no longer exist — remove them
    returned_oids = {str(f["attributes"]["OBJECTID"]) for f in features}
    for oid in missing:
        if oid not in returned_oids:
            not_found.append(oid)
            print(f"  OID {oid} not in ArcGIS — removing from cache")
            del data[oid]

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nPatched:  {patched}")
    print(f"Removed (no longer in ArcGIS): {len(not_found)}")
    print(f"Total records: {len(data):,}")
    print(f"File saved: {OUTPUT_FILE}")
    print()
    print("Next steps:")
    print("  git add geocoded_approx.json")
    print('  git commit -m "Patch missing filterable fields in approx cache"')
    print("  git push origin main")


if __name__ == "__main__":
    main()
