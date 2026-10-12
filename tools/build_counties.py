"""Colorado's 64 counties -> data/state_counties.geojson, plus the county
rows in data/results.json (2024 presidential vote).

Boundaries. Built by dissolving the 2020 Census blocks
(tl_2020_08_tabblock20) by county, not from the national cartographic county
file: that one is generalised to 1:500,000 and wanders up to a few hundred
metres off the street a county line actually follows, which shows as soon as
the map is zoomed in next to the House and Senate lines drawn from TIGER.
Blocks nest exactly inside counties and the legislative districts, so all four
maps agree to the metre. Colorado's county lines have not moved since 2020
apart from small Broomfield annexation adjustments.

Each county carries:
  d      county FIPS code (1 = Adams ... 125 = Yuma), the key everywhere else
  name   "Adams", seat, pop (2020 Census), area (square miles, land and water)
  in     {house:[[district, people], ...], senate:[...], congress:[...]}
         the districts the county's residents live in, by 2020 block
         population, largest first. From tools in Maps/Scripts (overlap.py),
         which assigns each block to a district by its internal point.
  c      a colour index 0-4 such that no two neighbouring counties share one

Run:  python3 tools/build_counties.py <dissolved.geojson> <overlap.json> <2024 precinct csv>
"""
import csv, json, os, sys
from collections import defaultdict
import pyproj
from shapely.geometry import shape, mapping
from shapely.ops import transform, polylabel

SRC, OVL, CSV = sys.argv[1:4]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TO_M  = pyproj.Transformer.from_crs("EPSG:4269", "EPSG:26954", always_xy=True).transform
TO_LL = pyproj.Transformer.from_crs("EPSG:26954", "EPSG:4269", always_xy=True).transform
TOLERANCE, PRECISION = 5.0, 6

# FIPS -> (name, county seat)
COUNTIES = {
    1: ("Adams", "Brighton"), 3: ("Alamosa", "Alamosa"), 5: ("Arapahoe", "Littleton"),
    7: ("Archuleta", "Pagosa Springs"), 9: ("Baca", "Springfield"),
    11: ("Bent", "Las Animas"), 13: ("Boulder", "Boulder"),
    14: ("Broomfield", "Broomfield"), 15: ("Chaffee", "Salida"),
    17: ("Cheyenne", "Cheyenne Wells"), 19: ("Clear Creek", "Georgetown"),
    21: ("Conejos", "Conejos"), 23: ("Costilla", "San Luis"), 25: ("Crowley", "Ordway"),
    27: ("Custer", "Westcliffe"), 29: ("Delta", "Delta"), 31: ("Denver", "Denver"),
    33: ("Dolores", "Dove Creek"), 35: ("Douglas", "Castle Rock"), 37: ("Eagle", "Eagle"),
    39: ("Elbert", "Kiowa"), 41: ("El Paso", "Colorado Springs"),
    43: ("Fremont", "Cañon City"), 45: ("Garfield", "Glenwood Springs"),
    47: ("Gilpin", "Central City"), 49: ("Grand", "Hot Sulphur Springs"),
    51: ("Gunnison", "Gunnison"), 53: ("Hinsdale", "Lake City"),
    55: ("Huerfano", "Walsenburg"), 57: ("Jackson", "Walden"), 59: ("Jefferson", "Golden"),
    61: ("Kiowa", "Eads"), 63: ("Kit Carson", "Burlington"), 65: ("Lake", "Leadville"),
    67: ("La Plata", "Durango"), 69: ("Larimer", "Fort Collins"),
    71: ("Las Animas", "Trinidad"), 73: ("Lincoln", "Hugo"), 75: ("Logan", "Sterling"),
    77: ("Mesa", "Grand Junction"), 79: ("Mineral", "Creede"), 81: ("Moffat", "Craig"),
    83: ("Montezuma", "Cortez"), 85: ("Montrose", "Montrose"),
    87: ("Morgan", "Fort Morgan"), 89: ("Otero", "La Junta"), 91: ("Ouray", "Ouray"),
    93: ("Park", "Fairplay"), 95: ("Phillips", "Holyoke"), 97: ("Pitkin", "Aspen"),
    99: ("Prowers", "Lamar"), 101: ("Pueblo", "Pueblo"), 103: ("Rio Blanco", "Meeker"),
    105: ("Rio Grande", "Del Norte"), 107: ("Routt", "Steamboat Springs"),
    109: ("Saguache", "Saguache"), 111: ("San Juan", "Silverton"),
    113: ("San Miguel", "Telluride"), 115: ("Sedgwick", "Julesburg"),
    117: ("Summit", "Breckenridge"), 119: ("Teller", "Cripple Creek"),
    121: ("Washington", "Akron"), 123: ("Weld", "Greeley"), 125: ("Yuma", "Wray"),
}
assert len(COUNTIES) == 64


