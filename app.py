from flask import Flask, render_template, request, jsonify, Response
import requests
import json
import csv
import io
import math
import re
from pathlib import Path
from datetime import datetime, timezone

app = Flask(__name__)

ARCGIS_URL = (
    "https://services1.arcgis.com/"
    "BkFxaEFNwHqX3tAw/arcgis/rest/services/"
    "FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"
)

# -----------------------------
# Load Vermont code mappings
# -----------------------------
CODES_PATH = Path(__file__).with_name("vt_codes.json")

with CODES_PATH.open() as f:
    VT_CODES = json.load(f)

COUNTY_CODE_TO_NAME = VT_CODES["counties"]
TOWN_TO_COUNTY      = VT_CODES["town_to_county"]
SCHOOL_TO_TOWN      = {int(k): v for k, v in VT_CODES["school_to_town"].items()}

# Grand List Category lookup: numeric code → description text
GL_CATEGORY_LOOKUP   = VT_CODES.get("grand_list_categories", {})
# Building type lookup: numeric code → description text
BUILDING_TYPE_LOOKUP = VT_CODES.get("building_types", {})
# Town centroid fallback: town name (UPPERCASE) → {lat, lon, county}
# Used when a record's ArcGIS coordinates fail the county bounds check.
TOWN_CENTROIDS = VT_CODES.get("town_centroids", {})

# Approximate bounding boxes for each Vermont county (lat_min, lat_max, lon_min, lon_max)
# Used to validate that a record's ArcGIS coordinates actually fall in the county it reports.
# Records whose coordinates land outside their county's box are treated as null-coordinate
# (no map marker plotted) to prevent them from appearing in the wrong county.
COUNTY_BOUNDS = {
    "01": (43.70, 44.40, -73.50, -72.75),  # Addison
    "02": (42.70, 43.35, -73.50, -72.75),  # Bennington
    "03": (44.25, 45.05, -72.40, -71.40),  # Caledonia
    "04": (44.25, 45.05, -73.35, -72.65),  # Chittenden
    "05": (44.35, 45.05, -72.20, -71.40),  # Essex
    "06": (44.55, 45.05, -73.35, -72.45),  # Franklin
    "07": (44.55, 45.10, -73.50, -73.05),  # Grand Isle
    "08": (44.35, 44.85, -73.05, -72.25),  # Lamoille
    "09": (43.65, 44.40, -72.75, -71.95),  # Orange
    "10": (44.45, 45.05, -72.60, -71.85),  # Orleans
    "11": (43.25, 43.95, -73.50, -72.45),  # Rutland
    "12": (43.95, 44.65, -73.00, -72.20),  # Washington
    "13": (42.70, 43.35, -72.90, -71.95),  # Windham
    "14": (43.25, 44.15, -72.80, -72.05),  # Windsor
}


def coords_valid_for_county(lat, lon, county_code):
    """Return True if (lat, lon) falls within the expected bounding box for county_code."""
    if lat is None or lon is None:
        return False
    code = str(county_code).strip().zfill(2)
    bounds = COUNTY_BOUNDS.get(code)
    if bounds is None:
        return True   # Unknown county — don't discard
    lat_min, lat_max, lon_min, lon_max = bounds
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def resolve_coordinates(raw_lat, raw_lon, county_code, trusted_town):
    """Return (lat, lon, approx) for a record.

    - If ArcGIS coordinates are valid for the county: return them, approx=False.
    - If invalid (geocoding error in source data): fall back to the town centroid,
      approx=True so the front-end can show a distinct marker style.
    - If no centroid available: return (None, None, False).
    """
    if coords_valid_for_county(raw_lat, raw_lon, county_code):
        return raw_lat, raw_lon, False

    # Bad coordinates — look up the town centroid as a fallback
    town_key = (trusted_town or "").strip().upper()
    centroid  = TOWN_CENTROIDS.get(town_key)
    if centroid:
        return centroid["lat"], centroid["lon"], True

    # No centroid found either — return null so no marker is plotted
    return None, None, False


def derive_trusted_location(attr):
    raw_county  = attr.get("countyCode")
    school_code = attr.get("schoolCode")
    town_code   = attr.get("townCode")
    span        = attr.get("span")
    prop_city   = (
        attr.get("propertyLocationCity")
        or attr.get("propLocCty")
        or attr.get("TownCityorTown")
    )

    trusted_town   = None
    trusted_county = None

    if school_code is not None:
        try:
            sc_int = int(school_code)
        except (TypeError, ValueError):
            sc_int = None
        if sc_int is not None:
            trusted_town = SCHOOL_TO_TOWN.get(sc_int)

    if trusted_town:
        trusted_county = TOWN_TO_COUNTY.get(trusted_town)

    if trusted_county is None and raw_county is not None:
        trusted_county = str(raw_county).zfill(2)

    corrected = False
    if raw_county is not None and trusted_county is not None:
        corrected = str(raw_county).zfill(2) != str(trusted_county).zfill(2)

    trusted_county_name = None
    if trusted_county is not None:
        trusted_county_name = COUNTY_CODE_TO_NAME.get(str(trusted_county).zfill(2))

    return {
        "rawCountyCode":      raw_county,
        "trustedCountyCode":  trusted_county,
        "trustedCountyName":  trusted_county_name,
        "trustedTown":        trusted_town,
        "schoolCode":         school_code,
        "townCode":           town_code,
        "span":               span,
        "displayCity":        prop_city,
        "correctedCounty":    corrected,
    }


def parse_filters(args):
    """Parse all filter query parameters into a dict."""
    f = {}

    # --- Location ---
    f["counties"]     = args.get("counties", "")       # comma-separated codes
    f["towns"]        = args.get("towns", "")           # comma-separated town names

    # --- Date range ---
    f["date_from"]    = args.get("date_from", "")       # ISO date string YYYY-MM-DD
    f["date_to"]      = args.get("date_to", "")

    # --- Price range ---
    f["price_low"]    = args.get("price_low", "")
    f["price_high"]   = args.get("price_high", "")

    # --- Land size range ---
    f["land_low"]     = args.get("land_low", "")
    f["land_high"]    = args.get("land_high", "")

    # --- Street address (contains) ---
    f["street"]       = args.get("street", "").strip()

    # --- Interest type (multi, pipe-separated codes) ---
    f["interest"]     = args.get("interest", "")        # e.g. "1|2"

    # --- Building construction (multi, pipe-separated codes) ---
    f["building"]     = args.get("building", "")        # e.g. "2|3"

    # --- Seller use of property (multi, pipe-separated codes) ---
    f["seller_use"]   = args.get("seller_use", "")

    # --- Buyer use of property (multi, pipe-separated codes) ---
    f["buyer_use"]    = args.get("buyer_use", "")

    # --- PTT Exemption (multi, pipe-separated codes) ---
    f["ptt_exemption"] = args.get("ptt_exemption", "")

    # --- Grand List Category (multi, pipe-separated codes) ---
    f["grand_list"]   = args.get("grand_list", "")

    # --- Seller name fields ---
    f["seller_entity"] = args.get("seller_entity", "").strip()
    f["seller_last"]   = args.get("seller_last", "").strip()
    f["seller_first"]  = args.get("seller_first", "").strip()

    # --- Buyer name fields ---
    f["buyer_entity"]  = args.get("buyer_entity", "").strip()
    f["buyer_last"]    = args.get("buyer_last", "").strip()
    f["buyer_first"]   = args.get("buyer_first", "").strip()

    # --- SPAN ---
    f["span"]          = args.get("span", "").strip().replace("-", "")

    # --- Boolean radio buttons ---
    f["dev_prev_conv"]         = args.get("dev_prev_conv", "")    # "true"/"false"/""
    f["buyer_adjoining"]       = args.get("buyer_adjoining", "")
    f["enrolled_current_use"]  = args.get("enrolled_current_use", "")  # "true"/"false"/""
    f["foreclosed"]            = args.get("foreclosed", "")            # "true"/"false"/""

    return f


