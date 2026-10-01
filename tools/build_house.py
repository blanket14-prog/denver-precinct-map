"""Statewide Colorado House districts -> data/house.geojson.

Source: Census TIGER/Line 2024 state legislative districts, lower chamber
(tl_2024_08_sldl). The web map only needs shapes, a number and somewhere to
put the label, so the geometry is simplified hard: the full file is 2.5 MB of
shapefile, which is far more detail than a statewide view can draw.

Run:  python3 tools/build_house.py /path/to/tl_2024_08_sldl
"""
import json, os, sys, math
import shapefile
from shapely.geometry import shape, mapping
from shapely.ops import transform
import pyproj

SRC = sys.argv[1] if len(sys.argv) > 1 else \
    "/mnt/user-data/uploads/Maps/State House/tl_2024_08_sldl"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "house.geojson")

# simplify in metres, then go back to lat/lon
TO_M = pyproj.Transformer.from_crs("EPSG:4269", "EPSG:26954", always_xy=True).transform
TO_LL = pyproj.Transformer.from_crs("EPSG:26954", "EPSG:4269", always_xy=True).transform
TOLERANCE = 120.0          # metres; ~0.3 px at the zoom this map opens on

def round_geom(g, nd=5):
    """Trim coordinate precision. 5 dp is about a metre and halves the file."""
    def r(o):
        if isinstance(o, (list, tuple)):
            if o and isinstance(o[0], (int, float)):
                return [round(float(o[0]), nd), round(float(o[1]), nd)]
            return [r(x) for x in o]
        return o
    gg = mapping(g)
    gg["coordinates"] = r(gg["coordinates"])
    return gg

r = shapefile.Reader(SRC)
feats, kept = [], 0
for rec, shp in zip(r.records(), r.shapes()):
    num = int(rec["SLDLST"])
    g = shape(shp.__geo_interface__).buffer(0)
    gm = transform(TO_M, g).simplify(TOLERANCE, preserve_topology=True).buffer(0)
    if gm.is_empty:
        gm = transform(TO_M, g)
    gl = transform(TO_LL, gm)
    # label point: guaranteed inside, unlike a centroid on a crescent-shaped seat
    pt = transform(TO_LL, transform(TO_M, g).representative_point())
    feats.append({
        "type": "Feature",
        "properties": {"d": num, "label": [round(pt.x, 5), round(pt.y, 5)],
                       "area": round(transform(TO_M, g).area / 2.59e6, 1)},
        "geometry": round_geom(gl),
    })
    kept += 1

feats.sort(key=lambda f: f["properties"]["d"])
doc = {"type": "FeatureCollection",
       "source": "U.S. Census Bureau TIGER/Line 2024, tl_2024_08_sldl",
       "simplified_m": TOLERANCE,
       "features": feats}
with open(OUT, "w") as fh:
    json.dump(doc, fh, separators=(",", ":"))
print("%d districts -> %s (%.0f KB)" % (kept, OUT, os.path.getsize(OUT) / 1024))
nums = [f["properties"]["d"] for f in feats]
print("district numbers %d-%d, missing: %s"
      % (min(nums), max(nums), sorted(set(range(1, 66)) - set(nums)) or "none"))
