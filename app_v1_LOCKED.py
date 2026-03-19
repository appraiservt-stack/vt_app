from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)

ARCGIS_URL = "https://services1.arcgis.com/BkFxaEFNwHqX3tAw/arcgis/rest/services/FS_VCGI_OPENDATA_Cadastral_PTTR_point_WM_v1_view/FeatureServer/0/query"

@app.route("/")
def home():
    return render_template("map.html")


# ================= DATA =================
@app.route("/data")
def data():

    xmin = request.args.get("xmin")
    ymin = request.args.get("ymin")
    xmax = request.args.get("xmax")
    ymax = request.args.get("ymax")

    params = {
        "where": "1=1",
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
        geom = f["geometry"]

        results.append({
            "id": attr.get("OBJECTID"),
            "address": attr.get("propLocStr"),
            "city": attr.get("propLocCty"),
            "price": attr.get("RlPrVlPdTr") or 0,
            "date": attr.get("closeDate"),
            "lat": geom.get("y"),
            "lon": geom.get("x")
        })

    return jsonify({"data": results})


if __name__ == "__main__":
    app.run(debug=True)