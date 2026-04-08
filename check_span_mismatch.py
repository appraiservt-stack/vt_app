"""
check_span_mismatch.py
----------------------
Compares the SPAN entered by the preparer in Section C (field: span)
against the SPAN entered by the town clerk (field: TownSpan).

Reports all records where they don't match, which may indicate:
  - Data entry errors by preparer or clerk
  - Properties that were re-parceled or reassigned after the sale
  - Timeshare / condo master vs unit SPAN differences

Run from c:\\vt_app:
    python check_span_mismatch.py

Output: span_mismatches.csv
"""

import requests
import csv
import os
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "span_mismatches.csv")

PTT_URL = (
    "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"
)

def normalize_span(raw):
    """
    Normalize a SPAN to a consistent format for comparison.
    Handles both formats:
      - 11-digit raw:  44113911228
      - Dashed format: 441-139-11228
    Returns the dashed format, or None if invalid.
    """
    if not raw:
        return None
    s = str(raw).strip().replace("-", "").replace(" ", "")
    if not s.isdigit():
        return None
    if len(s) == 11:
        return f"{s[:3]}-{s[3:6]}-{s[6:]}"
    if len(s) == 10:
        # Some older records are 10 digits — pad to 11
        s = s.zfill(11)
        return f"{s[:3]}-{s[3:6]}-{s[6:]}"
    return None


def fetch_page(offset, page_size=2000):
    params = {
        "where":             "ValPdOrTrn > 0",
        "outFields":         "OBJECTID,span,TownSpan,propLocStr,propLocCty,TOWNNAME,closeDate,ValPdOrTrn",
        "f":                 "json",
        "outSR":             "4326",
        "resultRecordCount": page_size,
        "resultOffset":      offset,
    }
    r = requests.post(PTT_URL, data=params, timeout=30)
    return r.json()


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
    print("VT Property Sales — SPAN Mismatch Checker")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print("Fetching all records from ArcGIS (this may take a few minutes)...")

    mismatches = []
    total_fetched = 0
    offset = 0
    page_size = 2000

    while True:
        data = fetch_page(offset, page_size)
        if data.get("error"):
            print(f"\nArcGIS error: {data['error']}")
            break

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            a = feat["attributes"]
            span_c    = normalize_span(a.get("span"))      # Section C (preparer)
            span_town = normalize_span(a.get("TownSpan"))  # Town clerk

            # Skip if either SPAN is missing/invalid
            if not span_c or not span_town:
                continue

            # Skip if they match
            if span_c == span_town:
                continue

            mismatches.append({
                "OBJECTID":   a.get("OBJECTID"),
                "Section_C_SPAN":  span_c,
                "TownClerk_SPAN":  span_town,
                "Address":    a.get("propLocStr") or "",
                "City":       a.get("propLocCty") or "",
                "Town":       a.get("TOWNNAME") or "",
                "CloseDate":  fmt_date(a.get("closeDate")),
                "SalePrice":  fmt_price(a.get("ValPdOrTrn")),
            })

        total_fetched += len(features)
        print(f"  Fetched {total_fetched:,} records, {len(mismatches):,} mismatches so far...", end="\r")

        if len(features) < page_size:
            break
        offset += page_size
        time.sleep(0.1)

    print(f"\nTotal records checked: {total_fetched:,}")
    print(f"SPAN mismatches found: {len(mismatches):,}")

    if not mismatches:
        print("No mismatches found.")
        return

    # Write CSV
    fieldnames = ["OBJECTID", "Section_C_SPAN", "TownClerk_SPAN",
                  "Address", "City", "Town", "CloseDate", "SalePrice"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(mismatches)

    print(f"Output saved to: {OUTPUT_FILE}")
    print()
    print("Column definitions:")
    print("  Section_C_SPAN  — SPAN entered by preparer in Section C of PTT-172")
    print("  TownClerk_SPAN  — SPAN entered by town clerk")
    print("  A mismatch may indicate a data entry error, re-parceling, or")
    print("  a condo/timeshare where the unit SPAN differs from the land SPAN.")


if __name__ == "__main__":
    main()
