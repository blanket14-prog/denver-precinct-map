"""Last general-election result for every Colorado House and Senate seat.

Writes data/results.json, which drives the map's "By margin" shading and the
"Last election" line in the detail card.

Source: OpenElections precinct-level returns, which are the Secretary of
State's certified county results in one consistent CSV per election:

    git clone --depth 1 --filter=blob:none --sparse \\
        https://github.com/openelections/openelections-data-co.git
    cd openelections-data-co && git sparse-checkout set 2022 2024

Every House seat was last on the ballot in November 2024. Senate terms are
four years and staggered, so 18 seats were last contested in 2024 and the
other 17 in 2022; where a seat appears in both, the later result wins.
Colorado fills vacancies by appointment, not special election, so there is no
later contest to look for -- which also means that in a dozen seats the person
who won the last election is no longer the person holding the seat. The map
says so rather than crediting the appointee with someone else's margin.

Margin is (winner - runner-up) / all votes cast in the race, in percentage
points, so a third-party candidate's votes widen the denominator rather than
being ignored. A race with no opponent on the ballot is marked uncontested.

Run:  python3 tools/build_results.py [path to the openelections clone]
"""
import collections, csv, json, os, sys

CLONE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/oe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "results.json")
FILES = [(2022, "2022/20221108__co__general__precinct.csv"),
         (2024, "2024/20241105__co__general__precinct.csv")]
OFFICES = {"State House": "house", "State Senate": "senate", "U.S. House": "congress"}
PARTY = {"DEM": "D", "REP": "R", "LIB": "L", "GRN": "G", "UNA": "U", "ACN": "A"}


def tally(path):
    votes = collections.defaultdict(lambda: collections.defaultdict(int))
    party = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            chamber = OFFICES.get(row["office"])
            if not chamber:
                continue
            try:
                v = int(float(row["votes"] or 0))
            except ValueError:
                continue
            seat = (chamber, str(int(row["district"])))
            name = row["candidate"].strip()
            votes[seat][name] += v
            party[(seat, name)] = row["party"].strip()
    return votes, party


def main():
    out = {"house": {}, "senate": {}, "congress": {}}
    for year, rel in FILES:
        votes, party = tally(os.path.join(CLONE, rel))
        for (chamber, district), cands in votes.items():
            ranked = sorted(cands.items(), key=lambda kv: -kv[1])
            total = sum(v for _, v in ranked)
            (wname, wv) = ranked[0]
            rname, rv = ranked[1] if len(ranked) > 1 else (None, 0)
            out[chamber][district] = {
                "year": year,
                "winner": wname,
                "party": PARTY.get(party[((chamber, district), wname)], "?"),
                "votes": wv,
                "runner": rname,
                "runner_party": (PARTY.get(party[((chamber, district), rname)], "?")
                                 if rname else None),
                "runner_votes": rv,
                "total": total,
                "margin": round(100.0 * (wv - rv) / total, 2) if total else None,
                "uncontested": rname is None,
            }

    for chamber, n in (("house", 65), ("senate", 35), ("congress", 8)):
        have = sorted(out[chamber], key=int)
        assert have == [str(i) for i in range(1, n + 1)], (chamber, have)

    doc = {
        "source": "Colorado Secretary of State certified results, via OpenElections",
        "note": ("Last general election for each seat: November 2024 for every House "
                 "and congressional seat; November 2022 or 2024 for the Senate. Margin is winner minus "
                 "runner-up as a share of all votes cast in the race."),
        "house": out["house"],
        "senate": out["senate"],
        "congress": out["congress"],
    }
    with open(OUT, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"), ensure_ascii=False)

    years = collections.Counter(v["year"] for v in out["senate"].values())
    cyears = collections.Counter(v["year"] for v in out["congress"].values())
    unc = [c + " " + d for c in out for d, v in out[c].items() if v["uncontested"]]
    close = sorted(((v["margin"], c, d, v["votes"] - v["runner_votes"])
                    for c in out for d, v in out[c].items()))[:3]
    print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
    print("  senate seats last up: %s" % dict(sorted(years.items())))
    print("  congress seats last up: %s" % dict(sorted(cyears.items())))
    print("  uncontested: %d (%s)" % (len(unc), ", ".join(unc)))
    print("  closest: " + "; ".join("%s %s by %d votes" % (c, d, v) for _, c, d, v in close))


if __name__ == "__main__":
    main()
