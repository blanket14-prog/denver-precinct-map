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
import math
import os
import sys

import shapefile
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import linemerge, transform as shp_transform

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
    ("senate",  "senate", "State Senate"),
    ("house",   "house", "State House"),
]



def load_neighborhoods(maps_dir):
    """Denver's 78 statistical neighborhoods, straight from their shapefile.

    Unlike council, school board, RTD, senate and house, neighbourhoods are
    NOT built out of whole precincts: a precinct can straddle a neighbourhood
    line. The precinct shapefile's STAT_NBHD field records only the dominant
    neighbourhood per precinct, so dissolving precincts by it would draw
    boundaries that are visibly wrong. Read the real polygons instead.
    """
    hits = sorted(glob.glob(os.path.join(maps_dir, "Neighborhoods", "*.shp")))
    if not hits:
        print("  ! no Neighborhoods shapefile found; skipping that layer")
        return []
    path = hits[0][:-4]
    r = shapefile.Reader(path)
    fields = [f[0] for f in r.fields[1:]]
    project = to_wgs84(r, path)

    name_field = next((f for f in fields if "NAME" in f.upper()), fields[0])
    out = []
    for sr in r.shapeRecords():
        rec = dict(zip(fields, list(sr.record)))
        name = str(rec.get(name_field, "")).strip()
        if not name:
            continue
        out.append((name, shapely.set_precision(
            project(shape(sr.shape.__geo_interface__)).buffer(0), 1e-7)))
    print("  neighbourhoods loaded: %d" % len(out))
    return out


