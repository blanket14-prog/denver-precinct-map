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

A slider under the legend's colour ramp raises a floor on the return rate,
hiding every precinct below it so the strongest turnout stands out on its own.
The greyed part of the ramp shows how much of the range is cut, and the
threshold travels in share links.

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

**Demographics** — American Community Survey 5-year estimates by census tract,
from data.census.gov, plus the TIGER/Line tract shapefile for Colorado
(`tl_<year>_08_tract.shp`). Put both in one directory and run:

    python3 tools/build_demographics.py /path/to/Demographics

Output is `data/tracts.geojson` (178 Denver tracts) and
`data/demographics.json`.

These are drawn on their own geography rather than apportioned onto precincts,
and that is deliberate. The Census publishes nothing by voting precinct. Blocks
nest inside Denver's precincts and could be summed exactly, but ACS estimates
are not published at block level, so a precinct figure would be an
apportionment presented as a measurement. Every value keeps the margin of error
the Bureau published with it, the map shades between the 5th and 95th
percentile, and any tract whose margin exceeds 30% of its estimate is drawn
with a dashed edge. In the 2020-2024 median household income table that is 42
of 174 tracts, one of them $121,379 give or take $82,893.

The margin of a share is not the numerator's margin divided by the
denominator. The numerator is part of the denominator, so the two move
together, and the ACS handbook subtracts that shared variance. Skipping the
correction overstates every share: renter-occupied came out at a median margin
of 10.8 percentage points before it, against 8.4 after.

Reliability is judged two ways for the same reason. A median in dollars is
measured against its own size, flagged over 30%. A share is measured in
percentage points, flagged over 10, because a 3-point margin on a 4% poverty
rate is a good estimate even though it is 75% of it.

Summed across the 178 tracts the tables give Denver 718,877 people, 335,428
households, 21.6% of households with children, 51.2% renter-occupied, 23.9%
speaking a language other than English at home, and 11.2% below poverty.
Households and occupied housing units come from different tables and both total
335,428, which is a useful check that the join is right.

Two quirks of the ACS export are worth knowing. The Bureau top-codes a median
it will not publish exactly: Washington Park and Hilltop both come through as
"250,000+", which is the two richest tracts in Denver rather than missing data,
so the builder reads them at the cap and the map says "or more". And a genuine
blank, written "-", means too few households to survey; those tracts are left
unfilled rather than coloured. In Denver that is the airport and one other
unpopulated tract.

The precinct detail bar names the tract a precinct's centre falls in. It is
labelled as the tract, not as the precinct, because the two geographies cross.

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

## Known artifacts

Precinct polygons are simplified to ~1.5 m each, independently, which pulls
shared edges very slightly apart and leaves about 31,000 m2 of overlap slivers
across the whole county, roughly 0.008% of its area and 1 to 1.6 m wide. The
source shapefile has exactly zero overlap; this is display simplification only,
and every district overlay is dissolved from the exact geometry rather than
from these. Shapely's `coverage_simplify` would remove the slivers entirely,
but at the same file size it costs 40 m of positional error, and at the same
accuracy it doubles the file. The slivers are the better trade.

## Notes

Leaflet 1.9.4 is vendored in `static/leaflet/` rather than loaded from a CDN,
so the app has no third-party script dependency at runtime. Basemap tiles are
still fetched from Esri (World Light Gray Canvas) and OpenStreetMap.