def build_where_clause(filters):
    """Build the ArcGIS WHERE clause from filters.

    All filters are pushed to ArcGIS SQL so the server does the heavy
    lifting across 218K+ records.  Python post-filtering is only used for
    the county-bounds coordinate validation (not a data filter).
    """
    clauses = ["1=1"]

    # County filter
    if filters["counties"]:
        codes = filters["counties"].split(",")
        codes_sql = ",".join([f"'{c}'" for c in codes])
        clauses.append(f"countyCode IN ({codes_sql})")

    # Date range — ArcGIS requires DATE 'YYYY-MM-DD' format
    if filters["date_from"]:
        try:
            datetime.strptime(filters["date_from"], "%Y-%m-%d")
            clauses.append(f"closeDate >= DATE '{filters['date_from']}'")
        except Exception:
            pass

    if filters["date_to"]:
        try:
            datetime.strptime(filters["date_to"], "%Y-%m-%d")
            clauses.append(f"closeDate <= DATE '{filters['date_to']}'")
        except Exception:
            pass

    # Price range — actual field name: ValPdOrTrn
    if filters["price_low"]:
        try:
            clauses.append(f"ValPdOrTrn >= {float(filters['price_low'])}")
        except Exception:
            pass

    if filters["price_high"]:
        try:
            clauses.append(f"ValPdOrTrn <= {float(filters['price_high'])}")
        except Exception:
            pass

    # Land size range — actual field name: landSize
    if filters["land_low"]:
        try:
            clauses.append(f"landSize >= {float(filters['land_low'])}")
        except Exception:
            pass

    if filters["land_high"]:
        try:
            clauses.append(f"landSize <= {float(filters['land_high'])}")
        except Exception:
            pass

    # Interest type — actual field name: intPrpType
    if filters["interest"]:
        codes = filters["interest"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"intPrpType IN ({codes_sql})")

    # Building construction — actual field name: blCn1
    if filters["building"]:
        codes = filters["building"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"blCn1 IN ({codes_sql})")

    # Seller use of property — actual field name: sUsePr
    if filters["seller_use"]:
        codes = filters["seller_use"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"sUsePr IN ({codes_sql})")

    # Buyer use of property — actual field name: bUsePr
    if filters["buyer_use"]:
        codes = filters["buyer_use"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"bUsePr IN ({codes_sql})")

    # PTT exemption — actual field name: prTxEx
    if filters["ptt_exemption"]:
        codes = filters["ptt_exemption"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"prTxEx IN ({codes_sql})")

    # Grand list category — actual field name: TownGlCat
    if filters["grand_list"]:
        codes = filters["grand_list"].split("|")
        codes_sql = ",".join(codes)
        clauses.append(f"TownGlCat IN ({codes_sql})")

    # ---------------------------------------------------------------
    # Name / SPAN / Street filters — pushed into SQL LIKE so ArcGIS
    # searches all 218K+ records efficiently instead of relying on
    # Python post-filtering across paginated statewide results.
    # ---------------------------------------------------------------

    # Helper: escape single-quotes in user input for SQL safety
    def sql_like(val):
        return val.replace("'", "''").upper()

    seller_last   = filters.get("seller_last",   "").strip()
    seller_first  = filters.get("seller_first",  "").strip()
    seller_entity = filters.get("seller_entity", "").strip()
    buyer_last    = filters.get("buyer_last",    "").strip()
    buyer_first   = filters.get("buyer_first",   "").strip()
    buyer_entity  = filters.get("buyer_entity",  "").strip()

    has_seller_name = bool(seller_last or seller_first or seller_entity)
    has_buyer_name  = bool(buyer_last  or buyer_first  or buyer_entity)

    if has_seller_name and has_buyer_name:
        # Both sides filled — OR: match seller criteria OR buyer criteria
        seller_parts = []
        if seller_last:   seller_parts.append(f"UPPER(sellLstNam) LIKE '%{sql_like(seller_last)}%'")
        if seller_first:  seller_parts.append(f"UPPER(sellFstNam) LIKE '%{sql_like(seller_first)}%'")
        if seller_entity: seller_parts.append(f"UPPER(sellEntNam) LIKE '%{sql_like(seller_entity)}%'")
        buyer_parts = []
        if buyer_last:    buyer_parts.append(f"UPPER(buyLstNam) LIKE '%{sql_like(buyer_last)}%'")
        if buyer_first:   buyer_parts.append(f"UPPER(buyFstNam) LIKE '%{sql_like(buyer_first)}%'")
        if buyer_entity:  buyer_parts.append(f"UPPER(buyEntNam) LIKE '%{sql_like(buyer_entity)}%'")
        seller_sql = " AND ".join(seller_parts)
        buyer_sql  = " AND ".join(buyer_parts)
        clauses.append(f"(({seller_sql}) OR ({buyer_sql}))")
    elif has_seller_name:
        if seller_last:   clauses.append(f"UPPER(sellLstNam) LIKE '%{sql_like(seller_last)}%'")
        if seller_first:  clauses.append(f"UPPER(sellFstNam) LIKE '%{sql_like(seller_first)}%'")
        if seller_entity: clauses.append(f"UPPER(sellEntNam) LIKE '%{sql_like(seller_entity)}%'")
    elif has_buyer_name:
        if buyer_last:    clauses.append(f"UPPER(buyLstNam) LIKE '%{sql_like(buyer_last)}%'")
        if buyer_first:   clauses.append(f"UPPER(buyFstNam) LIKE '%{sql_like(buyer_first)}%'")
        if buyer_entity:  clauses.append(f"UPPER(buyEntNam) LIKE '%{sql_like(buyer_entity)}%'")

    # Street address — pushed to SQL
    if filters.get("street", "").strip():
        clauses.append(f"UPPER(propLocStr) LIKE '%{sql_like(filters['street'].strip())}%'")

    # SPAN — pushed to SQL (strip dashes before comparing)
    span_val = filters.get("span", "").strip().replace("-", "")
    if span_val:
        clauses.append(f"CAST(span AS VARCHAR(20)) LIKE '%{sql_like(span_val)}%'")

    # ---------------------------------------------------------------
    # Town filter — convert town names to schoolCodes for SQL IN()
    # schoolCode is the most reliable town identifier in the ArcGIS data.
    # ---------------------------------------------------------------
    if filters.get("towns", "").strip():
        requested_towns = [t.strip().upper() for t in filters["towns"].split(",") if t.strip()]
        school_codes = []
        for town in requested_towns:
            # SCHOOL_TO_TOWN maps code->town; we need town->code(s)
            for code, mapped_town in SCHOOL_TO_TOWN.items():
                if mapped_town.upper() == town:
                    school_codes.append(str(code))
        if school_codes:
            codes_sql = ",".join([f"'{c}'" for c in school_codes])
            clauses.append(f"schoolCode IN ({codes_sql})")
        else:
            # No matching school codes found — force zero results rather
            # than returning unfiltered statewide data
            clauses.append("1=0")

    # ---------------------------------------------------------------
    # Boolean / radio flag filters — all pushed to SQL
    # Fields store uppercase strings 'TRUE' / 'FALSE'.
    # ---------------------------------------------------------------
    def bool_clause(field, value):
        """Return SQL clause for a boolean string field given 'true'/'false'."""
        if value == "true":
            return f"UPPER({field}) = 'TRUE'"
        elif value == "false":
            return f"UPPER({field}) = 'FALSE'"
        return None

    for flt_key, field in [
        ("dev_prev_conv",       "devPrevCnv"),
        ("buyer_adjoining",     "buyrAdjPrp"),
        ("enrolled_current_use", "enrCrntUse"),
    ]:
        clause = bool_clause(field, filters.get(flt_key, ""))
        if clause:
            clauses.append(clause)

    # Foreclosed — LGTEx code '6' means foreclosed sale
    fc = filters.get("foreclosed", "")
    if fc == "true":
        clauses.append("LGTEx = '6'")
    elif fc == "false":
        clauses.append("LGTEx <> '6'")

    return " AND ".join(clauses)