def build_districts(feats, exact_geoms, out_path, maps_dir=None):
    """Dissolve precincts into district outlines.

    Denver publishes shapefiles for council and school board but not for RTD,
    and dissolving the precinct layer gives all three from one source. It also
    guarantees each district outline traces precinct lines exactly, so the
    overlays never sit a few metres off the precincts underneath them.
    """
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    # Snap every precinct to a common grid (about 1 cm) before dissolving.
    # Neighbouring precincts then share bitwise-identical vertices, so the
    # dissolve is exact and needs no closing buffer. The old positive-then-
    # negative buffer was what created the pinprick holes and specks in the
    # first place.
    GRID = 1e-7

    groups = {}
    for f, exact in zip(feats, exact_geoms):
        props = f["properties"]
        # NB: the exact polygon, not f["geometry"]. The stored geometry is
        # simplified per precinct, and simplifying neighbours independently
        # pulls their shared edge apart. Dissolving those leaves a sliver
        # between every pair of precincts, and the slivers render as short
        # detached dashes all over the interior of a district.
        geom = shapely.set_precision(exact, GRID)
        for layer, key, _label in DISTRICT_LAYERS:
            val = props.get(key)
            if val:
                groups.setdefault((layer, val), []).append(geom)

    # Dissolve to polygons first, then emit the BOUNDARY LINES.
    # Emitting polygons draws every interior edge twice, once from each of the
    # two districts that share it, which doubles its apparent weight. Unioning
    # the boundaries collapses each shared edge to a single line.
    from shapely.geometry import Polygon, MultiPolygon, Point

    # Degenerate rings and specks left by floating-point noise. A tenth of a
    # square metre: far below any real enclave or island, far above a
    # rounding artefact.
    SPECK = 1e-11

    def clean(geom):
        """Drop only degenerate rings and specks, keeping real holes.

        Denver's council districts 2, 4 and 6, senate 26 and 32, house 1, 3
        and 9 and RTD A, D and E all have genuine holes or detached parts.
        An earlier version stripped every interior ring, which deleted the
        boundary around each enclave from the district that surrounds it
        while the neighbouring district still traced it, leaving lines on
        the map that looked orphaned.
        """
        parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        kept = []
        for part in parts:
            if part.is_empty or part.area < SPECK:
                continue
            holes = [ring for ring in part.interiors
                     if Polygon(ring).area >= SPECK]
            kept.append(Polygon(part.exterior, holes))
        if not kept:
            return geom
        return kept[0] if len(kept) == 1 else MultiPolygon(kept)

    # A district gets a second (or third) number when part of it sits far
    # from the first one. Senate 26 runs the whole southern edge of the
    # county, so a single label near Hampden left Bear Valley and Marston
    # looking unlabelled; school board 4, RTD B and council 11 have the same
    # problem with the Green Valley Ranch arm. Anything under the threshold
    # keeps exactly one label.
    LABEL_SPREAD_KM = 9.0
    MAX_LABELS = 2
    MIN_LABEL_PRECINCTS = 3
    KM_LON = 85.6                     # at Denver's latitude
    KM_LAT = 111.0

    def _km(a, b):
        return math.hypot((a.x - b.x) * KM_LON, (a.y - b.y) * KM_LAT)

    def _mean(pts):
        return Point(sum(p.x for p in pts) / len(pts),
                     sum(p.y for p in pts) / len(pts))

    def label_points(geom, precinct_geoms):
        """Where to put the district's big number(s).

        The pole of inaccessibility alone puts council 11, school board 4 and
        RTD B out in the middle of DIA, because the airport is a huge empty
        polygon that dominates the district's area. Aim instead for the mean
        of the district's precinct centres, which sits where the precincts
        actually are, and fall back to the pole when that mean lands outside
        a concave district.
        """
        parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        main = max(parts, key=lambda g: g.area)

        pts = [g.centroid for g in precinct_geoms]
        first = None
        if pts:
            mean = _mean(pts)
            if geom.contains(mean):
                first = mean
        if first is None:
            try:
                from shapely.ops import polylabel
                first = polylabel(main, tolerance=0.0005)
            except Exception:
                first = main.representative_point()

        # representative_point is inside its precinct, so any label placed on
        # one is guaranteed to land inside the district.
        cands = [g.representative_point() for g in precinct_geoms]
        labels = [first]
        while len(labels) < MAX_LABELS and cands:
            far = max(cands, key=lambda c: min(_km(c, l) for l in labels))
            if min(_km(far, l) for l in labels) < LABEL_SPREAD_KM:
                break
            # Centre the new label on the precincts it speaks for rather than
            # leaving it on the outermost one.
            own = [c for c in cands
                   if _km(c, far) < min(_km(c, l) for l in labels)]
            # One outlying precinct is usually the airport or a rail yard:
            # a number floating out there reads as noise, not a district.
            if len(own) < MIN_LABEL_PRECINCTS:
                break
            centre = _mean(own)
            if not geom.contains(centre):
                # concave arm: the mean can fall outside it, so snap to the
                # precinct nearest the mean rather than to the far corner
                centre = min(own, key=lambda c: _km(c, centre))
            # Recentring pulls the label back toward the rest of the
            # district; if it lands close to a label already there, one
            # number was enough.
            if min(_km(centre, l) for l in labels) < LABEL_SPREAD_KM:
                break
            labels.append(centre)
        return labels

    by_layer = {}
    for (layer, val), geoms in sorted(groups.items()):
        merged = clean(unary_union(geoms))
        by_layer.setdefault(layer, []).append((val, merged, geoms))

    out = []
    for layer, entries in sorted(by_layer.items()):
        # The union nodes the boundaries at every precinct corner, leaving
        # thousands of two-point stubs that simplify cannot touch because it
        # preserves each part's endpoints. linemerge stitches them back into
        # continuous runs between real junctions first.
        lines = linemerge(unary_union([e[1].boundary for e in entries]))
        lines = lines.simplify(0.00002, preserve_topology=True)   # ~2 m
        out.append({
            "type": "Feature",
            "properties": {
                "kind": "outline",
                "layer": layer,
                "districts": [e[0] for e in entries],
            },
            "geometry": mapping(lines),
        })
        for val, poly, precinct_geoms in entries:
            for pt in label_points(poly, precinct_geoms):
                out.append({
                    "type": "Feature",
                    "properties": {"kind": "label", "layer": layer,
                                   "district": val,
                                   "precincts": len(precinct_geoms)},
                    "geometry": mapping(Point(pt.x, pt.y)),
                })

    # A world rectangle with the county cut out of it. Filled on the map, this
    # greys everything outside Denver so the county reads as the subject.
    #
    # Cut with a real difference rather than by hand-assembling the county's
    # rings as holes. Denver's outline pinches to a point in the southwest, so
    # a hand-built ring self-intersects there and the result is invalid
    # geometry that only renders correctly because the canvas happens to use an
    # even-odd fill. difference() is valid by construction, the same size, and
    # it keeps the enclaves without a size floor: they are holes in the county,
    # so cutting the county leaves them filled, which is what greying Glendale
    # and its six smaller neighbours requires.
    county = clean(unary_union(
        [shapely.set_precision(g, GRID) for g in exact_geoms]))
    world = Polygon([(-180.0, -85.0), (180.0, -85.0),
                     (180.0, 85.0), (-180.0, 85.0)])
    # Snap to the same grid the output is rounded to. Denver's outline pinches
    # in the southwest, and rounding an unsnapped mask to six decimals closes
    # that pinch into a self-intersection: valid before writing, invalid after.
    # Snapping first lets GEOS resolve the topology at the precision that will
    # actually be stored.
    mask = shapely.set_precision(world.difference(county), 1e-6)
    county_parts = (county.geoms if isinstance(county, MultiPolygon)
                    else [county])
    enclaves = sum(len(p.interiors) for p in county_parts)
    print("  county mask: %d county part(s), %d enclave(s) greyed, valid: %s"
          % (len(county_parts), enclaves, mask.is_valid))
    if not mask.is_valid:
        sys.exit("FATAL: the county mask came out invalid")
    out.append({
        "type": "Feature",
        "properties": {"kind": "mask", "layer": "mask"},
        "geometry": mapping(mask),
    })

    # Validate what actually gets written, not what was built. Rounding is the
    # step that broke the mask, so the check belongs after it.
    def audit(features, label):
        bad = []
        for f in features:
            g = shape(rnd(f["geometry"]))
            if not g.is_valid or g.is_empty:
                bad.append("%s %s" % (f["properties"].get("kind", ""),
                                      f["properties"].get("layer", "")))
        if bad:
            sys.exit("FATAL: %s has invalid geometry after rounding: %s"
                     % (label, ", ".join(bad[:6])))
        print("  %s: %d features, all valid at output precision"
              % (label, len(features)))

    if maps_dir:
        hoods = load_neighborhoods(maps_dir)
        if hoods:
            polys = [clean(g) for _n, g in hoods]
            lines = linemerge(unary_union([g.boundary for g in polys]))
            # 78 outlines is a lot of linework for a contextual layer, so
            # simplify these harder than the district boundaries (~3 m).
            lines = lines.simplify(0.00003, preserve_topology=True)
            out.append({
                "type": "Feature",
                "properties": {"kind": "outline", "layer": "nbhd",
                               "districts": [n for n, _g in hoods]},
                "geometry": mapping(lines),
            })
            for (name, _orig), poly in zip(hoods, polys):
                pt = label_points(poly, [])[0]
                out.append({
                    "type": "Feature",
                    "properties": {"kind": "label", "layer": "nbhd",
                                   "district": name},
                    "geometry": mapping(Point(pt.x, pt.y)),
                })

    def rnd(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, (list, tuple)):
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    audit(out, os.path.basename(out_path))
    with open(out_path, "w") as fh:
        json.dump(rnd({"type": "FeatureCollection", "features": out}), fh,
                  separators=(",", ":"))

    print("wrote %s  (%s, %d KB)"
          % (out_path,
             ", ".join("%s: %d districts" % (f["properties"]["layer"],
                                             len(f["properties"]["districts"]))
                       for f in out if f["properties"]["kind"] == "outline"),
             os.path.getsize(out_path) // 1024))


def apply_election_districts(feats, exact_geoms, elections_path):
    """Correct school_board to the map the 2025 election actually ran on.

    The DPS board adopted a new district map in April 2024, so the Denver Open
    Data director-district shapefile -- which matches the 2023 District 1 and
    District 5 ballots exactly, 134 precincts of 134 -- is a cycle out of date.
    The election results pin the new map down without it:

      * a precinct on the 2025 District 2, 3 or 4 ballot is in that district,
        and nothing here changes that;
      * a precinct on the 2023 District 1 or 5 ballot that no 2025 contest
        claimed is provisionally still in that district;
      * whatever is left is a precinct that left District 3 or 4 for 1 or 5.

    Districts 1 and 5 are the blind spot, since neither seat was on the 2025
    ballot, and contiguity is what closes it. Every district in the 2023 map is
    a single connected piece, so a precinct with no neighbour in its own
    district has been placed wrong, and since it cannot be in 2, 3 or 4 without
    having appeared on that ballot, the other of 1 and 5 is the only answer
    left. That is how precinct 212 in Speer resolves: all four of its old
    District 5 neighbours moved to District 2, stranding it, and its remaining
    neighbours are 1, 2 and 3.

    Contiguity here means sharing any boundary at all, including a single
    corner, which is what the real maps use: precinct 801 in Cole reaches the
    rest of its 2023 district only through a corner, as does the Indian Creek
    trio 915-917.

    What survives is a trade between Districts 1 and 5 that leaves both sides
    connected. Replacing the shapefile with a current export makes all of this
    a no-op.
    """
    try:
        with open(elections_path) as fh:
            doc = json.load(fh)
    except OSError:
        print("  ! no elections.json; school board districts stay on the "
              "shapefile's map")
        return

    contests = {c["key"]: c for c in doc.get("contests", [])}
    need = ("d2_2025", "d3_2025", "d4_2025", "d1_2023", "d5_2023")
    if not all(k in contests for k in need):
        print("  ! elections.json is missing a district contest; school board "
              "districts stay on the shapefile's map")
        return

    fixed = {}
    for key, dist in (("d2_2025", "2"), ("d3_2025", "3"), ("d4_2025", "4")):
        for num in contests[key]["precincts"]:
            fixed[num] = dist

    new = dict(fixed)
    for key, dist in (("d1_2023", "1"), ("d5_2023", "5")):
        for num in contests[key]["precincts"]:
            new.setdefault(num, dist)

    nums = [f["properties"]["precinct"] for f in feats]
    adj = {n: set() for n in nums}
    for i, a in enumerate(nums):
        for j in range(i + 1, len(nums)):
            b = nums[j]
            if exact_geoms[i].intersects(exact_geoms[j]):
                adj[a].add(b)
                adj[b].add(a)

    # Precincts that left District 3 or 4: place each by the district its
    # cluster can actually reach.
    unplaced = [n for n in nums if n not in new]
    for start in unplaced:
        seen, stack, touch = {start}, [start], set()
        while stack:
            for nb in adj[stack.pop()]:
                if nb in new:
                    if new[nb] in ("1", "5"):
                        touch.add(new[nb])
                elif nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(touch) != 1:
            sys.exit("FATAL: precinct %s left its 2023 district and touches %s,"
                     " so contiguity cannot place it. Get a current DPS "
                     "director-district export."
                     % (start, "/".join(sorted(touch)) or "no district"))
        new[start] = touch.pop()

    # Repair anything the 2023 assignment stranded. Only 1 and 5 can move: a
    # precinct in 2, 3 or 4 was seen on that ballot.
    repaired = []
    for _pass in range(len(nums)):
        stranded = [n for n in nums
                    if new[n] in ("1", "5")
                    and not any(new[m] == new[n] for m in adj[n])]
        if not stranded:
            break
        for n in stranded:
            other = "5" if new[n] == "1" else "1"
            if not any(new[m] == other for m in adj[n]):
                sys.exit("FATAL: precinct %s has no neighbour in District 1 or"
                         " 5, so contiguity cannot place it. Get a current DPS"
                         " director-district export." % n)
            repaired.append((n, new[n], other))
            new[n] = other
    else:
        sys.exit("FATAL: school board district repair did not settle")

    moved = []
    for f in feats:
        props = f["properties"]
        was = props.get("school_board", "")
        now = new[props["precinct"]]
        if was and was != now:
            props["school_board_2023"] = was
            moved.append((props["precinct"], was, now))
        props["school_board"] = now

    print("  school board districts set from election results: %d precincts "
          "moved since the 2023 map" % len(moved))
    if unplaced:
        print("      %d placed by contiguity after leaving District 3 or 4: %s"
              % (len(unplaced), ", ".join(sorted(unplaced, key=int))))
    for num, was, _now in repaired:
        print("      precinct %s was stranded in District %s and can only be "
              "in District %s" % (num, was, new[num]))
    for num, was, now in sorted(moved, key=lambda t: int(t[0])):
        print("      precinct %-5s D%s -> D%s" % (num, was, now))


def main():
    src = os.path.join(MAPS, "Precincts", "ELEC_ELECTIONPRECINCTS_A")
    if not os.path.exists(src + ".shp"):
        sys.exit("FATAL: precinct shapefile not found at %s.shp" % src)

    boards = load_school_board(MAPS)
    voters, voter_source = load_voter_counts(MAPS, os.path.dirname(OUT) or ".")

    r = shapefile.Reader(src)
    fields = [f[0] for f in r.fields[1:]]
    project = to_wgs84(r, src)

    feats, exact_geoms, no_board, no_voters = [], [], [], 0
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

        # anchor for the big precinct number: a point guaranteed inside the
        # shape, so the label never lands outside a concave precinct
        try:
            from shapely.geometry import MultiPolygon as _MP
            main = (max(geom.geoms, key=lambda g: g.area)
                    if isinstance(geom, _MP) else geom)
            anchor = main.representative_point()
            props["label"] = [round(anchor.x, 6), round(anchor.y, 6)]
        except Exception as exc:
            print("  ! no label anchor for precinct %s: %s" % (num, exc))

        feats.append({"type": "Feature", "properties": props,
                      "geometry": mapping(geom.simplify(SIMPLIFY_DEG,
                                                        preserve_topology=True))})
        exact_geoms.append(geom)

    order = sorted(range(len(feats)),
                   key=lambda i: (len(feats[i]["properties"]["precinct"]),
                                  feats[i]["properties"]["precinct"]))
    feats = [feats[i] for i in order]
    exact_geoms = [exact_geoms[i] for i in order]

    def rnd(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, (list, tuple)):
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    # Do this before anything downstream reads school_board: the district
    # overlay and the zoom-to-a-district menu are both dissolved from it.
    apply_election_districts(
        feats, exact_geoms,
        os.path.join(os.path.dirname(OUT) or ".", "elections.json"))

    bad = [f["properties"]["precinct"] for f in feats
           if not shape(rnd(f["geometry"])).is_valid]
    if bad:
        sys.exit("FATAL: precincts invalid after rounding: %s" % bad[:8])
    print("  precincts: %d features, all valid at output precision" % len(feats))

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    fc = {"type": "FeatureCollection", "features": feats}
    if voter_source:
        fc["voter_source"] = voter_source
    with open(OUT, "w") as fh:
        json.dump(rnd(fc), fh, separators=(",", ":"))

    build_districts(feats, exact_geoms,
                    os.path.join(os.path.dirname(OUT) or ".",
                                 "districts.geojson"), MAPS)

    print("\nwrote %s  (%d features, %d KB)"
          % (OUT, len(feats), os.path.getsize(OUT) // 1024))
    if no_board:
        print("  ! %d precincts got no school board district: %s"
              % (len(no_board), ", ".join(no_board[:12])))
    if no_voters:
        print("  ! %d precincts have no registered voter count" % no_voters)


if __name__ == "__main__":
    main()
