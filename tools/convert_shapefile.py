#!/usr/bin/env python3
"""Build data/precincts.geojson from the Denver precinct shapefile.

Adds, per precinct:
  * DPS school board district, by largest-area overlap with the DPS Board shapefile
  * active / inactive / total registered voters, from a Colorado SOS monthly
    statistics workbook found under MAPS_DIR (its "Voter Counts by Precinct"
    sheet), or from data/registered_voters.csv as a fallback

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


def load_voter_counts(maps_dir, data_dir):
    """{precinct_code: {"active","inactive","total"}} plus the source label.

    Prefers a Colorado SOS monthly statistics workbook (its "Voter Counts by
    Precinct" sheet keys on the 10-digit precinct code, which matches the
    shapefile's PRECINCT_C exactly). Falls back to a hand-made CSV.
    """
    for xlsx in sorted(glob.glob(os.path.join(maps_dir, "**", "*Statistics*.xlsx"),
                                 recursive=True)):
        got = _from_sos_workbook(xlsx)
        if got:
            return got, os.path.basename(xlsx)

    csv_path = os.path.join(data_dir, "registered_voters.csv")
    got = _from_csv(csv_path)
    if got:
        return got, os.path.basename(csv_path)

    print("  ! no voter data found; registered voters will be blank")
    return {}, ""


def _from_sos_workbook(path, county="Denver"):
    try:
        import openpyxl
    except ImportError:
        print("  ! openpyxl not installed, cannot read %s" % path)
        return {}
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        print("  ! could not open %s: %s" % (path, exc))
        return {}

    sheet = next((n for n in wb.sheetnames if "precinct" in n.lower()), None)
    if not sheet:
        print("  ! %s has no precinct sheet (sheets: %s)" % (path, wb.sheetnames))
        return {}

    out = {}
    for row in wb[sheet].iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 5:
            continue
        if not row[0] or str(row[0]).strip().lower() != county.lower():
            continue
        code = str(row[1]).strip().split(".")[0]
        try:
            active, inactive, total = (int(row[2] or 0), int(row[3] or 0),
                                       int(row[4] or 0))
        except (TypeError, ValueError):
            continue
        out[code] = {"active": active, "inactive": inactive, "total": total}

    if out:
        print("  voter data: %s sheet %r, %d %s precincts"
              % (os.path.basename(path), sheet, len(out), county))
    return out


def _from_csv(path):
    """Fallback CSV: a precinct column (number or 10-digit code) and a count."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
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
    acol = pick(["active"], exclude=("in",)) or pick(["registered", "total", "count"])
    if not pcol or not acol:
        print("  ! could not find precinct/count columns in %s (saw %s)"
              % (path, cols))
        return {}

    out = {}
    for row in rows:
        key = (row.get(pcol) or "").strip().split(".")[0]
        val = (row.get(acol) or "").strip().replace(",", "")
        if not key or not val:
            continue
        try:
            n = int(float(val))
        except ValueError:
            continue
        out[key] = {"active": n, "inactive": 0, "total": n}
    print("  voter data: %s, %d precincts" % (os.path.basename(path), len(out)))
    return out



DISTRICT_LAYERS = [
    ("council", "council", "City Council"),
    ("school",  "school_board", "School Board"),
    ("rtd",     "rtd", "RTD"),
]


def build_districts(feats, out_path):
    """Dissolve precincts into district outlines.

    Denver publishes shapefiles for council and school board but not for RTD,
    and dissolving the precinct layer gives all three from one source. It also
    guarantees each district outline traces precinct lines exactly, so the
    overlays never sit a few metres off the precincts underneath them.
    """
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    groups = {}
    for f in feats:
        props = f["properties"]
        for layer, key, _label in DISTRICT_LAYERS:
            val = props.get(key)
            if val:
                groups.setdefault((layer, val), []).append(_shape(f["geometry"]))

    # Dissolve to polygons first, then emit the BOUNDARY LINES.
    # Emitting polygons draws every interior edge twice, once from each of the
    # two districts that share it, which doubles its apparent weight. Unioning
    # the boundaries collapses each shared edge to a single line.
    by_layer = {}
    for (layer, val), geoms in sorted(groups.items()):
        merged = unary_union([g.buffer(0) for g in geoms])
        # Close hairline slivers where neighbouring precincts share an edge but
        # not identical vertices. Mitred joins with a single quadrant segment
        # keep the vertex count from exploding on a 300-precinct union.
        merged = (merged
                  .buffer(0.0000015, quad_segs=1, join_style=2)
                  .buffer(-0.0000015, quad_segs=1, join_style=2))
        by_layer.setdefault(layer, []).append((val, merged, len(geoms)))

    out = []
    for layer, entries in sorted(by_layer.items()):
        lines = unary_union([e[1].boundary for e in entries])
        lines = lines.simplify(0.00001, preserve_topology=True)
        out.append({
            "type": "Feature",
            "properties": {
                "layer": layer,
                "districts": [e[0] for e in entries],
            },
            "geometry": mapping(lines),
        })

    def rnd(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, (list, tuple)):
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    with open(out_path, "w") as fh:
        json.dump(rnd({"type": "FeatureCollection", "features": out}), fh,
                  separators=(",", ":"))

    print("wrote %s  (%s, %d KB)"
          % (out_path,
             ", ".join("%s: %d districts" % (f["properties"]["layer"],
                                             len(f["properties"]["districts"]))
                       for f in out),
             os.path.getsize(out_path) // 1024))


def main():
    src = os.path.join(MAPS, "Precincts", "ELEC_ELECTIONPRECINCTS_A")
    if not os.path.exists(src + ".shp"):
        sys.exit("FATAL: precinct shapefile not found at %s.shp" % src)

    boards = load_school_board(MAPS)
    voters, voter_source = load_voter_counts(MAPS, os.path.dirname(OUT) or ".")

    r = shapefile.Reader(src)
    fields = [f[0] for f in r.fields[1:]]
    project = to_wgs84(r, src)

    feats, no_board, no_voters = [], [], 0
    for sr in r.shapeRecords():
        rec = dict(zip(fields, list(sr.record)))
        geom = project(shape(sr.shape.__geo_interface__)).buffer(0)

        board = ""
        best = 0.0
        for num, _name, bg in boards:
            try:
                a = geom.intersection(bg).area
            except Exception:
                continue
            if a > best:
                best, board = a, num

        num = str(rec.get("PRECINCT_N", "")).strip()
        if not board:
            no_board.append(num)

        code = str(rec.get("PRECINCT_C", "")).strip()
        reg = voters.get(code) or voters.get(num)
        if reg is None:
            no_voters += 1

        props = {
            "precinct": num,
            "precinct_code": code,
            "cong": str(rec.get("CONG_DIST", "")).strip(),
            "senate": str(rec.get("SENATE_DIS", "")).strip(),
            "house": str(rec.get("HOUSE_DIST", "")).strip(),
            "council": str(rec.get("COUNCIL_DI", "")).strip(),
            "rtd": str(rec.get("RTD_DIST", "")).strip(),
            "school_board": board,
            "neighborhood": str(rec.get("STAT_NBHD", "")).strip(),
        }
        if reg is not None:
            props["active"] = reg["active"]
            props["inactive"] = reg["inactive"]
            props["registered"] = reg["total"]

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
    fc = {"type": "FeatureCollection", "features": feats}
    if voter_source:
        fc["voter_source"] = voter_source
    with open(OUT, "w") as fh:
        json.dump(rnd(fc), fh, separators=(",", ":"))

    build_districts(feats, os.path.join(os.path.dirname(OUT) or ".",
                                        "districts.geojson"))

    print("\nwrote %s  (%d features, %d KB)"
          % (OUT, len(feats), os.path.getsize(OUT) // 1024))
    if no_board:
        print("  ! %d precincts got no school board district: %s"
              % (len(no_board), ", ".join(no_board[:12])))
    if no_voters:
        print("  ! %d precincts have no registered voter count" % no_voters)


if __name__ == "__main__":
    main()
