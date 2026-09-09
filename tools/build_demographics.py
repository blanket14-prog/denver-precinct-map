#!/usr/bin/env python3
"""Build data/tracts.geojson and data/demographics.json from Census files.

Census data is published by tract, block group and block, never by voting
precinct. Blocks nest inside Denver's precincts, so block-level counts could be
summed exactly, but ACS estimates are not published at block level. Rather than
apportion tract estimates onto precincts and manufacture a precision the survey
does not have, these layers are drawn on their own geography, and every value
carries the margin of error the Bureau published with it.

Usage:
    python3 tools/build_demographics.py [DEMOGRAPHICS_DIR]

The directory holds the TIGER/Line tract shapefile for Colorado
(tl_<year>_08_tract.shp) and one or more data.census.gov table exports
(ACSDT5Y2024.<table>-Data.csv, ACSST5Y2024.<table>-Data.csv).
"""

import csv
import glob
import json
import os
import sys

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping, Point
from shapely.ops import transform as shp_transform

SRC = sys.argv[1] if len(sys.argv) > 1 else "../Demographics"
OUT_DIR = "data"
DENVER_COUNTY = "031"
SIMPLIFY_DEG = 0.00002          # ~2 m; tracts are large, lines stay clean

# Each measure names the ACS table, the estimate variable inside it, and how to
# read it. "pct" divides one variable by another and reports a share.
MEASURES = [
    {"key": "income", "table": "B19013", "var": "B19013_001",
     "label": "Median household income", "fmt": "usd",
     "note": "Median household income in the past 12 months, "
             "in 2024 inflation-adjusted dollars"},
    {"key": "poverty", "table": "S1701", "var": "S1701_C03_001",
     "label": "Poverty rate", "fmt": "pct_direct",
     "note": "Share of people whose income in the past 12 months was below "
             "the poverty level"},
    {"key": "kids", "table": "B11005", "var": "B11005_002",
     "denom": "B11005_001", "label": "Households with children",
     "fmt": "pct", "note": "Share of households with one or more people "
                           "under 18"},
    {"key": "school", "table": "B14001", "var": "B14001_002",
     "denom": "B14001_001", "label": "Enrolled in school", "fmt": "pct",
     "note": "Share of the population 3 years and over enrolled in school, "
             "nursery through graduate"},
    {"key": "renters", "table": "B25003", "var": "B25003_003",
     "denom": "B25003_001", "label": "Renter-occupied homes", "fmt": "pct",
     "note": "Share of occupied housing units that are rented"},
    {"key": "language", "table": "C16001", "var": "C16001_002",
     "denom": "C16001_001", "label": "Speak a language other than English",
     "fmt": "pct_inv",
     "note": "Share of people 5 years and over who speak a language other "
             "than English at home"},
    {"key": "pop", "table": "B01003", "var": "B01003_001",
     "label": "Total population", "fmt": "count",
     "note": "Total population"},
]


def load_tracts(src):
    hits = sorted(glob.glob(os.path.join(src, "tl_*_08_tract.shp")))
    if not hits:
        raise SystemExit("no tl_<year>_08_tract.shp under %s" % src)
    path = hits[-1]
    r = shapefile.Reader(path)
    src_crs = CRS.from_wkt(open(path[:-4] + ".prj").read())
    tr = Transformer.from_crs(src_crs, CRS.from_epsg(4326), always_xy=True)
    fields = [f[0] for f in r.fields[1:]]

    feats = {}
    for sr in r.shapeRecords():
        rec = dict(zip(fields, sr.record))
        if str(rec.get("COUNTYFP", "")).strip() != DENVER_COUNTY:
            continue
        geom = shape(sr.shape.__geo_interface__)
        if src_crs.to_epsg() != 4326:
            geom = shp_transform(tr.transform, geom)
        geom = geom.buffer(0).simplify(SIMPLIFY_DEG, preserve_topology=True)
        geoid = str(rec["GEOID"]).strip()
        feats[geoid] = {
            "type": "Feature",
            "properties": {"geoid": geoid,
                           "name": str(rec.get("NAMELSAD", "")).strip()},
            "geometry": mapping(geom),
        }
    print("  %s: %d Denver tracts" % (os.path.basename(path), len(feats)))
    return feats


