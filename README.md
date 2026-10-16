<img src="assets/icon.svg" width="64" height="64" alt="">

# maply

Plan Google Street View photos of every footpath in the City of Sydney, then
capture them quickly yourself with a Chrome extension. Each photo is saved with
the metadata an image model will need later: street, OSM tags, footpath side,
planned and actual camera position and heading, distance to the nearest
intersection, and imagery date.

maply runs locally. It needs no API keys and uploads nothing.

## Start

```bash
docker compose up -d --build
open http://127.0.0.1:8000
```

Once, download the City of Sydney street network from OpenStreetMap (a minute
or two). "Block out an area" and "Whole city" then work offline:

```bash
cd backend && uv sync && uv run python ../scripts/fetch_streets.py
```

## 1. Plan a job

**New job**, then choose how to pick streets:

* **Block out an area:** outline a few blocks on the map.
* **Draw streets:** click along individual streets.
* **Whole city:** every street inside the City of Sydney boundary. At 15 m spacing
  that's about 62,700 photos along 436 km of streets. Ticking *Also photograph into
  each intersection* adds corner views for kerb ramps (about 102,000 photos).

Then set the spacing, which footpaths, and the views per point. Each shot gets a
Street View link in Google's documented format
(`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=LAT,LNG&heading=H&pitch=P&fov=F`)
that opens at the planned spot, already facing the planned direction.

## 2. Capture with the Chrome extension

Install it once:

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**
   and choose the `extension/` folder in this repository.
2. Pin **maply capture**. Click it, choose your job and press **Start capturing**.

Then work through the shots in that tab:

| Key | What it does |
|---|---|
| **Enter** | Save the view exactly as you've lined it up, and open the next shot |
| **N** | No imagery here |
| **B** | Footpath blocked from view |
| **X** | Skip for now (skipped shots come back if you tick *Include shots I skipped*) |
| **[** | Back to the previous shot |
| **H** | Hide or show the panel |

The panel at the bottom of the page shows the shot, which footpath to aim at,
and live feedback as you drag: *On target* or *Turn left 12°*, plus the
camera's distance from the plan. It hides itself while saving, so it's never in
the photo.

Progress is stored in maply, so you can stop at any time and press Start later
to carry on from the next unfinished shot. The same actions also work from
anywhere in Chrome with Alt+Shift+S / N / K / B (change these at
`chrome://extensions/shortcuts`).

**Why the extension asks for access to all sites:** Chrome only lets an extension
capture a tab from a key press if it has that permission. The extension only
runs on `google.com/maps` pages, and it only sends captures to your local maply
at `127.0.0.1`.

Prefer to hand batches to Codex? Each job also has **Hand photos to Codex
instead**: copyable prompts plus a *Check photos* step.

## 3. Use the dataset

Everything for a job lives in one folder on your Mac:

```
data/jobs/<job>/
  images/<street>/<shot>_<lat>_<lng>_h<heading>.jpg   the photos
  thumbs/<street>/...                                  480 px previews
  shots.csv                                            every planned shot
  dataset/manifest.jsonl                               written by "Write dataset file"
  dataset/schema.json                                  field descriptions + suggested labels
```

`manifest.jsonl` has one line per photo:

```json
{"id": "city-of-sydney/P04211-L1", "image": "images/george-street/P04211-L1_….jpg",
 "street": {"name": "George Street", "highway": "primary", "osm_way_ids": [...], "osm_tags": {"sidewalk": "both", "surface": "asphalt"}},
 "footpath": {"side": "west", "relative_side": "left"},
 "view": {"kind": "street", "type": "perpendicular", "description": "straight across"},
 "planned": {"lat": …, "lng": …, "heading": 277.5, "pitch": -8, "fov": 80, "url": "…"},
 "camera": {"lat": …, "lng": …, "heading": 281.0, "pano_id": "…", "offset_from_plan_m": 1.1},
 "near_intersection_m": 42.0, "imagery_date": {"value": "2024-03"},
 "quality": {"status": "captured", "flags": []}, "labels": null}
```

`labels` is reserved for phase 2, when an image model adds accessibility tags.
`schema.json` suggests fields: footpath present, condition, kerb type, kerb
ramp, tactile indicators, obstructions, surface. The database has a matching
`labels` column ready.

**Disk space:** photos are JPEGs of your browser window. At the extension's
default quality, expect roughly 0.3–1 MB each, so 20–60 GB for the whole city.
The job page shows space used so far and an estimate for the finished job.
Lower *JPEG quality* in the extension's settings if you need smaller files.

## Conventions

* Distances are in metres (computed in GDA2020 / MGA zone 56), and headings
  are true-north compass bearings.
* "Left/right of arrow" is relative to each street's drawn direction. The
  dataset also gives the compass side (north, east, …).
* Camera positions are where Street View's camera stood, never the location of
  a kerb or ramp in the photo.
* Imagery dates come only from the "Image capture" text Street View shows. If
  it isn't shown, the date is recorded as unknown.
* Shots are ordered street by street through neighbourhoods, so a session moves
  smoothly from block to block.

## Development

```bash
docker compose up -d db
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd ../frontend && npm install && npm run dev     # http://127.0.0.1:5173
cd ../backend && uv run pytest                   # quick checks, about a second
```

After changing the extension, click its reload icon on `chrome://extensions`.

## Limitations

* The City of Sydney boundary in `config/` is a hand-drawn approximation.
* Divided roads appear in OpenStreetMap as two one-way lines, so both sides of
  each carriageway get photos. Untick or redraw them in small jobs if that's unwanted.
* Photos include Google's on-screen controls around the edges. Crop fixed
  margins before analysis if they get in the way.
* The extension reads the camera position and imagery date from Google's page
  (address bar and visible text). If Google changes its page, values that
  can't be read are recorded as unknown and the photo is flagged.
