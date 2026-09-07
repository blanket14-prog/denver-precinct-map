# Denver Precinct Map

Interactive map of Denver County's 301 election precincts. Muted CARTO Positron
basemap, precincts drawn as highlighted polygons, click a precinct for a popup
with its number, neighborhood, and district assignments.

## Data

Source: Denver Open Data, `ELEC_ELECTIONPRECINCTS_A` shapefile.
Converted from NAD83(HARN) StatePlane Colorado Central (ft) to WGS84,
simplified to ~1.5 m tolerance, and stored as `data/precincts.geojson`
(301 features, ~280 KB).

To regenerate after a new shapefile drop, run `tools/convert_shapefile.py`
with the shapefile path.

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
still fetched from CARTO.