def read_table(src, table):
    """{geoid: {variable: (estimate, margin)}} from a data.census.gov export."""
    hits = sorted(glob.glob(os.path.join(src, "*%s-Data.csv" % table)))
    if not hits:
        return None
    with open(hits[-1], encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    head = [h.strip() for h in rows[0]]
    out = {}

    def parse(raw):
        """(value, capped) from an ACS cell.

        The Bureau top-codes a median it will not publish exactly: Washington
        Park and Hilltop both come through as "250,000+" rather than a figure.
        Those are the two richest tracts in Denver, not missing data, so they
        are read at the cap and flagged. Blanks, "-", "N" and "(X)" really are
        absent, and a margin of "**" or "***" means none was published.
        """
        txt = (raw or "").strip().replace(",", "")
        if txt in ("", "-", "N", "(X)", "*", "**", "***", "null"):
            return None, False
        capped = txt.endswith(("+", "-")) and len(txt) > 1
        if capped:
            txt = txt[:-1]
        try:
            num = float(txt)
        except ValueError:
            return None, False
        # -666666666 and friends are the Bureau's own suppression markers
        if num <= -999999:
            return None, False
        return num, capped
    # Row 0 is the machine header, row 1 repeats it in prose; data starts at 2.
    for row in rows[2:]:
        if not row or not row[0].strip():
            continue
        rec = dict(zip(head, row))
        geoid = rec["GEO_ID"].split("US")[-1]
        vals = {}
        for col, raw in rec.items():
            if not col.endswith(("E", "M")) or "_" not in col:
                continue
            var, kind = col[:-1], col[-1]
            num, capped = parse(raw)
            slot = vals.setdefault(var, {})
            slot[kind] = num
            if kind == "E" and capped:
                slot["capped"] = True
        out[geoid] = vals
    return out


def main():
    print("reading %s" % SRC)
    tracts = load_tracts(SRC)

    measures, missing = [], []
    for m in MEASURES:
        data = read_table(SRC, m["table"])
        if data is None:
            missing.append(m["table"])
            continue

        values, unmatched, suppressed = {}, 0, 0
        for geoid, vals in data.items():
            if geoid not in tracts:
                unmatched += 1
                continue
            slot = vals.get(m["var"], {})
            est, moe, capped = slot.get("E"), slot.get("M"), slot.get("capped")
            if est is None:
                suppressed += 1
                continue

            if m["fmt"] in ("usd", "count", "pct_direct"):
                cell = [round(est, 1), None if moe is None else round(moe, 1)]
                if capped:
                    cell.append(1)
                values[geoid] = cell
                continue

            denom = vals.get(m["denom"], {}).get("E")
            if not denom:
                suppressed += 1
                continue
            share = 100.0 * est / denom
            if m["fmt"] == "pct_inv":       # variable counts English-only
                share = 100.0 - share
            # The denominator is itself an estimate, but its margin is small
            # next to the numerator's, so this is the ratio's margin to a good
            # approximation. Flagged in the note rather than hidden.
            band = None if moe is None else round(100.0 * moe / denom, 1)
            values[geoid] = [round(share, 1), band]

        if unmatched:
            print("  ! %s: %d rows outside Denver County, skipped"
                  % (m["table"], unmatched))
        wide = sum(1 for v in values.values()
                   if v[1] and v[0] and abs(v[1] / v[0]) > .30)
        capped_n = sum(1 for v in values.values() if len(v) > 2)
        print("  %-9s %-34s %3d tracts%s%s%s"
              % (m["table"], m["label"], len(values),
                 ", %d with no estimate" % suppressed if suppressed else "",
                 ", %d at the published cap" % capped_n if capped_n else "",
                 ", %d with a margin over 30%%" % wide if wide else ""))

        out = dict(m)
        out.pop("denom", None)
        out["values"] = values
        measures.append(out)

    if missing:
        print("  ! not downloaded yet: %s" % ", ".join(missing))
    if not measures:
        raise SystemExit("no ACS tables found under %s" % SRC)

    # Which tract each precinct sits in, so the detail bar can name it. This
    # is the tract containing the precinct's centre, NOT a value for the
    # precinct: tracts and precincts cut across each other, and the readout
    # says which tract it is showing.
    precinct_tract = {}
    try:
        pgeo = json.load(open(os.path.join(OUT_DIR, "precincts.geojson")))
    except OSError:
        pgeo = None
    if pgeo:
        polys = [(k, shape(tracts[k]["geometry"])) for k in sorted(tracts)]
        outside = 0
        for f in pgeo["features"]:
            props = f["properties"]
            anchor = props.get("label")
            pt = Point(anchor) if anchor else shape(f["geometry"]).representative_point()
            hit = next((k for k, g in polys if g.contains(pt)), None)
            if hit is None:      # on a shared edge, or a rounding hair outside
                hit = min(polys, key=lambda kg: kg[1].distance(pt))[0]
                outside += 1
            precinct_tract[props["precinct"]] = hit
        print("  precinct to tract: %d matched%s"
              % (len(precinct_tract),
                 ", %d by nearest tract" % outside if outside else ""))

    os.makedirs(OUT_DIR, exist_ok=True)
    geo = {"type": "FeatureCollection",
           "features": [tracts[k] for k in sorted(tracts)]}

    def rnd(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, list):
            return [rnd(x) for x in o]
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        return o

    gpath = os.path.join(OUT_DIR, "tracts.geojson")
    with open(gpath, "w") as fh:
        json.dump(rnd(geo), fh, separators=(",", ":"))
    dpath = os.path.join(OUT_DIR, "demographics.json")
    with open(dpath, "w") as fh:
        json.dump({"measures": measures, "precinctTract": precinct_tract},
                  fh, separators=(",", ":"))

    print("\nwrote %s  (%d tracts, %d KB)"
          % (gpath, len(geo["features"]), os.path.getsize(gpath) // 1024))
    print("wrote %s  (%d measures, %d KB)"
          % (dpath, len(measures), os.path.getsize(dpath) // 1024))


if __name__ == "__main__":
    main()
