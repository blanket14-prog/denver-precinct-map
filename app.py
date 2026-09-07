import os
from flask import Flask, render_template, send_from_directory, jsonify

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data/precincts.geojson")
def precincts():
    path = os.path.join(DATA_DIR, "precincts.geojson")
    if not os.path.exists(path):
        app.logger.error("MISSING DATA FILE: %s", path)
        return jsonify({"error": "precincts.geojson not found on server"}), 500
    resp = send_from_directory(DATA_DIR, "precincts.geojson",
                               mimetype="application/geo+json")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/healthz")
def healthz():
    ok = os.path.exists(os.path.join(DATA_DIR, "precincts.geojson"))
    return jsonify({"ok": ok}), (200 if ok else 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
