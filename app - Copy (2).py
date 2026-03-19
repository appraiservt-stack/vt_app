from flask import Flask, render_template, request, jsonify, send_file
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime

app = Flask(__name__)

ARCGIS_URL = "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"

# ✅ CORRECT COUNTY MAP
COUNTY_MAP = {
    "01": "Addison",
    "02": "Bennington",
    "03": "Caledonia",
    "04": "Chittenden",
    "05": "Essex",
    "06": "Franklin",
    "07": "Grand Isle",
    "08": "Lamoille",
    "09": "Orange",
    "10": "Orleans",
    "11": "Rutland",
    "12": "Washington",
    "13": "Windham",
    "14": "Windsor"
}

COUNTY_NAME_TO_CODE = {v: k for k, v in COUNTY_MAP.items()}

@app.route("/")
def home():
    return render_template("map.html")

@app.route("/metadata")
def metadata():
    return jsonify({
        "counties": list(COUNTY_MAP.values())
    })

@app.route("/data")
def data():

    xmin = request.args.get("xmin")
    ymin = request.args.get("ymin")
    xmax = request.args.get("xmax")
    ymax = request.args.get("ymax")

    counties = request.args.getlist("county")

    where = "1=1"

    if counties and "ALL" not in counties:
        codes = [COUNTY_NAME_TO_CODE[c] for c in counties if c in COUNTY_NAME_TO_CODE]
        if codes:
            code_str = ",".join([f"'{c}'" for c in codes])
            where += f" AND countyCode IN ({code_str})"

    params = {
        "where": where,
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "*",
        "f": "json",
        "outSR": "4326",
        "resultRecordCount": 2000
    }

    r = requests.get(ARCGIS_URL, params=params).json()

    results = []

    for f in r.get("features", []):
        attr = f["attributes"]

        date = attr.get("closeDate")
        if date:
            date = datetime.fromtimestamp(date / 1000).strftime("%m/%d/%Y")

        results.append({
            "id": attr.get("OBJECTID"),
            "address": attr.get("propLocStr"),
            "city": attr.get("propLocCty"),
            "price": attr.get("ValPdOrTrn") or 0,
            "date": date,
            "lat": attr.get("Latitude"),
            "lon": attr.get("Longitude"),
            "county": COUNTY_MAP.get(attr.get("countyCode"))
        })

    return jsonify({"data": results})

# ================= EXPORT FIX =================
@app.route("/export", methods=["POST"])
def export():

    data = request.json["data"]

    df = pd.DataFrame(data)

    # 🔥 Convert date to sortable format
    df["date_sort"] = pd.to_datetime(df["date"], format="%m/%d/%Y", errors="coerce")

    # 🔥 Create grouping key
    df["group"] = df["address"].fillna("") + " | " + df["city"].fillna("")

    # 🔥 Sort: group first, then newest sale first
    df = df.sort_values(by=["group", "date_sort"], ascending=[True, False])

    # Drop helper columns
    df = df.drop(columns=["date_sort", "group"])

    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, download_name="selected_sales.xlsx", as_attachment=True)

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True)