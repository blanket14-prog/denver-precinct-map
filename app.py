import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Nominatim asks for an identifying User-Agent and at most one request per
# second. We proxy geocoding through the server so both are actually enforced,
# and so browsers are not making cross-origin calls to OSM on every keystroke.
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT",
    "denver-precinct-map/1.0 (https://github.com/blanket14-prog/denver-precinct-map)",
)
DENVER_VIEWBOX = "-105.11,39.914,-104.59,39.61"

_geo_lock = threading.Lock()
_geo_last = [0.0]
_geo_cache = {}
GEO_CACHE_MAX = 500


# Shown in the map's bottom-right corner and returned by /healthz, so it is
# obvious at a glance whether a browser is on the current deploy or a cached
# copy. Bump this with every change that ships.
APP_VERSION = "27"

DATA_FILES = ("precincts.geojson", "districts.geojson", "elections.json",
              "returns.json", "tracts.geojson", "demographics.json")
_geojson_ver = {}


def geojson_version(name):
    """Short content hash, so the client URL changes whenever the data does.

    Without this, the long Cache-Control on the GeoJSON means anyone who
    loaded the map before a data update keeps the stale copy for a day.
    """
    path = os.path.join(DATA_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        app.logger.error("MISSING DATA FILE when versioning: %s", path)
        return "0"
    cached = _geojson_ver.get(name)
    if not cached or cached[0] != mtime:
        digest = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        cached = (mtime, digest.hexdigest()[:10])
        _geojson_ver[name] = cached
    return cached[1]


@app.route("/s/<path:code>")
def shared(code):
    """A shared view. The state is encoded in the path itself.

    Render's free tier has no persistent disk and the service sleeps, so a
    lookup table of short codes would not survive. Encoding the state in the
    code instead means a shared link never expires and needs no storage. See
    encodeState()/applyState() in index.html for the format.
    """
    if len(code) > 200:
        return jsonify({"error": "share code too long"}), 400
    return index()


@app.route("/")
def index():
    resp = app.make_response(
        render_template(
            "index.html",
            data_url="/data/precincts.geojson?v=" + geojson_version("precincts.geojson"),
            districts_url="/data/districts.geojson?v=" + geojson_version("districts.geojson"),
            elections_url="/data/elections.json?v=" + geojson_version("elections.json"),
            returns_url="/data/returns.json?v=" + geojson_version("returns.json"),
            tracts_url="/data/tracts.geojson?v=" + geojson_version("tracts.geojson"),
            demographics_url="/data/demographics.json?v="
                             + geojson_version("demographics.json"),
            version=APP_VERSION,
        )
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/data/<name>.<ext>")
def geojson(name, ext):
    fname = name + "." + ext
    if fname not in DATA_FILES:
        return jsonify({"error": "unknown data file"}), 404
    path = os.path.join(DATA_DIR, fname)
    if not os.path.exists(path):
        app.logger.error("MISSING DATA FILE: %s", path)
        return jsonify({"error": fname + " not found on server"}), 500
    mime = "application/geo+json" if ext == "geojson" else "application/json"
    resp = send_from_directory(DATA_DIR, fname, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/geocode")
def geocode():
    """Street address -> {lat, lon, name}, restricted to the Denver area."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "empty query"}), 400
    if len(q) > 200:
        return jsonify({"error": "query too long"}), 400

    key = q.lower()
    if key in _geo_cache:
        return jsonify(_geo_cache[key])

    query = q
    low = q.lower()
    if "denver" not in low and ", co" not in low and "colorado" not in low:
        query = q + ", Denver, Colorado"

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": "1",
        "countrycodes": "us",
        "viewbox": DENVER_VIEWBOX,
        "bounded": "1",
        "addressdetails": "0",
    })
    url = NOMINATIM + "?" + params

    with _geo_lock:
        wait = 1.0 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            app.logger.error("GEOCODE HTTP %s for %r", exc.code, query)
            return jsonify({"error": "geocoder returned %s" % exc.code}), 502
        except Exception as exc:
            app.logger.error("GEOCODE FAILED for %r: %s", query, exc)
            return jsonify({"error": "geocoder unreachable"}), 502
        finally:
            _geo_last[0] = time.time()

    if not payload:
        app.logger.info("GEOCODE no match for %r", query)
        return jsonify({"error": "no match in the Denver area"}), 404

    hit = payload[0]
    try:
        out = {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "name": hit.get("display_name", "").split(", United States")[0],
        }
    except (KeyError, TypeError, ValueError) as exc:
        app.logger.error("GEOCODE bad payload for %r: %s", query, exc)
        return jsonify({"error": "unexpected geocoder response"}), 502

    if len(_geo_cache) < GEO_CACHE_MAX:
        _geo_cache[key] = out
    return jsonify(out)


@app.route("/healthz")
def healthz():
    missing = [f for f in DATA_FILES
               if not os.path.exists(os.path.join(DATA_DIR, f))]
    if missing:
        app.logger.error("HEALTHCHECK missing data files: %s", missing)
    return jsonify({"ok": not missing, "version": APP_VERSION,
                    "missing": missing}), (200 if not missing else 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
