#!/usr/bin/env python3
"""Build data/elections.json from Denver's precinct-level school board results.

Denver publishes these as a Tableau viz (the ".numbers" exports come from
https://public.tableau.com/app/profile/ssharp8813). It never releases an exact
vote count per precinct, only which candidate led and which 25% band their
share fell in, so that band is all this file can carry.

Usage:
    python3 tools/build_elections.py [RESULTS_DIR] [OUT]

RESULTS_DIR defaults to "../Election Results" and holds one .numbers export per
contest, named <contest>-<year>.numbers (D1-2023.numbers, At-Large-2025.numbers).

Requires numbers-parser, which is a build-time dependency only -- the Flask app
just serves the JSON, so it stays out of requirements.txt.
"""

import glob
import json
import os
import re
import sys

from numbers_parser import Document

SRC = sys.argv[1] if len(sys.argv) > 1 else "../Election Results"
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/elections.json"

# Denver's own classification field, lowest band first. The map's colour ramp
# is indexed by this position, so the order is load-bearing.
BANDS = ["MC25", "MC50", "MC75", "MC100"]
BAND_LABEL = ["< 25%", "25-50%", "50-75%", "> 75%"]

# Order contests the way a reader expects them, not alphabetically.
def sort_key(key):
    m = re.match(r"^d(\d+)$", key)
    return (0, int(m.group(1)), "") if m else (1, 0, key)


def clean_name(raw):
    """Un-mangle names the Tableau export quotes badly.

    It writes a nickname as 'Donald DJ" Torres"' -- the opening quote is lost
    and the closing one drifts to the end of the string.
    """
    s = " ".join(str(raw).split())
    m = re.match(r'^(.*?)\s+(\S+)"\s+(.*)"$', s)
    if m:
        return '%s "%s" %s' % (m.group(1), m.group(2), m.group(3))
    return s


def read_contest(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(.*)-(\d{4})$", stem)
    if not m:
        raise SystemExit("cannot read a contest and year from %s" % stem)
    name, year = m.group(1), int(m.group(2))

    table = Document(path).sheets[0].tables[0]
    rows = table.rows(values_only=True)
    col = {n: i for i, n in enumerate(rows[0]) if n}
    for need in ("Precinct1", "Precinct N", "Precinct Leader",
                 "Map Classification", "Contest1"):
        if need not in col:
            raise SystemExit("%s has no '%s' column" % (stem, need))

    # The export appends per-candidate total rows after the precinct rows.
    # They carry a "Precinct N" (the candidate's citywide vote count, of all
    # things) but no precinct code, so key the filter on the code.
    body = [r for r in rows[1:] if r[col["Precinct1"]] is not None]

    # Blue goes to whoever led the most precincts, so the map's dominant
    # colour is the contest's dominant candidate.
    led = {}
    for r in body:
        who = clean_name(r[col["Precinct Leader"]])
        if who != "TIED":
            led[who] = led.get(who, 0) + 1
    cands = sorted(led, key=lambda n: (-led[n], n))
    idx = {n: i for i, n in enumerate(cands)}

    precincts = {}
    tied = 0
    for r in body:
        who = clean_name(r[col["Precinct Leader"]])
        num = str(int(r[col["Precinct N"]]))
        if who == "TIED":
            precincts[num] = [-1, -1]
            tied += 1
            continue
        band = str(r[col["Map Classification"]]).strip()
        if band not in BANDS:
            raise SystemExit("%s: unknown band %r" % (stem, band))
        precincts[num] = [idx[who], BANDS.index(band)]

    key = "al" if name.lower().startswith("at-large") else name.lower()
    label = ("At-Large" if key == "al" else "District " + key[1:]) + " (%d)" % year
    return {
        "key": "%s_%d" % (key, year),
        "sort": key,
        "label": label,
        "title": str(body[0][col["Contest1"]]).strip(),
        "year": year,
        "cands": cands,
        "led": [led[n] for n in cands],
        "tied": tied,
        "precincts": precincts,
    }


def main():
    paths = sorted(glob.glob(os.path.join(SRC, "*.numbers")))
    if not paths:
        raise SystemExit("no .numbers exports under %s" % SRC)

    contests = [read_contest(p) for p in paths]
    contests.sort(key=lambda c: (sort_key(c["sort"]), c["year"]))
    for c in contests:
        c.pop("sort")

    # Cross-check against the precinct layer so a numbering change in a future
    # export cannot quietly shade nothing.
    try:
        geo = json.load(open("data/precincts.geojson"))
        known = set(str(f["properties"]["precinct"]) for f in geo["features"])
    except OSError:
        known = None

    doc = {"bands": BAND_LABEL, "contests": contests}
    with open(OUT, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))

    print("wrote %s  (%d contests, %.0f KB)"
          % (OUT, len(contests), os.path.getsize(OUT) / 1024.0))
    for c in contests:
        extra = ""
        if known is not None:
            miss = [p for p in c["precincts"] if p not in known]
            extra = "  UNMATCHED %d" % len(miss) if miss else "  all matched"
        print("  %-18s %3d precincts  %s%s"
              % (c["label"], len(c["precincts"]),
                 ", ".join("%s %d" % (n, l) for n, l in zip(c["cands"], c["led"]))
                 + (", tied %d" % c["tied"] if c["tied"] else ""),
                 extra))


if __name__ == "__main__":
    main()