def round_coords(o):
    if isinstance(o, (list, tuple)):
        if o and isinstance(o[0], (int, float)):
            return [round(float(o[0]), PRECISION), round(float(o[1]), PRECISION)]
        return [round_coords(x) for x in o]
    return o


raw = json.load(open(SRC))["features"]
ovl = json.load(open(OVL))
assert len(raw) == 64

geoms, feats = {}, []
for f in raw:
    d = int(f["properties"]["countyfp"])
    name, seat = COUNTIES[d]
    g = transform(TO_M, shape(f["geometry"]))
    geoms[d] = g
    s = g.simplify(TOLERANCE, preserve_topology=True).buffer(0)
    gg = mapping(transform(TO_LL, s))
    gg["coordinates"] = round_coords(gg["coordinates"])
    big = max(getattr(g, "geoms", [g]), key=lambda q: q.area)
    lp = transform(TO_LL, polylabel(big, tolerance=20.0))
    key = "%03d" % d
    inside = {}
    for ch in ("house", "senate", "congress"):
        rows = sorted(((int(k), v) for k, v in ovl[ch].get(key, {}).items() if v > 0),
                      key=lambda r: -r[1])
        inside[ch] = [list(r) for r in rows]
    pop = f["properties"]["pop2020"]
    assert sum(v for _, v in inside["house"]) == pop, name
    feats.append({"type": "Feature",
                  "properties": {"d": d, "name": name, "seat": seat, "pop": pop,
                                 "label": [round(lp.x, 5), round(lp.y, 5)],
                                 "area": round(g.area / 2.59e6, 1), "in": inside},
                  "geometry": gg})

# Five colours, neighbours never the same: greedy, most-connected first.
nbr = defaultdict(set)
ks = sorted(geoms)
for i, a in enumerate(ks):
    for b in ks[i + 1:]:
        if geoms[a].distance(geoms[b]) < 1:
            nbr[a].add(b); nbr[b].add(a)
colour = {}
for d in sorted(ks, key=lambda k: -len(nbr[k])):
    used = {colour[n] for n in nbr[d] if n in colour}
    colour[d] = min(c for c in range(5) if c not in used)
for f in feats:
    f["properties"]["c"] = colour[f["properties"]["d"]]

feats.sort(key=lambda f: f["properties"]["name"])
path = os.path.join(OUT, "state_counties.geojson")
json.dump({"type": "FeatureCollection",
           "source": "U.S. Census Bureau, 2020 Census blocks dissolved by county",
           "simplified_m": TOLERANCE, "features": feats},
          open(path, "w"), separators=(",", ":"))
print("64 counties -> %s (%.0f KB), %d people" % (
    os.path.basename(path), os.path.getsize(path) / 1024,
    sum(f["properties"]["pop"] for f in feats)))

# ---- 2024 presidential vote by county ---------------------------------------
BY_NAME = {v[0].upper(): k for k, v in COUNTIES.items()}
votes = defaultdict(lambda: defaultdict(int))
cand = {}
for row in csv.DictReader(open(CSV, encoding="utf-8-sig")):
    if row["office"] != "President":
        continue
    d = BY_NAME[row["county"].strip().upper()]
    votes[d][row["party"]] += int(row["votes"] or 0)
    cand[row["party"]] = row["candidate"].split(" / ")[0]
SHORT = {"DEM": ("Harris", "D"), "REP": ("Trump", "R")}
res = {}
for d, v in votes.items():
    total = sum(v.values())
    top = sorted(v.items(), key=lambda kv: -kv[1])
    (wp, wv), (rp, rv) = top[0], top[1]
    res[str(d)] = {"year": 2024, "office": "President",
                   "winner": SHORT[wp][0], "party": SHORT[wp][1], "votes": wv,
                   "runner": SHORT[rp][0], "runner_party": SHORT[rp][1],
                   "runner_votes": rv, "total": total,
                   "margin": round(100.0 * (wv - rv) / total, 2), "uncontested": False}
assert len(res) == 64
rp = os.path.join(OUT, "results.json")
allres = json.load(open(rp))
allres["counties"] = res
json.dump(allres, open(rp, "w"), separators=(",", ":"))
wins = defaultdict(int)
for r in res.values():
    wins[r["winner"]] += 1
print("president 2024 by county:", dict(wins),
      "statewide", {k: sum(v[k] for v in votes.values()) for k in ("DEM", "REP")})
