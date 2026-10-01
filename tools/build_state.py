"""Colorado legislative districts -> data/state_house.geojson, state_senate.geojson.

Source: Census TIGER/Line 2024 state legislative districts (tl_2024_08_sldl
for the House, tl_2024_08_sldu for the Senate).

On simplification. An earlier build used a 120 m tolerance, which moved some
boundaries by as much as 79 m and replaced real stair-steps along streets with
straight diagonals cutting through blocks. At 5 m the worst displacement is
under 6 m, which is sub-pixel even at the map's deepest zoom, and the saving is
still large because TIGER carries many collinear vertices: House District 2
goes from 622 points to 47 without a visible change.

The files are ~1.1 MB and ~750 KB, served gzipped at about 30% of that.

Run:  python3 tools/build_state.py [directory holding the two shapefiles]
"""
import json, os, sys
import shapefile, pyproj
from shapely.geometry import shape, mapping
from shapely.ops import transform

SRC = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Maps"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

TO_M  = pyproj.Transformer.from_crs("EPSG:4269", "EPSG:26954", always_xy=True).transform
TO_LL = pyproj.Transformer.from_crs("EPSG:26954", "EPSG:4269", always_xy=True).transform
TOLERANCE = 5.0        # metres
PRECISION = 6          # decimal places, about 0.1 m

CHAMBERS = [
    ("house",  SRC + "/State House/tl_2024_08_sldl",  "SLDLST", 65),
    ("senate", SRC + "/State Senate/tl_2024_08_sldu", "SLDUST", 35),
]

def round_coords(o):
    if isinstance(o, (list, tuple)):
        if o and isinstance(o[0], (int, float)):
            return [round(float(o[0]), PRECISION), round(float(o[1]), PRECISION)]
        return [round_coords(x) for x in o]
    return o

for name, src, fld, expect in CHAMBERS:
    r = shapefile.Reader(src)
    feats, worst = [], 0.0
    for rec, shp in zip(r.records(), r.shapes()):
        num = int(rec[fld])
        g = transform(TO_M, shape(shp.__geo_interface__).buffer(0))
        s = g.simplify(TOLERANCE, preserve_topology=True).buffer(0)
        if s.is_empty:
            s = g
        try:
            worst = max(worst, g.exterior.hausdorff_distance(s.exterior))
        except Exception:
            pass                      # multipart districts have no single exterior
        gg = mapping(transform(TO_LL, s))
        gg["coordinates"] = round_coords(gg["coordinates"])
        pt = transform(TO_LL, g.representative_point())
        feats.append({
            "type": "Feature",
            "properties": {"d": num,
                           "label": [round(pt.x, 5), round(pt.y, 5)],
                           "area": round(g.area / 2.59e6, 1)},
            "geometry": gg,
        })
    feats.sort(key=lambda f: f["properties"]["d"])
    nums = [f["properties"]["d"] for f in feats]
    assert nums == list(range(1, expect + 1)), "%s: expected 1-%d, got %s" % (name, expect, nums)

    path = os.path.join(OUT, "state_%s.geojson" % name)
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "source": "U.S. Census Bureau TIGER/Line 2024",
                   "simplified_m": TOLERANCE,
                   "features": feats}, fh, separators=(",", ":"))
    print("%-7s %2d districts -> %s (%.0f KB, worst shift %.1f m)"
          % (name, len(feats), os.path.basename(path),
             os.path.getsize(path) / 1024, worst))