def apply_python_filters(record, filters):
    """All filters are now handled by ArcGIS SQL in build_where_clause.
    This function is retained as a no-op stub so call sites don't need
    to change.  The only remaining Python-side logic is the county-bounds
    coordinate validation in resolve_coordinates(), which is not a data
    filter but a geocoding quality check.
    """
    return True


def fetch_features(where, geometry_params=None, max_records=2000):
    """Fetch features from ArcGIS. geometry_params is optional bbox dict."""
    params = {
        "where":             where,
        "outFields":         "*",
        "f":                 "json",
        "outSR":             "4326",
        "resultRecordCount": max_records,
    }
    if geometry_params:
        params.update(geometry_params)
    else:
        params["inSR"] = "4326"

    r = requests.get(ARCGIS_URL, params=params)
    return r.json().get("features", [])


def fetch_all_features(where):
    """
    Page through ArcGIS results to get ALL matching records
    (for statewide export, no viewport limit).
    Uses resultOffset pagination.
    """
    all_features = []
    offset = 0
    page_size = 2000

    while True:
        params = {
            "where":             where,
            "outFields":         "*",
            "f":                 "json",
            "outSR":             "4326",
            "resultRecordCount": page_size,
            "resultOffset":      offset,
        }
        r = requests.get(ARCGIS_URL, params=params)
        data = r.json()
        features = data.get("features", [])
        all_features.extend(features)

        if len(features) < page_size:
            break
        if not data.get("exceededTransferLimit", True) and len(features) < page_size:
            break

        offset += page_size

    return all_features


def feature_to_record(f, filters):
    """Convert a raw ArcGIS feature to our record dict. Returns None if filtered out."""
    attr = f["attributes"]
    loc_info = derive_trusted_location(attr)

    # ---------------------------------------------------------
    # buildingConstruction1Desc fallback:
    # If blCn1Desc is blank AND blCn3 is 4 or "04", copy blCn3Desc
    # ---------------------------------------------------------
    bl_cn1_desc = attr.get("blCn1Desc") or ""
    bl_cn3      = attr.get("blCn3")
    bl_cn3_desc = attr.get("blCn3Desc") or ""
    if not bl_cn1_desc.strip():
        try:
            if str(bl_cn3).strip().lstrip("0") == "4":
                bl_cn1_desc = bl_cn3_desc
        except Exception:
            pass

    record = {
        # Location (trusted)
        "trustedTown":        loc_info["trustedTown"],
        "trustedCountyCode":  loc_info["trustedCountyCode"],
        "trustedCountyName":  loc_info["trustedCountyName"],
        "correctedCounty":    loc_info["correctedCounty"],

        # Map display
        # resolve_coordinates validates the ArcGIS coords against the county
        # bounding box. If they're wrong it falls back to the town centroid
        # and sets approxLocation=True so the front-end can render those
        # markers differently (hollow circle with a note in the popup).
        "id":      attr.get("OBJECTID"),
        "address": attr.get("propLocStr"),
        "city":    loc_info["displayCity"],
        "price":   attr.get("ValPdOrTrn") or 0,
        "date":    attr.get("closeDate"),
        **dict(zip(
            ("lat", "lon", "approxLocation"),
            resolve_coordinates(
                attr.get("Latitude"), attr.get("Longitude"),
                loc_info["rawCountyCode"],
                loc_info["trustedTown"]
            )
        )),

        # Export columns — using actual abbreviated field names from ArcGIS service
        "sellerEntityName":            attr.get("sellEntNam"),
        "sellerLastName":              attr.get("sellLstNam"),
        "sellerFirstName":             attr.get("sellFstNam"),
        "buyerEntityName":             attr.get("buyEntNam"),
        "buyerLastName":               attr.get("buyLstNam"),
        "buyerFirstName":              attr.get("buyFstNam"),
        "propertyLocationStreet":      attr.get("propLocStr"),
        "propertyLocationCity":        attr.get("propLocCty"),
        "countyCode":                  loc_info["rawCountyCode"],
        "landSize":                    attr.get("landSize"),
        "span":                        loc_info["span"],
        "townCode":                    loc_info["townCode"],
        "schoolCode":                  loc_info["schoolCode"],
        "closingDate":                 attr.get("closeDate"),
        "dateSellerAcquired":          attr.get("SellerAcq"),
        "propertyTaxExemption":        attr.get("prTxEx"),
        "propertyTaxExemptionDesc":    attr.get("prTxExDesc"),
        "familyMemberDesc":            attr.get("famMemDesc"),
        "LGTExemption":                attr.get("LGTEx"),
        "LGTExemptionDesc":            attr.get("LGTExDesc"),
        "interestPropertyType":        attr.get("intPrpType"),
        "interestUndivPercentDesc":    attr.get("intUDPdesc"),
        "interestUndivPercent":        attr.get("intUDP"),
        "interestPropertyTypeOther":   attr.get("intPrTypOt"),
        "buildingConstruction1":       attr.get("blCn1"),
        "buildingConstruction1Desc":   bl_cn1_desc,
        "buildingConstruction20":      attr.get("blConstr20"),
        "buildingConstructionDwellingUnits06": attr.get("bCnDUs06"),
        "tenantPurchase":              attr.get("tenantPrch"),
        "financing":                   attr.get("financing"),
        "enrolledCurrentUse":          attr.get("enrCrntUse"),
        "currentUseEnrollmentContinue": attr.get("cUseEnCont"),
        "sellerUseOfProperty":         attr.get("sUsePr"),
        "sellerUseOfPropertyDesc":     attr.get("sUsePrDesc"),
        "sellerUseOfPropertyExplain":  attr.get("sUsePrExpl"),
        "buyerUseOfProperty":          attr.get("bUsePr"),
        "buyerUseOfPropertyDesc":      attr.get("bUsePrDesc"),
        "buyerUseOfPropertyExplain":   attr.get("bUsePrExpl"),
        "rentedBefore":                attr.get("rntdBefore"),
        "rentedAfter":                 attr.get("rntdAfter"),
        "developmentPrevConv":         attr.get("devPrevCnv"),
        "buyerAdjoiningProperty":      attr.get("buyrAdjPrp"),
        "ValuePaidOrTransferred":      attr.get("ValPdOrTrn"),
        "PersonalPropValuePaidOrTrans": attr.get("PrPrVlPdTr"),
        "RealPropValuePaidOrTrans":    attr.get("RlPrVlPdTr"),
        "TownBookNumber":              attr.get("TownBkNum"),
        "TownPageNumber":              attr.get("TownPgNum"),
        "TownCityorTown":              attr.get("TownCtyOrT"),
        "TownParcelIDNo":              attr.get("TownParcID"),
        "TownDateOfRecord":            attr.get("TownDteRec"),
        "TownGrandListCategory":       attr.get("TownGlCat"),
        "TownSubdivision":             attr.get("TownSubdiv"),
        # Export lat/lon: use resolved coords (centroid fallback if source coords are bad)
        "Latitude":                    resolve_coordinates(
                                           attr.get("Latitude"), attr.get("Longitude"),
                                           loc_info["rawCountyCode"],
                                           loc_info["trustedTown"])[0],
        "Longitude":                   resolve_coordinates(
                                           attr.get("Latitude"), attr.get("Longitude"),
                                           loc_info["rawCountyCode"],
                                           loc_info["trustedTown"])[1],
        "additionalSellerNames":       attr.get("addSellNam"),
        "additionalBuyerNames":        attr.get("addBuyrNam"),
    }

    if not apply_python_filters(record, filters):
        return None

    return record


