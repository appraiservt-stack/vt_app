import json

OUTPUT = 'c:/vt_app/geocoded_approx.json'

with open(OUTPUT) as f:
    data = json.load(f)

# ── Existing fixes ─────────────────────────────────────────────────────────────
data['213103'] = {
    'lat': 44.13364210000003,
    'lon': -72.66174629999995,
    'address': '76 VERMONT ROUTE 12A, UNIT #6',
    'city': 'Northfield',
    'span': '44113912112',
    'trustedTown': 'Northfield',
    'trustedCountyCode': '12',
    'trustedCountyName': 'Washington',
    'method': 'manual_sibling',
    'geocoded_at': '2026-04-04',
}

data['260586'] = {
    'lat': 44.13364210000003,
    'lon': -72.66174629999995,
    'address': '76 VERMONT ROUTE 12A, UNIT #4',
    'city': 'Northfield',
    'span': '44113912110',
    'trustedTown': 'Northfield',
    'trustedCountyCode': '12',
    'trustedCountyName': 'Washington',
    'method': 'manual_sibling',
    'geocoded_at': '2026-04-04',
}

# ── 2232 Vermont Route 14 — Randolph (filer omitted "North") ──────────────────
# SPAN 50715914288 (507-159-14288) does not exist in the VT parcel layer.
# Coords interpolated from neighboring parcel centroids on VT Route 14 North:
#   507-159-12800 → 2156 VT Route 14 North: 43.972439, -72.551826
#   507-159-14126 → 2248 VT Route 14 North: 43.971761, -72.553894
# 2232 is 82.6% of the way from 2156 to 2248.

# Nominatim returns exact house-number interpolated coords for this address:
# '2232, Vermont Route 14 North, North Randolph, Randolph, Orange County, Vermont'
# lat=43.9710045, lon=-72.5539551  (on the road, not parcel centroid)
data['163338'] = {
    'lat': 43.9710045,
    'lon': -72.5539551,
    'address': '2232 VERMONT ROUTE 14',
    'city': 'Randolph',
    'span': '50715914288',
    'trustedTown': 'Randolph',
    'trustedCountyCode': '09',
    'trustedCountyName': 'Orange',
    'method': 'manual_nominatim',
    'geocoded_at': '2026-04-05',
    'note': 'Filer omitted "North". Geocoded as 2232 VT Route 14 North, Randolph.',
}

data['282392'] = {
    'lat': 43.9710045,
    'lon': -72.5539551,
    'address': '2232 VERMONT ROUTE 14N',
    'city': 'Randolph',
    'span': '50715914288',
    'trustedTown': 'Randolph',
    'trustedCountyCode': '09',
    'trustedCountyName': 'Orange',
    'method': 'manual_nominatim',
    'geocoded_at': '2026-04-05',
    'note': 'Same parcel as OBJECTID 163338. Geocoded as 2232 VT Route 14 North, Randolph.',
}

# ── Lot 10 South Hill Road, Stockbridge — vacant land, no house number ────────
# Nominatim result for "South Hill Road, Stockbridge, Vermont":
#   lat=43.7595726, lon=-72.7825868  (road-level coords, Windsor County)
# isCentroid will be False since coords are now set — shown as orange dashed circle.
data['387151'] = {
    'lat': 43.7595726,
    'lon': -72.7825868,
    'address': 'LOT 10, SOUTH HILL ROAD',
    'city': 'Stockbridge',
    'trustedTown': 'Stockbridge',
    'trustedCountyCode': '14',
    'trustedCountyName': 'Windsor',
    'method': 'manual_nominatim',
    'geocoded_at': '2026-04-07',
    'note': 'Vacant land lot, no house number. Geocoded to road centerline via Nominatim.',
}

# ── OID 286176: Old Bailey Road, Cavendish — wrong schoolCode (044=Chelsea) ─
# ArcGIS has schoolCode='044' (Chelsea) but TOWNNAME='Cavendish'.
# trustedTown was set to Chelsea causing wrong centroid placement.
# Coords from ArcGIS geometry: lat=43.35583660000003, lon=-72.64897219999995
# (MatchMthod='Property Address (Composite)' so coords are reliable)
data['286176'] = {
    'lat': 43.35583660000003,
    'lon': -72.64897219999995,
    'address': 'OLD BAILEY ROAD',
    'city': 'Cavendish',
    'trustedTown': 'Cavendish',
    'trustedCountyCode': '14',
    'trustedCountyName': 'Windsor',
    'method': 'manual_sibling',
    'geocoded_at': '2026-04-09',
    'note': 'schoolCode=044 (Chelsea) is wrong in ArcGIS. TOWNNAME=Cavendish is correct.',
}

with open(OUTPUT, 'w') as f:
    json.dump(data, f, indent=2)

print('Done.')
print('  Fixed 213103 and 260586 (76 VT Route 12A units)')
print('  Fixed 163338 and 282392 (2232 VT Route 14 → Randolph)')
print('  Fixed 387151 (Lot 10 South Hill Road, Stockbridge)')
print('  Fixed 286176 (Old Bailey Road, Cavendish — wrong schoolCode 044=Chelsea)')
print('Total records:', len(data))
