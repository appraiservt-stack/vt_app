"""
remove_records.py
-----------------
Removes specific OBJECTIDs from geocoded_approx.json so they get
re-processed on the next geocode_approx.py run.

Usage:
    python remove_records.py

Run from c:\vt_app
"""

import json
import os

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'geocoded_approx.json')

# OBJECTIDs to remove — these are Unmatched records whose propLocCty is a
# village name (not an official VT town), causing wrong centroid placement.
# geocode_approx.py now has VILLAGE_TO_TOWN mapping so it will correctly
# constrain Nominatim to the parent town bbox on re-geocoding.
REMOVE = [
    # Village-name records: propLocCty is a village, not a town.
    # geocode_approx.py now has VILLAGE_TO_TOWN so re-geocoding will use correct bbox.
    152796,  # ASCUTNEY MOUNTAIN RESORT ROUTE 44, Brownsville -> West Windsor
    154663,  # ASCUTNEY MOUNTAIN RESORT ROUTE 44, Brownsville -> West Windsor
    160285,  # 4323 VT RTE 108S, Jeffersonville -> Cambridge
    160622,  # UNIT 2406 ORANGE LAKE, Brownsville -> West Windsor
    161127,  # ORANGE LAKE IN VERMONT, Brownsville -> West Windsor
    163144,  # PARCEL 2 MURPHY HILL, North Bennington -> Bennington
    165374,  # 485 HOTEL RD, Brownsville -> West Windsor
    166393,  # ASPENS AH13, Jeffersonville -> Cambridge
    168279,  # 4323 VERMONT ROUTE 108 SOUTH, Jeffersonville -> Cambridge
    168685,  # ?, Brownsville -> West Windsor
    168892,  # 59 COUNTRY SKYLINE BLVD, Ascutney -> Weathersfield
    171221,  # 485 HOTEL ROAD, Brownsville -> West Windsor
    173443,  # 10 NORTH SHORE DR, Bellows Falls -> Rockingham
    177101,  # ASCUTNEY MOUNTAIN RESORT, Brownsville -> West Windsor
    185967,  # TAMARADES TA11, Jeffersonville -> Cambridge
    187479,  # HILL ST BEANS HOMES, Lyndonville -> Lyndon
    315904,  # 10 N SHORE DR, Bellows Falls -> Rockingham (newer record)
    315908,  # 12 NORTH SHORE DRIVE, Bellows Falls -> Rockingham (newer record)

    # Miscoded schoolCode records: schoolCode maps to wrong town/county.
    # app.py now prefers propLocCty centroid when schoolCode county != propLocCty county.
    # Remove from cache so they re-geocode using corrected centroid logic.
    244617,  # 112 BEAVER HOLLOW RD, Londonderry — schoolCode=100->Isle La Motte (wrong)
    322859,  # ROUTE 9, Wilmington — schoolCode=246->Winooski (wrong), should use Wilmington centroid
]

with open(OUTPUT) as f:
    data = json.load(f)

before = len(data)
removed = []
for oid in REMOVE:
    key = str(oid)
    if key in data:
        del data[key]
        removed.append(oid)

with open(OUTPUT, 'w') as f:
    json.dump(data, f, indent=2)

print(f'Removed {len(removed)} records: {removed}')
print(f'Cache: {before:,} -> {len(data):,} records')
print()
print('Next step: run geocode_approx.py to re-geocode these records.')
print('They will now use VILLAGE_TO_TOWN mapping for correct bbox placement.')
