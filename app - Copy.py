from flask import Flask, render_template, request, send_file
import requests
import pandas as pd
import io
from datetime import datetime

app = Flask(__name__)

ARCGIS_URL = "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"

@app.route("/")
def home():
    return render_template("map.html")


@app.route("/data")
def data():

    xmin = request.args.get("xmin")
    ymin = request.args.get("ymin")
    xmax = request.args.get("xmax")
    ymax = request.args.get("ymax")

    geometry = f"{xmin},{ymin},{xmax},{ymax}"

    params = {
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 2000
    }

    response = requests.get(ARCGIS_URL, params=params)
    data = response.json()

    features = data.get("features", [])
    results = []

    for f in features:
        attr = f.get("attributes", {})
        geom = f.get("geometry", {})

        raw_date = attr.get("closeDate")
        if raw_date:
            try:
                date = datetime.utcfromtimestamp(raw_date / 1000).strftime("%m/%d/%Y")
            except:
                date = ""
        else:
            date = ""

        results.append({
            "id": attr.get("OBJECTID"),
            "lat": geom.get("y"),
            "lon": geom.get("x"),
            "address": attr.get("propLocStr"),
            "city": attr.get("propLocCty"),
            "price": attr.get("ValPdOrTrn") or 0,
            "date": date
        })

    return {"data": results}


@app.route("/export", methods=["POST"])
def export():
    data = request.json.get("data", [])

    if not data:
        return {"error": "No data selected"}

    df = pd.DataFrame(data)

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        download_name="selected_sales.xlsx",
        as_attachment=True
    )


if __name__ == "__main__":
    app.run(debug=True)