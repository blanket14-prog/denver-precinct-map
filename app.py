import hashlib
import json
import os
import re
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
# Set GA_MEASUREMENT_ID in the Render dashboard to switch analytics on. Left
# unset, nothing is loaded at all, so local runs and forks stay untracked.
GA_ID = (os.environ.get("GA_MEASUREMENT_ID") or "").strip()
if GA_ID and not re.match(r"^G-[A-Z0-9]{4,20}$", GA_ID):
    GA_ID = ""          # a typo should mean no analytics, not a broken tag

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
APP_VERSION = "32"

DATA_FILES = ("precincts.geojson", "districts.geojson", "elections.json",
              "returns.json", "tracts.geojson", "demographics.json")

CONFIG_FILE = "config.json"
_config_cache = {}


def load_config():
    """What the public is allowed to see, from data/config.json.

    Kept in the repo rather than a database: Render's free tier has no
    persistent disk and spins down when idle, so a file written at runtime
    would not survive, and the two gunicorn workers would not agree on it
    anyway. /admin builds this document; committing it is what publishes it.

    A missing or unreadable file means show everything, so a bad edit degrades
    to the full map rather than a blank one.
    """
    path = os.path.join(DATA_DIR, CONFIG_FILE)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _config_cache.get("mtime") != mtime:
        try:
            with open(path) as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("config is not an object")
        except (OSError, ValueError) as exc:
            app.logger.error("BAD CONFIG %s: %s -- showing everything", path, exc)
            doc = {}
        _config_cache.update(mtime=mtime, doc=doc)
    return _config_cache.get("doc", {})


def visible(group, key, default=True):
    """Is one switch on? Unknown keys default to visible."""
    section = load_config().get(group)
    if not isinstance(section, dict):
        return default
    return bool(section.get(key, default))


def section_on(name):
    """A section is off if its own switch is off, or nothing inside it is on."""
    if not visible("sections", name):
        return False
    inner = {"elections": "elections", "returns": "returns",
             "demographics": "demographics", "districts": "filters",
             "boundaries": "boundaries"}.get(name)
    if inner:
        group = load_config().get(inner)
        if isinstance(group, dict) and group and not any(group.values()):
            return False
    return True
_geojson_ver = {}


def geojson_version(name):
    """Short content hash, so the client URL changes whenever the data does.

    Without this, the long Cache-Control on the GeoJSON means anyone who
    loaded the map before a data update keeps the stale copy for a day. The
    config is folded in because it changes what the file contains: switch a
    contest off and the payload changes without the file on disk changing.
    """
    return _file_version(name) + _file_version(CONFIG_FILE)[:4]


def _file_version(name):
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
            config=json.dumps(public_config(), separators=(",", ":")),
            ga_id=GA_ID,
            # Share codes carry the whole map state, so reporting them as page
            # paths would scatter one page across thousands of distinct URLs
            # and make every report useless. Every shared view counts as "/s".
            ga_path="/s" if request.path.startswith("/s/") else request.path,
        )
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def public_config():
    """The switches the page needs, with every gap filled in as visible.

    Sent whole rather than as a set of flags so the template stays simple, and
    so a section the config does not mention keeps working.
    """
    cfg = load_config()
    out = {"sections": {}, "filters": {}, "boundaries": {}, "elections": {},
           "returns": {}, "demographics": {}, "detail": {}}
    for group in out:
        given = cfg.get(group)
        out[group] = dict(given) if isinstance(given, dict) else {}
    for name in ("districts", "elections", "returns", "demographics",
                 "basemap", "boundaries", "labels"):
        out["sections"][name] = section_on(name)
    return out


@app.route("/admin")
def admin():
    """A switchboard for what the public sees.

    It does not save anything. Render's free tier has no persistent disk, so
    the config lives in the repo: this page builds the document and you commit
    it. That also means /admin can be public without risk, since it cannot
    change what the server serves and the server does not serve what the
    committed config hides.
    """
    resp = app.make_response(render_template(
        "admin.html",
        version=APP_VERSION,
        config=json.dumps(load_config(), indent=2),
    ))
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def filter_payload(fname, doc):
    """Strip a data file down to what the config makes public.

    Hiding a row in the panel would still ship the whole file, so anyone could
    read what was meant to be private straight out of /data. Filtering here is
    what makes a switch mean something: what is off never leaves the server.
    """
    if fname == "elections.json":
        if not section_on("elections"):
            return {"bands": doc.get("bands", []), "contests": []}
        doc["contests"] = [c for c in doc.get("contests", [])
                           if visible("elections", c.get("key"))]
    elif fname == "returns.json":
        if not section_on("returns"):
            return {"elections": []}
        doc["elections"] = [e for e in doc.get("elections", [])
                            if visible("returns", e.get("key"))]
    elif fname == "demographics.json":
        if not section_on("demographics"):
            return {"measures": [], "precinctTract": {}, "source": {}}
        doc["measures"] = [m for m in doc.get("measures", [])
                           if visible("demographics", m.get("key"))]
    elif fname == "districts.geojson":
        keep = {"council", "school", "rtd", "senate", "house", "nbhd"}
        on = {k for k in keep if visible("boundaries", k)}
        feats = []
        for f in doc.get("features", []):
            layer = f.get("properties", {}).get("layer")
            if layer == "mask":
                if visible("boundaries", "mask"):
                    feats.append(f)
            elif layer in keep:
                if layer in on and section_on("boundaries"):
                    feats.append(f)
            else:
                feats.append(f)
        doc["features"] = feats
    elif fname == "precincts.geojson":
        FIELDS = {"voters": ("active", "inactive", "registered"),
                  "code": ("precinct_code",),
                  "neighborhood": ("neighborhood",)}
        gone = [prop for switch, props in FIELDS.items()
                if not visible("detail", switch) for prop in props]
        if gone:
            for f in doc.get("features", []):
                for p in gone:
                    f.get("properties", {}).pop(p, None)
            doc.pop("voter_source", None)
    return doc


@app.route("/data/<name>.<ext>")
def geojson(name, ext):
    fname = name + "." + ext
    if fname not in DATA_FILES:
        return jsonify({"error": "unknown data file"}), 404
    path = os.path.join(DATA_DIR, fname)
    if not os.path.exists(path):
        app.logger.error("MISSING DATA FILE: %s", path)
        return jsonify({"error": fname + " not found on server"}), 500

    if fname == "tracts.geojson" and not section_on("demographics"):
        return jsonify({"type": "FeatureCollection", "features": []})

    mime = "application/geo+json" if ext == "geojson" else "application/json"
    if load_config():
        with open(path) as fh:
            doc = json.load(fh)
        resp = app.response_class(
            json.dumps(filter_payload(fname, doc), separators=(",", ":")),
            mimetype=mime)
    else:
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
