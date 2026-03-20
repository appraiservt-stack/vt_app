from flask import Flask, render_template, request, jsonify
import requests

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

    counties = request.args.get("counties")

    where = "1=1"

    if counties:
        codes = counties.split(",")
        codes_sql = ",".join([f"'{c}'" for c in codes])
        where += f" AND countyCode IN ({codes_sql})"

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

    r = requests.get(ARCGIS_URL, params=params)
    data = r.json()

    results = []

    for f in data.get("features", []):
        attr = f["attributes"]

        results.append({
            "id": attr.get("OBJECTID"),
            "address": attr.get("propLocStr"),
            "city": attr.get("propLocCty"),
            "price": attr.get("ValPdOrTrn") or 0,
            "date": attr.get("closeDate"),
            "lat": attr.get("Latitude"),
            "lon": attr.get("Longitude"),
            "countyCode": attr.get("countyCode")
        })

    return jsonify({"data": results})


if __name__ == "__main__":
    app.run(debug=True)