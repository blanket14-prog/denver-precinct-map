# Denver Precinct Map

Interactive map of Denver County's 301 election precincts. Muted Esri gray canvas
basemap, precincts drawn as highlighted polygons, click a precinct for a bottom
bar with its number, neighborhood, registered voters, and district assignments.
Overlay layers for City Council, DPS, RTD, State Senate, State House and
neighborhoods, plus a school board election choropleth per contest.

## Data

**Boundaries** — Denver Open Data, `ELEC_ELECTIONPRECINCTS_A` shapefile.
Reprojected from NAD83(HARN) StatePlane Colorado Central (ftUS) to WGS84 and
simplified to ~1.5 m.

**School board district** — joined from Denver Open Data's DPS Board shapefile
by largest-area overlap, because the precinct shapefile's own `DPS_DIST` column
is empty on all 301 records. The two at-large directors (`board_dist` 0) are
citywide and excluded from the join.

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

Regenerate with:

    python3 tools/convert_shapefile.py /path/to/Maps data/precincts.geojson
    pip install numbers-parser
    python3 tools/build_elections.py "/path/to/Election Results"

Output is `data/precincts.geojson` (301 features, ~305 KB), `data/districts.geojson`
(~286 KB) and `data/elections.json` (~12 KB). `numbers-parser` is a build-time
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
