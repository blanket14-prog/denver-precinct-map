#!/usr/bin/env python3
"""Build data/precincts.geojson from the Denver precinct shapefile.

Adds, per precinct:
  * DPS school board district, by largest-area overlap with the DPS Board shapefile
  * registered voter count, if data/registered_voters.csv is present
    (CSV needs a precinct column and a count column; header names are sniffed)

Usage:
    python3 tools/convert_shapefile.py [MAPS_DIR] [OUT]

MAPS_DIR defaults to ../Maps and must contain:
    Precincts/ELEC_ELECTIONPRECINCTS_A.shp
    DPS Board/geo_export_*.shp        (optional)
"""
import csv
import glob
import json
import os
import sys

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform

MAPS = sys.argv[1] if len(sys.argv) > 1 else "../Maps"
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/precincts.geojson"
VOTER_CSV = os.path.join(os.path.dirname(OUT) or ".", "registered_voters.csv")

SIMPLIFY_DEG = 0.000015  # ~1.5 m


def to_wgs84(reader, path):
    """Return a function that reprojects a shapely geometry to WGS84."""
    prj = path + ".prj"
    if not os.path.exists(prj):
        print("  ! no .prj for %s, assuming WGS84" % path)
        return lambda g: g
    src = CRS.from_wkt(open(prj).read())
    if src.to_epsg() == 4326 or src.is_geographic:
        return lambda g: g
    tr = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)
    return lambda g: shp_transform(tr.transform, g)


def load_school_board(maps_dir):
    """[(district, director_name, geometry)] for the numbered DPS districts."""
    hits = sorted(glob.glob(os.path.join(maps_dir, "DPS Board", "*.shp")))
    if not hits:
        print("  ! no DPS Board shapefile found; school board district will be blank")
        return []
    path = hits[0][:-4]
    r = shapefile.Reader(path)
    fields = [f[0] for f in r.fields[1:]]
    project = to_wgs84(r, path)
    out = []
    for sr in r.shapeRecords():
        rec = dict(zip(fields, list(sr.record)))
        num = str(rec.get("board_dist", "")).strip()
        if num in ("", "0"):
            continue  # at-large directors are citywide, not a district
        out.append((num, str(rec.get("dir_name", "")).strip(),
                    project(shape(sr.shape.__geo_interface__)).buffer(0)))
    print("  school board districts loaded: %d" % len(out))
    return out


def load_voter_counts(path):
    """{precinct: registered_count} from an optional CSV."""
    if not os.path.exists(path):
        print("  ! %s not found; registered voters will be blank" % path)
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("  ! %s is empty" % path)
        return {}
    cols = list(rows[0].keys())

    def pick(cands, exclude=()):
        for want in cands:
            for c in cols:
                k = c.strip().lower()
                if want in k and not any(x in k for x in exclude):
                    return c
        return None

    pcol = pick(["precinct"])
    vcol = (pick(["registered", "total", "active", "voters", "count"],
                 exclude=("precinct",)))
    if not pcol or not vcol:
        print("  ! could not find precinct/count columns in %s (saw %s)" % (path, cols))
        return {}
    print("  voter CSV: precinct column %r, count column %r" % (pcol, vcol))

    counts = {}
    for row in rows:
        raw = (row.get(pcol) or "").strip()
        val = (row.get(vcol) or "").strip().replace(",", "")
        if not raw or not val:
            continue
        # Denver precincts appear as "214" or as the 10-digit code "1310216214"
        key = raw.split(".")[0]
        if len(key) > 4:
            key = key[-3:]
        try:
            counts[key.lstrip("0") or key] = int(float(val))
        except ValueError:
            continue
    print("  voter counts loaded: %d precincts" % len(counts))
    return counts


def main():
    src = os.path.join(MAPS, "Precincts", "ELEC_ELECTIONPRECINCTS_A")
    if not os.path.exists(src + ".shp"):
        sys.exit("FATAL: precinct shapefile not found at %s.shp" % src)

    boards = load_school_board(MAPS)
    voters = load_voter_counts(VOTER_CSV)

    r = shapefile.Reader(src)
    fields = [f[0] for f in r.fields[1:]]
    project = to_wgs84(r, src)

    feats, no_board, no_voters = [], [], 0
    for sr in r.shapeRecords():
        rec = dict(zip(fields, list(sr.record)))
        geom = project(shape(sr.shape.__geo_interface__)).buffer(0)

        board, director = "", ""
        best = 0.0
        for num, name, bg in boards:
            try:
                a = geom.intersection(bg).area
            except Exception:
                continue
            if a > best:
                best, board, director = a, num, name

        num = str(rec.get("PRECINCT_N", "")).strip()
        if not board:
            no_board.append(num)

        reg = voters.get(num)
        if reg is None:
            no_voters += 1

        props = {
            "precinct": num,
            "precinct_code": str(rec.get("PRECINCT_C", "")).strip(),
            "cong": str(rec.get("CONG_DIST", "")).strip(),
            "senate": str(rec.get("SENATE_DIS", "")).strip(),
            "house": str(rec.get("HOUSE_DIST", "")).strip(),
            "council": str(rec.get("COUNCIL_DI", "")).strip(),
            "school_board": board,
            "school_board_director": director,
            "neighborhood": str(rec.get("STAT_NBHD", "")).strip(),
        }
        if reg is not None:
            props["registered"] = reg

        feats.append({"type": "Feature", "properties": props,
                      "geometry": mapping(geom.simplify(SIMPLIFY_DEG,
                                                        preserve_topology=True))})

    feats.sort(key=lambda f: (len(f["properties"]["precinct"]),
                              f["properties"]["precinct"]))

    def rnd(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, (list, tuple)):
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(rnd({"type": "FeatureCollection", "features": feats}), fh,
                  separators=(",", ":"))

    print("\nwrote %s  (%d features, %d KB)"
          % (OUT, len(feats), os.path.getsize(OUT) // 1024))
    if no_board:
        print("  ! %d precincts got no school board district: %s"
              % (len(no_board), ", ".join(no_board[:12])))
    if no_voters:
        print("  ! %d precincts have no registered voter count" % no_voters)


if __name__ == "__main__":
    main()
