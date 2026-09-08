#!/usr/bin/env python3
"""Build data/returns.json from Denver's ballot-returns-by-precinct exports.

Denver Elections publishes returns per precinct on its election dashboard
(https://public.tableau.com/app/profile/ssharp8813). The "Map - Ballot Returns
by Precinct" export gives ballots cast, ballots issued and the return rate for
every precinct, which is a real count rather than the 25% bands the results
maps are limited to.

Usage:
    python3 tools/build_returns.py [RETURNS_DIR] [OUT]

RETURNS_DIR holds one export per election, named returns_<year>.tsv or .csv.
The exports arrive UTF-16 and tab-separated whatever the extension says, so
both are read the same way. Columns: a precinct column whose header varies by
year, "Ballots Cast", "Ballots Issued", "Return Percent".

Only elections run on the current precinct map can be used. Denver redrew
precincts for 2022: the 2019 and 2021 exports carry 356 precincts against
today's 301, and although 283 of those numbers still exist, the same number is
not the same ground. Those years need a crosswalk, so the builder refuses any
export that does not join cleanly.
"""

import glob
import json
import os
import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "../Returns"
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/returns.json"


def read_rows(path):
    """Rows from an export, whatever encoding Tableau used that year."""
    raw = open(path, "rb").read()
    for enc in ("utf-16", "utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "\t" in text or "," in text:
            break
    else:
        raise SystemExit("cannot decode %s" % path)

    lines = [l for l in text.replace("﻿", "").splitlines() if l.strip()]
    sep = "\t" if lines[0].count("\t") >= lines[0].count(",") else ","
    head = [h.strip() for h in lines[0].split(sep)]
    return head, [[c.strip() for c in l.split(sep)] for l in lines[1:]]


def num(s):
    s = re.sub(r"[,%\s]", "", s or "")
    return float(s) if s else None


def read_year(path, known):
    head, rows = read_rows(path)

    def col(*names):
        for i, h in enumerate(head):
            for n in names:
                if h.lower().startswith(n):
                    return i
        raise SystemExit("%s has no column starting %r; headers are %s"
                         % (os.path.basename(path), names[0], head))

    i_p = col("precinct")
    i_c = col("ballots cast")
    i_i = col("ballots issued")

    out, missing = {}, []
    for row in rows:
        if len(row) <= max(i_p, i_c, i_i):
            continue
        p = re.sub(r"\.0$", "", row[i_p])
        cast, issued = num(row[i_c]), num(row[i_i])
        if cast is None or not issued:
            continue
        if p not in known:
            missing.append(p)
            continue
        out[p] = [int(cast), int(issued)]

    if missing:
        raise SystemExit(
            "FATAL: %s has %d precincts that are not on the current map (%s"
            "...). Denver redrew precincts for 2022, so an older export needs "
            "a crosswalk before it can be mapped."
            % (os.path.basename(path), len(missing),
               ", ".join(sorted(missing, key=str)[:6])))
    return out


def main():
    paths = sorted(glob.glob(os.path.join(SRC, "returns_*.tsv")) +
                   glob.glob(os.path.join(SRC, "returns_*.csv")))
    if not paths:
        raise SystemExit("no returns_<year> exports under %s" % SRC)

    geo = json.load(open("data/precincts.geojson"))
    known = set(str(f["properties"]["precinct"]) for f in geo["features"])

    elections = []
    for path in paths:
        m = re.search(r"returns_(\d{4})", os.path.basename(path))
        if not m:
            continue
        year = int(m.group(1))
        rows = read_year(path, known)
        pct = sorted(100.0 * c / i for c, i in rows.values())
        elections.append({
            "key": "r%d" % year,
            "year": year,
            "label": "%d Coordinated" % year,
            "cast": sum(c for c, _i in rows.values()),
            "issued": sum(i for _c, i in rows.values()),
            "lo": round(pct[0], 2),
            "hi": round(pct[-1], 2),
            "precincts": rows,
        })

    elections.sort(key=lambda e: -e["year"])
    with open(OUT, "w") as fh:
        json.dump({"elections": elections}, fh, separators=(",", ":"))

    print("wrote %s  (%d elections, %.0f KB)"
          % (OUT, len(elections), os.path.getsize(OUT) / 1024.0))
    for e in elections:
        print("  %-18s %3d precincts, %s of %s ballots returned (%.1f%%), "
              "precinct range %.2f%% to %.2f%%"
              % (e["label"], len(e["precincts"]), "{:,}".format(e["cast"]),
                 "{:,}".format(e["issued"]),
                 100.0 * e["cast"] / e["issued"], e["lo"], e["hi"]))


if __name__ == "__main__":
    main()
