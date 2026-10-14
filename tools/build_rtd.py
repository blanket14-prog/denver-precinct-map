"""RTD Board of Directors districts -> data/state_rtd.geojson, plus the
directors in data/legislators.json (key "rtd") and an "rtd" list on every
county in data/state_counties.geojson.

Source: RTD's own ArcGIS service behind the district map on rtd-denver.com,
RTD_GIS_Boundaries/FeatureServer/1 (DirectorDistricts), the 2022 plan used
from the 2022 election on. The service's director names lag (it still has
Vince Buzek in J), so the names here come from the Board of Directors page.

Fifteen districts, A to O. Keyed 1-15 (A = 1) like every other chamber on
the page; people only ever see the letter.

Population per district and county, by 2020 Census block, comes from
Maps/Scripts/rtd_overlap.py.

Run:  python3 tools/build_rtd.py <rtd_director_districts.geojson> <rtd_district_county_population.json>
"""
import json, os, sys
import pyproj
from shapely.geometry import shape, mapping
from shapely.ops import transform, polylabel

SRC, POP = sys.argv[1:3]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TO_M  = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:26954", always_xy=True).transform
TO_LL = pyproj.Transformer.from_crs("EPSG:26954", "EPSG:4326", always_xy=True).transform
TOLERANCE, PRECISION = 5.0, 6
LETTERS = "ABCDEFGHIJKLMNO"

# rtd-denver.com/about-rtd/board-of-directors, October 2026. Elected in 2024
# -> seat next up November 2028; elected in 2022 or appointed since -> on
# the November 2026 ballot. Candidates from Denverite's 2026 voter guide.
DIRECTORS = {
    "A": ("Chris Nicholson", 2028), "B": ("JoyAnn Ruscha", 2026),
    "C": ("Michael Guzman", 2026), "D": ("Chris Gutschenritter", 2028),
    "E": ("Matt Larsen", 2028), "F": ("Kathleen Chandler", 2028),
    "G": ("Julien Bouquet", 2028), "H": ("Patrick O'Keefe", 2028),
    "I": ("Karen Benker", 2028), "J": ("Joyce Downing", 2026),
    "K": ("Troy L. Whitmore", 2026), "L": ("Ian Harwick", 2026),
    "M": ("Brett Paglieri", 2028), "N": ("Cindy Arcuri", 2026),
    "O": ("Lynn Guissinger", 2026),
}
NOTES = {"J": "Appointed to fill a vacancy.", "N": "Appointed to fill a vacancy."}
BALLOT = {
    "B": ["John Glenn", "Bianka Emerson"], "C": ["Joe Meyer"],
    "J": ["Coleman Erickson"], "K": ["Michael Farrington", "Jessica Sandgren"],
    "L": ["Ian Harwick"], "N": ["Cindy Arcuri"],
    "O": ["Jack Rosenthal", "Audrey DeBarros"],
}

src = [f for f in json.load(open(SRC))["features"] if f["properties"]["BND"] in DIRECTORS]
pops = json.load(open(POP))
assert len(src) == 15

feats = []
for f in src:
    L = f["properties"]["BND"]
    d = LETTERS.index(L) + 1
    g = transform(TO_M, shape(f["geometry"]).buffer(0))
    s = g.simplify(TOLERANCE, preserve_topology=True).buffer(0)
    gg = mapping(transform(TO_LL, s))
    def rnd(o):
        if o and isinstance(o[0], (int, float)):
            return [round(o[0], PRECISION), round(o[1], PRECISION)]
        return [rnd(x) for x in o]
    gg["coordinates"] = rnd(gg["coordinates"])
    big = max(getattr(g, "geoms", [g]), key=lambda q: q.area)
    lp = transform(TO_LL, polylabel(big, tolerance=20.0))
    name, up = DIRECTORS[L]
    feats.append({"type": "Feature", "properties": {
        "d": d, "name": L, "pop": pops[L]["pop"], "up": up,
        "ballot": BALLOT.get(L, []),
        "label": [round(lp.x, 5), round(lp.y, 5)],
        "area": round(g.area / 2.59e6, 1)}, "geometry": gg})
# Five colours, neighbours never the same.
gs = {f["properties"]["d"]: shape(f["geometry"]) for f in feats}
nbr = {d: {e for e in gs if e != d and gs[d].distance(gs[e]) < 1e-4} for d in gs}
col = {}
for d in sorted(gs, key=lambda k: -len(nbr[k])):
    used = {col[n] for n in nbr[d] if n in col}
    col[d] = min(c for c in range(5) if c not in used)
for f in feats:
    f["properties"]["c"] = col[f["properties"]["d"]]
feats.sort(key=lambda f: f["properties"]["d"])
path = os.path.join(OUT, "state_rtd.geojson")
json.dump({"type": "FeatureCollection",
           "source": "Regional Transportation District, RTD_GIS_Boundaries/DirectorDistricts",
           "simplified_m": TOLERANCE, "features": feats},
          open(path, "w"), separators=(",", ":"))
print("15 RTD districts -> %s (%.0f KB)" % (os.path.basename(path), os.path.getsize(path) / 1024))

# Directors into the member list. No party: the board is nonpartisan.
mp = os.path.join(OUT, "legislators.json")
mem = json.load(open(mp))
mem["rtd"] = {str(LETTERS.index(L) + 1): [n] + ([""] + [NOTES[L]] if L in NOTES else [""])
              for L, (n, _) in DIRECTORS.items()}
mem["rtd_source"] = ("RTD Board of Directors page, October 2026; 2026 candidates "
                     "from Denverite's voter guide.")
json.dump(mem, open(mp, "w"), separators=(",", ":"), ensure_ascii=False)

# Each county learns which RTD districts its residents live in. Counts under
# 50 people are where RTD's lines, drawn on their own, graze a block that is
# really across the county line; they are left out.
cp = os.path.join(OUT, "state_counties.geojson")
cty = json.load(open(cp))
for f in cty["features"]:
    key = "%03d" % f["properties"]["d"]
    rows = [[LETTERS.index(L) + 1, v["counties"][key]] for L, v in pops.items()
            if v["counties"].get(key, 0) >= 50]
    f["properties"]["in"]["rtd"] = sorted(rows)
json.dump(cty, open(cp, "w"), separators=(",", ":"))
print("counties with RTD:", sorted(f["properties"]["name"] for f in cty["features"] if f["properties"]["in"]["rtd"]))