def format_epoch_date(epoch_ms):
    """Convert epoch milliseconds to MM/DD/YYYY string."""
    if epoch_ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return str(epoch_ms)


def format_span(raw_span):
    """Format SPAN as XXX-XXX-XXXXX."""
    if raw_span is None:
        return ""
    s = str(int(raw_span)).zfill(11) if str(raw_span).isdigit() else str(raw_span)
    s = s.replace("-", "").zfill(11)
    if len(s) >= 11:
        return f"{s[0:3]}-{s[3:6]}-{s[6:11]}"
    return s


def format_boolean(val):
    """Convert ArcGIS string boolean ('TRUE'/'FALSE') to Yes / No."""
    if val is None or val == "":
        return "No"
    s = str(val).strip().upper()
    if s == "TRUE":
        return "Yes"
    return "No"  # 'FALSE', '0', blank, etc.


def format_interest_percent(val):
    """If zero, blank, or None → show 100%. Otherwise format as percentage."""
    if val is None or val == "" or val == 0:
        return "100%"
    try:
        f = float(val)
        if f == 0:
            return "100%"
        return f"{f:g}%"
    except Exception:
        return str(val)


def format_building_type(raw_desc):
    """Remove numeric code prefix from building type description.
    e.g. '02. Single Family Dwelling' → 'Single Family Dwelling'
    """
    if not raw_desc:
        return ""
    cleaned = re.sub(r"^\d+\.?\s*", "", str(raw_desc)).strip()
    return cleaned


def format_grand_list_category(code_val):
    """Look up Grand List Category description from numeric code."""
    if code_val is None or code_val == "":
        return ""
    try:
        code_str = str(int(float(str(code_val))))
        desc = GL_CATEGORY_LOOKUP.get(code_str) or GL_CATEGORY_LOOKUP.get(str(code_val))
        if desc:
            return desc
        return str(code_val)
    except Exception:
        return str(code_val)


def time_since_last_sale(epoch_ms):
    """Return 'X years Y months' between closeDate and today. No days."""
    if epoch_ms is None or epoch_ms == "":
        return ""
    try:
        close_dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
        today    = datetime.now(tz=timezone.utc)

        years  = today.year  - close_dt.year
        months = today.month - close_dt.month

        if months < 0:
            years  -= 1
            months += 12

        parts = []
        if years > 0:
            parts.append(f"{years} year{'s' if years != 1 else ''}")
        if months > 0 or years == 0:
            parts.append(f"{months} month{'s' if months != 1 else ''}")
        return " ".join(parts)
    except Exception:
        return ""


# -----------------------------------------------------------------------
# Export column order and display names
# -----------------------------------------------------------------------
EXPORT_COLUMNS = [
    ("dateSellerAcquired",          "Date Seller Acquired"),
    ("timeSinceLastSale",           "Time Since Last Sale"),
    ("span",                        "SPAN"),
    ("TownParcelIDNo",              "Town Parcel ID"),
    ("closingDate",                 "Closing Date"),
    ("ValuePaidOrTransferred",      "Price Paid"),
    ("PersonalPropValuePaidOrTrans","Personal Prop Val"),
    ("RealPropValuePaidOrTrans",    "Real Property Value"),
    ("propertyLocationStreet",      "Property Street"),
    ("propertyLocationCity",        "City"),
    ("landSize",                    "Land Size (acres)"),
    ("buildingConstruction1Desc",   "Building Type"),
    ("buildingConstructionDwellingUnits06", "Dwelling Units"),
    ("buildingConstruction20",      "Other Uses"),
    ("sellerUseOfPropertyDesc",     "Seller Use of Property"),
    ("buyerUseOfPropertyDesc",      "Buyer Use of Property"),
    ("interestUndivPercentDesc",    "Interest Type"),
    ("buyerLastName",               "Buyer Last Name"),
    ("buyerFirstName",              "Buyer First Name"),
    ("buyerEntityName",             "Buyer Entity Name"),
    ("sellerLastName",              "Seller Last Name"),
    ("sellerFirstName",             "Seller First Name"),
    ("sellerEntityName",            "Seller Entity Name"),
    ("trustedCountyName",           "County"),
    ("TownCityorTown",              "Property City"),
    ("trustedTown",                 "Town"),
    ("interestUndivPercent",        "Interest Percent"),
    ("propertyTaxExemptionDesc",    "PTT Exemption"),
    ("LGTExemptionDesc",            "LGT Exemption"),
    ("familyMemberDesc",            "Family Member Relationship"),
    ("financing",                   "Financing"),
    ("enrolledCurrentUse",          "Enrolled Current Use"),
    ("currentUseEnrollmentContinue","Current Use Continues"),
    ("tenantPurchase",              "Tenant Purchase"),
    ("rentedBefore",                "Rented Before"),
    ("rentedAfter",                 "Rented After"),
    ("developmentPrevConv",         "Development Rights Previously Conveyed"),
    ("buyerAdjoiningProperty",      "Buyer Owns Adjoining Property"),
    ("TownBookNumber",              "Town Book Number"),
    ("TownPageNumber",              "Town Page Number"),
    ("TownDateOfRecord",            "Town Date of Record"),
    ("TownGrandListCategory",       "Grand List Category"),
    ("TownSubdivision",             "Subdivision"),
    ("Latitude",                    "Latitude"),
    ("Longitude",                   "Longitude"),
    ("schoolCode",                  "School Code"),
    ("townCode",                    "Town Code"),
    ("additionalSellerNames",       "Other Sellers"),
    ("additionalBuyerNames",        "Other Buyers"),
]

