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
recording where it was. The result is D1 68 precincts, D2 55, D3 64, D4 50,
D5 64.

This is exact rather than an estimate, and the argument closes. Both at-large
contests appear in all 301 precincts, so every Denver precinct is inside DPS
and belongs to one of the five seats. The 2025 ballots pin 169 of them
outright. The other 132 had no D2, D3 or D4 contest, so each is in District 1
or District 5. Those 132 form exactly two connected pieces, of 68 and 64, and a
district has to be contiguous: two districts, two pieces, one piece each. There
is no other way to divide them, and the 68-piece is District 1 because it holds
all 67 precincts that voted in the 2023 District 1 contest while the 64-piece
holds none of them.

So there is no room for an unseen trade between Districts 1 and 5 either. In
the 2025 map those two districts do not touch anywhere, and a precinct moved
between them would have to be adjacent to the district receiving it.

The one assumption is contiguity, and it is checked rather than asserted: all
five districts in the shapefile's own 2023 map are single connected pieces
under the same rule. A current director-district export would still be worth
having as independent confirmation, and dropping one into `Maps/DPS Board/`
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

## Controlling what is public

`data/config.json` decides which panel sections, contests, return years,
demographic measures, boundary layers and precinct detail fields the public
sees. Everything true is the full map.

The switches are not cosmetic. The server filters each payload to match, so a
contest that is switched off never leaves the server and cannot be read out of
`/data/elections.json`. Hiding a row in the panel alone would have shipped the
data anyway.

`/admin` is a switchboard for building that file. It saves nothing: set the
switches, copy the config, commit it, deploy. It can be public without risk,
since it cannot change what the server serves and the server does not serve
what the committed config hides.

The config lives in the repo because Render's free tier has no persistent disk
and spins down when idle, so a file written at runtime would not survive, and
the two gunicorn workers would not agree on it. A missing or malformed config
means show everything, so a bad edit degrades to the full map rather than a
blank one. The config hash is folded into the data URLs, so switching something
off busts the browser cache instead of leaving the old payload in place for a
day.

## Analytics

Set `GA_MEASUREMENT_ID` in the Render dashboard to a GA4 id (`G-XXXXXXXXXX`) to
switch Google Analytics on. Unset, nothing loads at all, so local runs and forks
stay untracked. A value that is not a well-formed id is ignored rather than
written into the page.

Two things are deliberate. Share codes carry the whole map state, so reporting
them as page paths would scatter one page across thousands of URLs; every
shared view is reported as `/s` with a `shared_link` flag instead. And the
search event records only whether someone searched by address or by precinct,
never the text they typed, so nobody's home address ends up in a third party's
analytics. `/admin` is not tracked.

Events: `page_view`, `layer_view` (which shading was chosen),
`boundary_toggle`, `search`, `share_open`, `image_open`, `image_download`
(format and size only).

## Labels on the map

A precinct shows its number as soon as its own shape has room for it, not when
the map crosses one zoom level. Green Valley Ranch and Montbello are large
enough to label at zoom 12; the few-block precincts downtown wait until 14.
Everything switching on together meant the whole county stayed blank until the
smallest precinct fit, which is two zooms later than most of the map needed.

Room is measured against the precinct's bounding box, which overstates it for
an L-shaped precinct. That is the forgiving direction: the numbers carry a
white halo, so one that is slightly crowded still reads.

The hover chip that names a precinct is suppressed only once essentially every
precinct is carrying its own number, so it keeps working through the zooms
where only the big ones are labelled.

## Share links

`/s/<code>` carries the whole view. The code is `~`-separated: a base36 bitmask
of the boundary layers first, then optional segments for the district filter
(`f`), contest (`e`), demographic measure (`d`), return year (`b`) and its
threshold (`t`), basemap (`m`), view-only (`v1`), selected precinct (`p`), and
last the view itself (`z<zoom>@<lat>,<lng>`).

Every control in the panel that changes what you see is in there, which is the
point: a link that quietly drops one setting is worse than no link. Two bits
are inverted on purpose -- bit 128 means "precincts hidden" and bits 256 and
512 mean the precinct and district numbers are *off* -- so that links written
before those things were shareable still open the way they always did, with
all three on.

A view-only link (`v1`) removes the controls panel outright rather than
collapsing it, so there is no caret to find and nothing to reopen. It is what
the Squarespace embed uses.

## Downloading the view as a picture

The **Image** button writes the current view to a PNG or JPG at one, two or
three times the size it is on screen, and the sheet shows the exact pixel
dimensions before you commit. The picture is the map area only: no controls
panel, no modal, no zoom buttons.

It is composited by hand rather than with a DOM-to-canvas library. On screen
the map is only ever three things -- tile images, the canvases Leaflet paints
the precincts and outlines into, and a handful of text labels -- so the
exporter walks the panes in z-index order and handles those three cases. That
keeps the app free of another dependency and, more usefully, lets the labels,
the scale bar, the "Showing" key and the credit line be *redrawn* at the
output size rather than upscaled, so they stay sharp at 3x. The basemap and
the precinct shapes are upscaled from what is on screen, which is the usual
trade for this kind of export.

Tiles are the one wrinkle. Tiles already on screen were fetched without CORS,
so drawing them straight into a canvas would taint it and `toBlob` would
throw. Putting `crossOrigin` on the live tile layers would fix that and break
the basemap outright the day Esri or OSM stopped sending the header, so the
live layers are left alone and each visible tile is re-requested at export
time as a CORS twin. A twin that fails is skipped, and the sheet says how many
were missing rather than handing over a map with silently blank ground.

The credit line is drawn into the corner of every picture, because the
attribution has to travel with the image once it leaves the page.

A view-only share link has no panel, so it gets a small download button under
the zoom control instead. `data/config.json` can switch it off with
`"actions": {"download": false}`.

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
