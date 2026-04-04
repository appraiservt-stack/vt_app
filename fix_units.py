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

data['163338'] = {
    'lat': 43.971879,
    'lon': -72.553534,
    'address': '2232 VERMONT ROUTE 14',
    'city': 'Randolph',
    'span': '50715914288',
    'trustedTown': 'Randolph',
    'trustedCountyCode': '09',
    'trustedCountyName': 'Orange',
    'method': 'manual_interpolated',
    'geocoded_at': '2026-04-05',
    'note': 'Filer omitted "North". Placed at interpolated position on VT Route 14 North in Randolph.',
}

data['282392'] = {
    'lat': 43.971879,
    'lon': -72.553534,
    'address': '2232 VERMONT ROUTE 14N',
    'city': 'Randolph',
    'span': '50715914288',
    'trustedTown': 'Randolph',
    'trustedCountyCode': '09',
    'trustedCountyName': 'Orange',
    'method': 'manual_interpolated',
    'geocoded_at': '2026-04-05',
    'note': 'Same parcel as OBJECTID 163338 (duplicate sale). Placed at interpolated position on VT Route 14 North in Randolph.',
}

with open(OUTPUT, 'w') as f:
    json.dump(data, f, indent=2)

print('Done.')
print('  Fixed 213103 and 260586 (76 VT Route 12A units)')
print('  Fixed 163338 and 282392 (2232 VT Route 14 → Randolph)')
print('Total records:', len(data))
