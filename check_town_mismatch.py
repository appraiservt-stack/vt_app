"""
check_town_mismatch.py
----------------------
Compares the City/Town entered by the preparer in Section C (propLocCty)
against the City/Town entered by the town clerk (TownCtyOrT).

Reports all 805 records where they don't match, side by side with the
property address from Block C so you can identify the property.

Common mismatch patterns:
  - Village name vs town name (White River Junction / Hartford)
  - Spelling variants (Alburg / Alburgh)
  - Abbreviations (St Albans Town / Saint Albans Town)
  - City vs town (Rutland / Rutland City)
  - ? or blank in preparer field

ArcGIS does the comparison server-side so this runs in seconds.

Run from c:\\vt_app:
    python check_town_mismatch.py

Output: town_mismatches.csv
"""

import requests
import csv
import os
from datetime import datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "town_mismatches.csv")

PTT_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"
)

WHERE = (
    "ValPdOrTrn > 0 "
    "AND propLocCty IS NOT NULL "
    "AND TownCtyOrT IS NOT NULL "
    "AND UPPER(propLocCty) <> UPPER(TownCtyOrT)"
)

FIELDS = "OBJECTID,propLocStr,propLocCty,TownCtyOrT,span,TownSpan,closeDate,ValPdOrTrn,TOWNNAME"


def fmt_date(epoch_ms):
    if not epoch_ms:
        return ""
    try:
        return datetime.utcfromtimestamp(int(epoch_ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fmt_price(v):
    if v is None:
        return ""
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return str(v)


def main():
    print("=" * 60)
    print("VT Property Sales — Town Name Mismatch Checker")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ArcGIS filters server-side so we can fetch all mismatches in one call
    print("Fetching mismatches from ArcGIS...")
    all_records = []
    offset = 0
    page_size = 2000

    while True:
        r = requests.post(PTT_URL, data={
            "where":             WHERE,
            "outFields":         FIELDS,
            "f":                 "json",
            "resultRecordCount": page_size,
            "resultOffset":      offset,
        }, timeout=30)
        data = r.json()

        if data.get("error"):
            print(f"ArcGIS error: {data['error']}")
            break

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            a = feat["attributes"]
            all_records.append({
                "OBJECTID":         a.get("OBJECTID", ""),
                "Property_Address": a.get("propLocStr", ""),
                "BlockC_City_Town": a.get("propLocCty", ""),
                "TownClerk_City_Town": a.get("TownCtyOrT", ""),
                "ArcGIS_TOWNNAME":  a.get("TOWNNAME", ""),
                "BlockC_SPAN":      a.get("span", ""),
                "TownClerk_SPAN":   a.get("TownSpan", ""),
                "CloseDate":        fmt_date(a.get("closeDate")),
                "SalePrice":        fmt_price(a.get("ValPdOrTrn")),
            })

        print(f"  Retrieved {len(all_records):,} records so far...", end="\r")

        if len(features) < page_size:
            break
        offset += page_size

    print(f"\nTotal mismatches: {len(all_records):,}")

    if not all_records:
        print("No mismatches found.")
        return

    # Sort by BlockC town name for easier review
    all_records.sort(key=lambda x: (x["BlockC_City_Town"].upper(), x["TownClerk_City_Town"].upper()))

    fieldnames = [
        "OBJECTID", "Property_Address",
        "BlockC_City_Town", "TownClerk_City_Town", "ArcGIS_TOWNNAME",
        "BlockC_SPAN", "TownClerk_SPAN",
        "CloseDate", "SalePrice"
    ]

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_records)

    print(f"Output saved to: {OUTPUT_FILE}")
    print()
    print("Columns:")
    print("  Property_Address    — Address from Block C (preparer)")
    print("  BlockC_City_Town    — City/Town from Block C (preparer)")
    print("  TownClerk_City_Town — City/Town from town clerk section")
    print("  ArcGIS_TOWNNAME     — Town name assigned by VCGI from GPS coords")
    print("  BlockC_SPAN         — SPAN from Block C (preparer)")
    print("  TownClerk_SPAN      — SPAN from town clerk section")
    print()
    print("Common patterns to look for:")
    print("  Village vs town     (White River Junction / Hartford)")
    print("  Spelling variants   (Alburg / Alburgh)")
    print("  Abbreviations       (St Albans Town / Saint Albans Town)")
    print("  City vs town        (Rutland / Rutland City)")


if __name__ == "__main__":
    main()