# Fields that store boolean values and should display as Yes/No
BOOLEAN_FIELDS = {
    "enrolledCurrentUse",
    "currentUseEnrollmentContinue",
    "tenantPurchase",
    "rentedBefore",
    "rentedAfter",
    "developmentPrevConv",
    "buyerAdjoiningProperty",
    "TownSubdivision",
}

# Date fields (epoch ms → MM/DD/YYYY)
DATE_FIELDS = {"closingDate", "dateSellerAcquired", "TownDateOfRecord"}


# Date column labels that should be written as real Excel dates
DATE_EXPORT_LABELS = {lbl for key, lbl in EXPORT_COLUMNS
                      if key in DATE_FIELDS}

def _parse_export_date(val):
    """Convert MM/DD/YYYY string to datetime.date for Excel. Returns None if unparseable."""
    if not val or not isinstance(val, str):
        return None
    try:
        from datetime import date as _date
        m, d, y = val.split("/")
        return _date(int(y), int(m), int(d))
    except Exception:
        return None


def format_record_for_export(rec):
    """Format a record dict for CSV/XLSX export."""
    out = {}

    # Pre-compute Time Since Last Sale from the raw closingDate epoch
    time_since = time_since_last_sale(rec.get("closingDate"))

    for key, label in EXPORT_COLUMNS:
        if key == "timeSinceLastSale":
            out[label] = time_since
            continue

        val = rec.get(key, "")

        if key in DATE_FIELDS:
            val = format_epoch_date(val)
        elif key == "span":
            val = format_span(val)
        elif key in BOOLEAN_FIELDS:
            val = format_boolean(val)
        elif key == "TownGrandListCategory":
            val = format_grand_list_category(val)
        elif key == "buildingConstruction1Desc":
            val = format_building_type(val)
        elif key == "interestUndivPercent":
            val = format_interest_percent(val)
        elif val is None:
            val = ""

        out[label] = val

    return out


# ==============================
# ROUTES
# ==============================

@app.route("/")
def home():
    return render_template("map.html")


@app.route("/codes")
def codes():
    """Return all code lookup tables to the frontend."""
    return jsonify({
        "ptt_exemptions":      VT_CODES.get("ptt_exemptions", {}),
        "interest_types":      VT_CODES.get("interest_types", {}),
        "building_types":      VT_CODES.get("building_types", {}),
        "use_of_property":     VT_CODES.get("use_of_property", {}),
        "grand_list_categories": VT_CODES.get("grand_list_categories", {}),
    })


@app.route("/data")
def data():
    """Map data endpoint — viewport-bounded, returns up to 2000 records.

    When a name/SPAN/street filter is present the geometry bbox is dropped so
    ArcGIS searches the entire dataset by SQL.  Python then filters by name
    across all returned records, ensuring statewide name searches work.
    """
    xmin = request.args.get("xmin")
    ymin = request.args.get("ymin")
    xmax = request.args.get("xmax")
    ymax = request.args.get("ymax")

    filters = parse_filters(request.args)
    where   = build_where_clause(filters)

    # If any name / SPAN / street filter is active, skip the geometry bbox.
    # ArcGIS would apply the bbox first and return at most 2000 random records
    # from that area; Python name-matching then only sees that small pool.
    # Without the bbox ArcGIS returns up to 2000 records matching the SQL
    # filters (date, price, etc.) from the whole state, giving Python a much
    # larger and relevant pool to name-filter against.
    name_fields = [
        filters.get("seller_entity"), filters.get("seller_last"),
        filters.get("seller_first"),  filters.get("buyer_entity"),
        filters.get("buyer_last"),    filters.get("buyer_first"),
        filters.get("street"),        filters.get("span"),
    ]
    has_name_filter = any(v for v in name_fields)

    if has_name_filter:
        # No geometry restriction — search the full dataset
        features = fetch_all_features(where)
    else:
        geo_params = {
            "geometry":     f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR":         "4326",
            "spatialRel":   "esriSpatialRelIntersects",
        }
        features = fetch_features(where, geo_params, max_records=2000)

    results = []
    for f in features:
        rec = feature_to_record(f, filters)
        if rec is not None:
            results.append({
                "id":               rec["id"],
                "address":          rec["address"],
                "city":             rec["city"],
                "price":            rec["ValuePaidOrTransferred"],
                "date":             rec["closingDate"],
                "lat":              rec["lat"],
                "lon":              rec["lon"],
                "trustedCountyCode": rec["trustedCountyCode"],
                "trustedCountyName": rec["trustedCountyName"],
                "trustedTown":       rec["trustedTown"],
                "schoolCode":        rec["schoolCode"],
                "span":              rec["span"],
                "correctedCounty":   rec["correctedCounty"],
                # Extra fields for popup display
                "interestUndivPercentDesc": rec["interestUndivPercentDesc"],
                "buildingConstruction1Desc": rec["buildingConstruction1Desc"],
                "sellerUseOfPropertyDesc": rec["sellerUseOfPropertyDesc"],
                "buyerUseOfPropertyDesc":  rec["buyerUseOfPropertyDesc"],
                "sellerLastName":   rec["sellerLastName"],
                "sellerFirstName":  rec["sellerFirstName"],
                "sellerEntityName": rec["sellerEntityName"],
                "buyerLastName":    rec["buyerLastName"],
                "buyerFirstName":   rec["buyerFirstName"],
                "buyerEntityName":  rec["buyerEntityName"],
            })

    return jsonify({"data": results})


@app.route("/export")
def export():
    """
    Export endpoint — fetches ALL matching records statewide (no viewport).
    Returns XLSX by default, CSV if ?format=csv.
    Accepts same filter params as /data plus optional ?ids= for selected-only export.
    """
    fmt     = request.args.get("format", "xlsx").lower()
    ids_raw = request.args.get("ids", "")

    filters = parse_filters(request.args)
    where   = build_where_clause(filters)

    selected_ids = [i.strip() for i in ids_raw.split("|") if i.strip()] if ids_raw else []
    if selected_ids:
        ids_sql = ",".join(selected_ids)
        where = f"({where}) AND OBJECTID IN ({ids_sql})"
        features = fetch_features(where, max_records=len(selected_ids) + 10)
    else:
        features = fetch_all_features(where)

    records = []
    for f in features:
        rec = feature_to_record(f, filters)
        if rec is not None:
            records.append(format_record_for_export(rec))

    if not records:
        return jsonify({"error": "No records matched the current filters."}), 404

    # Sort by Closing Date ascending (convert MM/DD/YYYY -> sortable YYYY-MM-DD)
    closing_label = next((lbl for _, lbl in EXPORT_COLUMNS if lbl == "Closing Date"), "Closing Date")
    def _date_sort_key(r):
        v = r.get(closing_label) or ""
        try: parts = v.split("/"); return f"{parts[2]}-{parts[0]}-{parts[1]}"
        except: return v
    records.sort(key=_date_sort_key)

    headers = [label for _, label in EXPORT_COLUMNS]

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        csv_data = output.getvalue()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=vt_property_transfers.csv"}
        )
    else:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
        except ImportError:
            return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "VT Property Transfers"

        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF")

        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        for row_idx, rec in enumerate(records, 2):
            for col_idx, header in enumerate(headers, 1):
                raw = rec.get(header, "")
                cell = ws.cell(row=row_idx, column=col_idx)
                if header in DATE_EXPORT_LABELS:
                    dt = _parse_export_date(raw)
                    if dt:
                        cell.value = dt
                        cell.number_format = "MM/DD/YYYY"
                    else:
                        cell.value = raw
                else:
                    cell.value = raw

        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

        ws.freeze_panes = "A2"

        xlsx_buffer = io.BytesIO()
        wb.save(xlsx_buffer)
        xlsx_buffer.seek(0)

        return Response(
            xlsx_buffer.read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=vt_property_transfers.xlsx"}
        )


