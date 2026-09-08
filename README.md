# Denver Precinct Map

Interactive map of Denver County's 301 election precincts. Muted Esri gray canvas
basemap, precincts drawn as highlighted polygons, click a precinct for a bottom
bar with its number, neighborhood, registered voters, and district assignments.
Overlay layers for City Council, DPS, RTD, State Senate, State House and
neighborhoods, plus two choropleths: school board results per contest, and
ballot return rates per election.

## Data

**Boundaries** — Denver Open Data, `ELEC_ELECTIONPRECINCTS_A` shapefile.
Reprojected from NAD83(HARN) StatePlane Colorado Central (ftUS) to WGS84 and
simplified to ~1.5 m.

**School board district** — joined from Denver Open Data's DPS Board shapefile
by largest-area overlap, because the precinct shapefile's own `DPS_DIST` column
is empty on all 301 records. The two at-large directors (`board_dist` 0) are
citywide and excluded from the join.

That shapefile is a cycle out of date: the DPS board adopted a new map in April
2024, and the export matches the 2023 District 1 and 5 ballots exactly, 134
precincts of 134. The converter corrects it from the election results, which
pin the new map down without a new shapefile:

- a precinct on the 2025 District 2, 3 or 4 ballot is in that district;
- a precinct on the 2023 District 1 or 5 ballot that no 2025 contest claimed is
  provisionally still in that district;
- the seven left over are precincts that left District 3 or 4, and contiguity
  places each one;
- anything left with no neighbour in its own district was placed wrong, and
  since it never appeared on a 2, 3 or 4 ballot, the other of 1 and 5 is the
  only answer. Precinct 212 in Speer resolves this way: all four of its old
  District 5 neighbours moved to District 2.

Contiguity here means sharing any boundary at all, including a single corner,
which is what the real maps use (precinct 801 in Cole reaches the rest of its
2023 district only through a corner, as does the Indian Creek trio 915-917).
Every district in the result is a single connected piece, and registered voters
per district spread 26% rather than the 47% of the 2023 map, which is the
direction a redistricting should move.

Twenty-one precincts moved, each carrying a `school_board_2023` property
recording where it was. What the method cannot see is a trade between Districts
1 and 5 that leaves both sides connected, since neither seat was on the 2025
ballot. Dropping a current director-district export into `Maps/DPS Board/`
makes the whole correction a no-op.

**Registered voters** — Colorado Secretary of State monthly voter registration
statistics workbook, "Voter Counts by Precinct" sheet, matched on the 10-digit
precinct code (301 of 301 matched). Download the current month from
https://www.sos.state.co.us/pubs/elections/VoterRegNumbers/VoterRegNumbers.html
and drop it anywhere under the maps directory; the converter finds any
`*Statistics*.xlsx`. A `data/registered_voters.csv` with a precinct column and
a count column works as a fallback.

**School board election results** — Denver's precinct-level results for the
odd-year school board races, exported from the city's Tableau viz at
https://public.tableau.com/app/profile/ssharp8813. Denver never publishes an
exact vote count per precinct: it reports which candidate led and which 25%
band their share fell in, so the map can only shade four steps deep. Put one
`.numbers` export per contest in an `Election Results` directory, named
`<contest>-<year>.numbers` (`D1-2023.numbers`, `At-Large-2025.numbers`).

**Ballot returns** — Denver Elections' "Map - Ballot Returns by Precinct"
export from the same dashboard, giving ballots cast, ballots issued and the
return rate per precinct. Unlike the results maps this is a real count, not a
band. Name each export `returns_<year>.tsv` (they arrive UTF-16 and
tab-separated whatever the extension says) and put them in one directory.

Only elections run on the current precinct map can be used. Denver redrew
precincts for 2022, so the 2019 and 2021 exports carry 356 precincts against
today's 301; 283 of those numbers still exist but do not cover the same ground,
and the builder refuses an export that does not join cleanly rather than
mapping the wrong precincts.

Regenerate with:

    python3 tools/convert_shapefile.py /path/to/Maps data/precincts.geojson
    pip install numbers-parser
    python3 tools/build_elections.py "/path/to/Election Results"
    python3 tools/build_returns.py "/path/to/Returns"

Output is `data/precincts.geojson` (301 features, ~306 KB), `data/districts.geojson`
(~287 KB), `data/elections.json` (~12 KB) and `data/returns.json` (~10 KB). `numbers-parser` is a build-time
dependency only and stays out of `requirements.txt`.

## Run locally

    pip install -r requirements.txt
    python app.py
    # http://localhost:5000

## Deploy on Render

New Web Service pointed at this repo:

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`
- Health check path: `/healthz`

`render.yaml` is included as a Blueprint if you prefer that route.

## Notes

Leaflet 1.9.4 is vendored in `static/leaflet/` rather than loaded from a CDN,
so the app has no third-party script dependency at runtime. Basemap tiles are
still fetched from Esri (World Light Gray Canvas) and OpenStreetMap.