# ---------------------------------------------------------------------------
# Server-side map image generator
# Fetches OSM tiles for the given bounds/zoom, draws polygons + stats boxes.
# ---------------------------------------------------------------------------
def generate_map_image(map_meta, group_records):
    """
    map_meta: {
        bounds: {north, south, east, west},
        zoom: int,
        shapes: [{label, color, latlngs: [[lat,lng],...], stats: {...}}]
                 (circles sent as polygon approximation from frontend)
    }
    Returns PNG bytes or None.
    """
    import math, struct, zlib
    import urllib.request
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageColor
    except ImportError:
        return None

    meta           = map_meta or {}
    bounds         = meta.get("bounds", {})
    zoom           = int(meta.get("zoom", 14))
    shapes         = meta.get("shapes", [])
    selected_dots  = meta.get("selected_dots",   [])  # [[lat,lng],...]
    unselected_dots= meta.get("unselected_dots",  [])  # [[lat,lng],...]

    north = float(bounds.get("north",  44.20))
    south = float(bounds.get("south",  44.08))
    east  = float(bounds.get("east",  -72.55))
    west  = float(bounds.get("west",  -72.75))

    # ---- Tile math ----
    def latlon_to_tile(lat, lon, z):
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        lat_r = math.radians(lat)
        y = int((1 - math.log(math.tan(lat_r) + 1/math.cos(lat_r)) / math.pi) / 2 * n)
        return x, y

    def tile_to_latlon(x, y, z):
        n = 2 ** z
        lon = x / n * 360 - 180
        lat_r = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
        return math.degrees(lat_r), lon

    tx_min, ty_min = latlon_to_tile(north, west, zoom)
    tx_max, ty_max = latlon_to_tile(south, east, zoom)
    tx_min, tx_max = min(tx_min, tx_max), max(tx_min, tx_max)
    ty_min, ty_max = min(ty_min, ty_max), max(ty_min, ty_max)

    TILE_SIZE = 256
    cols = tx_max - tx_min + 1
    rows = ty_max - ty_min + 1
    # Cap at reasonable size
    if cols > 8: tx_max = tx_min + 7; cols = 8
    if rows > 8: ty_max = ty_min + 7; rows = 8

    img_w = cols * TILE_SIZE
    img_h = rows * TILE_SIZE
    canvas = Image.new("RGB", (img_w, img_h), (240, 240, 240))

    headers = {"User-Agent": "VTPropertyTool/1.0 (appraiservt@gmail.com)"}
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            try:
                req  = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    tile_data = resp.read()
                tile_img = Image.open(io.BytesIO(tile_data)).convert("RGB")
                px = (tx - tx_min) * TILE_SIZE
                py = (ty - ty_min) * TILE_SIZE
                canvas.paste(tile_img, (px, py))
            except Exception:
                pass  # leave grey if tile fails

    # ---- Coordinate conversion ----
    # Top-left of canvas corresponds to tile (tx_min, ty_min)
    origin_lat, origin_lon = tile_to_latlon(tx_min, ty_min, zoom)
    n_tiles_lat, n_tiles_lon = tile_to_latlon(tx_min + cols, ty_min + rows, zoom)

    def latlon_to_px(lat, lon):
        # Mercator pixel position
        n = 2 ** zoom
        px_x = (lon + 180) / 360 * n * TILE_SIZE - tx_min * TILE_SIZE
        lat_r = math.radians(lat)
        py_y  = (1 - math.log(math.tan(lat_r) + 1/math.cos(lat_r)) / math.pi) / 2 * n * TILE_SIZE - ty_min * TILE_SIZE
        return int(px_x), int(py_y)

    draw = ImageDraw.Draw(canvas, "RGBA")

    # Try to load a font, fall back to default
    try:
        fnt_bold  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        fnt_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     12)
        fnt_lbl   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except Exception:
        fnt_bold  = ImageFont.load_default()
        fnt_small = fnt_bold
        fnt_lbl   = fnt_bold

    PURPLE      = (123, 0, 212)
    PURPLE_FILL = (123, 0, 212, 40)   # semi-transparent
    WHITE       = (255, 255, 255)
    DARK        = (40,  40,  40)
    BORDER      = (123, 0, 212, 200)

    # ---- Draw each shape ----
    box_positions = {}  # shape label -> (box_x, box_y, box_w, box_h)
    anchor_positions = {}

    for i, shape in enumerate(shapes):
        latlngs = shape.get("latlngs", [])
        if not latlngs:
            continue
        label = shape.get("label", f"Area {i+1}")
        stats = shape.get("stats", {})

        pts = [latlon_to_px(ll[0], ll[1]) for ll in latlngs]
        if not pts:
            continue

        # Polygon fill + outline
        draw.polygon(pts, fill=(123, 0, 212, 35), outline=None)
        for j in range(len(pts)):
            draw.line([pts[j], pts[(j+1) % len(pts)]], fill=(60, 80, 120), width=2)

        # Centroid for anchor dot
        cx = int(sum(p[0] for p in pts) / len(pts))
        cy = int(sum(p[1] for p in pts) / len(pts))
        anchor_positions[label] = (cx, cy)
        draw.ellipse([cx-5, cy-5, cx+5, cy+5], fill=PURPLE, outline=WHITE)

        # Stats box position: 220px right, offset by row
        bx = min(cx + 220, img_w - 200)
        by = max(cy - 80 - i * 22, 10)
        bx = max(bx, 5)

        # Measure stats text
        fmt_m = lambda v: f"${int(v):,}" if v is not None else "N/A"
        rows_data = [
            ("Sales (>$0):", str(stats.get("count", 0))),
            ("High:",        fmt_m(stats.get("high"))),
            ("Low:",         fmt_m(stats.get("low"))),
            ("Average:",     fmt_m(stats.get("avg"))),
            ("Median:",      fmt_m(stats.get("median"))),
        ]
        box_w  = 210
        hdr_h  = 28
        row_h  = 20
        pad    = 8
        box_h  = hdr_h + pad + len(rows_data) * row_h + pad

        # Shadow
        shadow_off = 3
        draw.rounded_rectangle(
            [bx+shadow_off, by+shadow_off, bx+box_w+shadow_off, by+box_h+shadow_off],
            radius=6, fill=(0, 0, 0, 60)
        )

        # White body
        draw.rounded_rectangle([bx, by, bx+box_w, by+box_h], radius=6,
                                fill=WHITE, outline=PURPLE, width=2)
        # Purple header
        draw.rounded_rectangle([bx, by, bx+box_w, by+hdr_h], radius=6,
                                fill=PURPLE)
        # Cover bottom corners of header (make it flat on bottom)
        draw.rectangle([bx, by+hdr_h-6, bx+box_w, by+hdr_h], fill=PURPLE)

        # Header text
        draw.text((bx+10, by+7), label, font=fnt_lbl, fill=WHITE)

        # Stats rows
        for ri, (k, v) in enumerate(rows_data):
            ry = by + hdr_h + pad + ri * row_h
            draw.text((bx+8,       ry), k, font=fnt_small, fill=DARK)
            # Right-align value
            try:
                vw = fnt_small.getlength(v)
            except Exception:
                vw = len(v) * 7
            draw.text((bx+box_w-8-vw, ry), v,
                      font=fnt_bold if ri == 0 else fnt_small, fill=DARK)

        box_positions[label] = (bx, by, box_w, box_h)

    # ---- Draw tie lines (anchor dot -> box edge) ----
    for label, (bx, by, bw, bh) in box_positions.items():
        if label not in anchor_positions:
            continue
        ax, ay = anchor_positions[label]
        # Connect to left-center of box
        ex, ey = bx, by + bh // 2
        # Dashed line
        dx, dy = ex - ax, ey - ay
        dist   = max(1, math.hypot(dx, dy))
        dash, gap = 8, 5
        step = dash + gap
        d = 0
        while d < dist:
            t0 = d / dist
            t1 = min((d + dash) / dist, 1.0)
            x0, y0 = int(ax + dx*t0), int(ay + dy*t0)
            x1, y1 = int(ax + dx*t1), int(ay + dy*t1)
            draw.line([(x0,y0),(x1,y1)], fill=PURPLE, width=2)
            d += step

    # ---- Draw Area label pills ----
    for label, (ax, ay) in anchor_positions.items():
        pill_pad_x, pill_pad_y = 10, 5
        try:
            tw = fnt_lbl.getlength(label)
        except Exception:
            tw = len(label) * 8
        pw = int(tw) + pill_pad_x * 2
        ph = 26
        px0 = ax - pw // 2
        py0 = ay - 40  # above anchor
        draw.rounded_rectangle([px0, py0, px0+pw, py0+ph],
                                radius=ph//2, fill=PURPLE)
        draw.text((px0+pill_pad_x, py0+5), label, font=fnt_lbl, fill=WHITE)

    # ---- Draw property dots (on top of everything) ----
    DOT_R = 5
    BLUE  = (0, 102, 255)
    DOT_DARK = (26, 26, 26)

    for ll in unselected_dots:
        px, py = latlon_to_px(ll[0], ll[1])
        draw.ellipse([px-DOT_R, py-DOT_R, px+DOT_R, py+DOT_R],
                     fill=(255, 255, 255), outline=DOT_DARK, width=2)

    for ll in selected_dots:
        px, py = latlon_to_px(ll[0], ll[1])
        draw.ellipse([px-DOT_R, py-DOT_R, px+DOT_R, py+DOT_R],
                     fill=BLUE, outline=BLUE)

    # ---- Export to PNG bytes ----
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.read()


@app.route("/export_grouped", methods=["GET", "POST"])
def export_grouped():
    """
    Grouped export.
    Accepts POST with JSON body: {format, filters, groups}
    or GET with ?groups=JSON&format=...
    """
    import json as _json

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        fmt        = data.get("format", "xlsx").lower()
        groups     = data.get("groups", [])
        map_meta   = data.get("map_meta",   None)  # {bounds, zoom, shapes}
        screenshot = data.get("screenshot", None)  # base64 PNG from getDisplayMedia
        # Build a fake args-like object from the filters dict
        filter_dict = data.get("filters", {})
        class _FakeArgs(dict):
            def get(self, k, default=""):
                return super().get(k, default)
        filters = parse_filters(_FakeArgs(filter_dict))
    else:
        fmt        = request.args.get("format", "xlsx").lower()
        groups_raw = request.args.get("groups", "[]")
        try:
            groups = _json.loads(groups_raw)
        except Exception:
            return jsonify({"error": "Invalid groups parameter"}), 400
        filters    = parse_filters(request.args)
        map_meta   = None
        screenshot = None

    if not groups:
        return jsonify({"error": "No groups provided"}), 400

    # Fetch records for each group
    group_records = []
    for g in groups:
        ids = [str(i).strip() for i in g.get("ids", []) if str(i).strip()]
        stats = g.get("stats", {})
        label = g.get("label", "Area")
        if not ids:
            group_records.append({"label": label, "records": [], "stats": stats})
            continue
        ids_sql  = ",".join(ids)
        where    = f"OBJECTID IN ({ids_sql})"
        features = fetch_features(where, max_records=len(ids) + 10)
        recs = []
        for f in features:
            rec = feature_to_record(f, filters)
            if rec is not None:
                recs.append(format_record_for_export(rec))
        # Sort each group by Closing Date ascending
        closing_label = next((lbl for _, lbl in EXPORT_COLUMNS if lbl == "Closing Date"), "Closing Date")
        def _grp_date_key(r):
            v = r.get(closing_label) or ""
            try: parts = v.split("/"); return f"{parts[2]}-{parts[0]}-{parts[1]}"
            except: return v
        recs.sort(key=_grp_date_key)
        group_records.append({"label": label, "records": recs, "stats": stats})

    headers = [label for _, label in EXPORT_COLUMNS]

    def fmt_money(val):
        if val is None:
            return "N/A"
        try:
            return f"${int(val):,}"
        except Exception:
            return str(val)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        first = True
        for g in group_records:
            if not first:
                writer.writerow({h: "" for h in headers})
                writer.writerow({h: "" for h in headers})
                writer.writerow({h: "" for h in headers})
                writer.writerow({h: "" for h in headers})
            first = False
            # Group header
            writer.writerow({headers[0]: f"=== {g['label']} ===", **{h: "" for h in headers[1:]}})
            writer.writeheader()
            writer.writerows(g["records"])
            # Blank row before stats
            writer.writerow({h: "" for h in headers})
            # Stats row
            st = g["stats"]
            writer.writerow({h: "" for h in headers})
            writer.writerow({
                headers[0]: f"{g['label']} Summary",
                headers[1]: f"Sales (>$0): {st.get('count','N/A')}",
                headers[2]: f"High: {fmt_money(st.get('high'))}",
                headers[3]: f"Low: {fmt_money(st.get('low'))}",
                headers[4]: f"Average: {fmt_money(st.get('avg'))}",
                headers[5]: f"Median: {fmt_money(st.get('median'))}",
                **{h: "" for h in headers[6:]}
            })
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=vt_property_transfers_grouped.csv"}
        )
    else:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500

        wb  = openpyxl.Workbook()
        ws  = wb.active
        ws.title = "VT Property Transfers"

        # Styles
        hdr_fill  = PatternFill("solid", fgColor="1F4E79")
        hdr_font  = Font(bold=True, color="FFFFFF")
        grp_fill  = PatternFill("solid", fgColor="2E75B6")
        grp_font  = Font(bold=True, color="FFFFFF", size=12)
        stat_fill = PatternFill("solid", fgColor="D9E1F2")
        stat_font = Font(bold=True, color="1F4E79")
        center    = Alignment(horizontal="center")

        cur_row = 1

        for g_idx, g in enumerate(group_records):
            # ---- Group label row ----
            # Fill every cell in the row with the blue style so the bar
            # spans all columns. Put the label text in column 1 only.
            # (Avoiding merge_cells entirely — it reliably drops the value
            # in some openpyxl versions on Python 3.14.)
            for col_idx in range(1, len(headers) + 1):
                c = ws.cell(row=cur_row, column=col_idx,
                            value=g["label"] if col_idx == 1 else "")
                c.fill = grp_fill
                c.font = grp_font
                if col_idx == 1:
                    c.alignment = Alignment(horizontal="left", vertical="center")
            cur_row += 1

            # ---- Column header row ----
            for col_idx, h in enumerate(headers, 1):
                cell = ws.cell(row=cur_row, column=col_idx, value=h)
                cell.fill = hdr_fill
                cell.font = hdr_font
                cell.alignment = center
            cur_row += 1

            # ---- Data rows ----
            for rec in g["records"]:
                for col_idx, h in enumerate(headers, 1):
                    raw  = rec.get(h, "")
                    cell = ws.cell(row=cur_row, column=col_idx)
                    if h in DATE_EXPORT_LABELS:
                        dt = _parse_export_date(raw)
                        if dt:
                            cell.value = dt
                            cell.number_format = "MM/DD/YYYY"
                        else:
                            cell.value = raw
                    else:
                        cell.value = raw
                cur_row += 1

            # ---- Blank row before stats (keeps stats out of sort range) ----
            cur_row += 1

            # ---- Stats row ----
            st = g["stats"]
            stats_labels = [
                f"Sales (>$0): {st.get('count', 'N/A')}",
                f"High: {fmt_money(st.get('high'))}",
                f"Low: {fmt_money(st.get('low'))}",
                f"Average: {fmt_money(st.get('avg'))}",
                f"Median: {fmt_money(st.get('median'))}",
            ]
            for col_idx, val in enumerate(stats_labels, 1):
                cell = ws.cell(row=cur_row, column=col_idx, value=val)
                cell.fill = stat_fill
                cell.font = stat_font
            cur_row += 1

            # ---- 4 blank spacer rows between groups ----
            if g_idx < len(group_records) - 1:
                cur_row += 4

        # Column widths (skip MergedCell objects which have no column_letter)
        for col in ws.columns:
            first = next((c for c in col if hasattr(c, 'column_letter')), None)
            if not first:
                continue
            max_len = max((len(str(cell.value or "")) for cell in col if hasattr(cell, 'value')), default=10)
            ws.column_dimensions[first.column_letter].width = min(max_len + 2, 40)

        ws.freeze_panes = "A3"  # freeze past group label + header

        # ---- Map tab: server-side rendered polygon map ----
        if map_meta:
            from openpyxl.drawing.image import Image as XLImage
            try:
                img_bytes = generate_map_image(map_meta, group_records)
                if img_bytes:
                    img_stream = io.BytesIO(img_bytes)
                    ws_map = wb.create_sheet(title="Map")
                    ws_map.sheet_view.showGridLines = False
                    xl_img = XLImage(img_stream)
                    xl_img.width  = 1200
                    xl_img.height = 800
                    ws_map.add_image(xl_img, "A1")
                    ws_map.column_dimensions["A"].width = 160
                    ws_map.row_dimensions[1].height     = 600
            except Exception:
                pass  # non-fatal

        # ---- Screenshot tab: exact screen capture from browser ----
        if screenshot:
            import base64 as _b64
            from openpyxl.drawing.image import Image as XLImage
            try:
                raw = _b64.b64decode(screenshot)
                ws_sc = wb.create_sheet(title="Screen Capture")
                ws_sc.sheet_view.showGridLines = False
                xl_sc = XLImage(io.BytesIO(raw))
                # Fit to ~1200 wide preserving aspect ratio
                from PIL import Image as _PILImage
                pil = _PILImage.open(io.BytesIO(raw))
                orig_w, orig_h = pil.size
                scale = 1200 / orig_w if orig_w > 1200 else 1.0
                xl_sc.width  = int(orig_w * scale)
                xl_sc.height = int(orig_h * scale)
                ws_sc.add_image(xl_sc, "A1")
                ws_sc.column_dimensions["A"].width = xl_sc.width / 7.5
                ws_sc.row_dimensions[1].height     = xl_sc.height * 0.75
            except Exception:
                pass  # non-fatal

    # ... you’ve just finished writing all rows & formatting on ws ...

   

    # ---- Existing code that saves and returns workbook ----
    xlsxbuffer = io.BytesIO()
    wb.save(xlsxbuffer)
    xlsxbuffer.seek(0)
    return Response(
        xlsxbuffer.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=vtpropertytransfers_grouped.xlsx"
        },
    )



        xlsx_buf = io.BytesIO()
        wb.save(xlsx_buf)
        xlsx_buf.seek(0)
        return Response(
            xlsx_buf.read(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=vt_property_transfers_grouped.xlsx"}
        )


@app.route("/history")
def history():
    """Return ALL sales for a given SPAN (no date filter) so the popup can
    show older sales above the dotted-line divider even when a date range
    filter is active on the main map."""
    span_raw = request.args.get("span", "").strip().replace("-", "")
    if not span_raw:
        return jsonify({"data": []})

    where = f"CAST(span AS VARCHAR(20)) LIKE '%{span_raw}%'"
    features = fetch_all_features(where)

    # Parse filters without date range so all sales are returned
    empty_filters = {k: "" for k in [
        "counties", "towns", "date_from", "date_to", "price_low", "price_high",
        "land_low", "land_high", "street", "interest", "building", "seller_use",
        "buyer_use", "ptt_exemption", "grand_list", "seller_entity", "seller_last",
        "seller_first", "buyer_entity", "buyer_last", "buyer_first", "span",
        "dev_prev_conv", "buyer_adjoining", "enrolled_current_use", "foreclosed",
    ]}

    results = []
    for f in features:
        rec = feature_to_record(f, empty_filters)
        if rec is not None:
            results.append({
                "id":              rec["id"],
                "address":         rec["address"],
                "price":           rec["ValuePaidOrTransferred"],
                "date":            rec["closingDate"],
                "lat":             rec["lat"],
                "lon":             rec["lon"],
                "sellerLastName":  rec["sellerLastName"],
                "sellerFirstName": rec["sellerFirstName"],
                "sellerEntityName":rec["sellerEntityName"],
                "buyerLastName":   rec["buyerLastName"],
                "buyerFirstName":  rec["buyerFirstName"],
                "buyerEntityName": rec["buyerEntityName"],
                "buildingConstruction1Desc": rec["buildingConstruction1Desc"],
                "interestUndivPercentDesc":  rec["interestUndivPercentDesc"],
                "approxLocation":  rec["approxLocation"],
            })

    return jsonify({"data": results})


if __name__ == "__main__":
    app.run(debug=True)
