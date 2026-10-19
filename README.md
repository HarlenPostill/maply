<img src="assets/icon.svg" width="64" height="64" alt="">

# maply

Plan Google Street View photos of every footpath in the City of Sydney, then
capture them quickly yourself with a Chrome extension. Each photo is saved with
the metadata an image model will need later: street, OSM tags, footpath side,
planned and actual camera position and heading, distance to the nearest
intersection, and imagery date.

Maply runs locally. Capture and planning need no API keys. Optional accessibility tagging sends selected photos to Gemini through your signed-in Antigravity CLI; it does not require a separate Gemini API key.

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
  that's about 58,300 photos along 436 km of streets. Ticking *Also photograph into
  each intersection* adds corner views for kerb ramps.

Then set the spacing, which footpaths, and the views per point.

Divided roads (Anzac Parade, Oxford Street, Primrose Ave, …) are drawn in
OpenStreetMap as two one-way lines. By default each carriageway only photographs
the footpath beside it: a view toward the other carriageway would look across
the median at a footpath the other line already covers up close. Two lines count
as one divided road when they share a name, run roughly parallel and are under
30 m apart. Untick *On divided roads, photograph each footpath only from the side
of the road it's on* to photograph both sides of every line.

Each shot gets a
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

**Re-planning a job you've started.** After a planner change, make a new job
from an existing one and bring your finished photos across:

```bash
cd backend && uv run python ../scripts/replan_job.py city-of-sydney --name "City of Sydney (divided roads fixed)"
```

The new job uses the old job's streets and options, so shots the new plan still
has keep their codes, and their photos, checks and status are copied over. The
old job is only read; shots the new plan dropped stay there. Completed labels
are also carried across when they match the copied image, retaining the original
inference provenance; stale labels and unfinished tagging work are not copied. If you take more
photos in the old job afterwards, add `--into <new job folder>` to copy them
across too (shots already done in the new job are left alone).

## 3. Tag captured images with Gemini

Start Maply as usual (`docker compose up -d --build`). Run the vision worker in a
second terminal **on your Mac**, where Antigravity is installed and signed in:

```bash
agy models  # verify your login and gemini-3.8-flash-low availability
cd backend
uv sync
uv run python ../scripts/tag_worker.py
```

The default is `gemini-3.8-flash-low`, tested with the signed-in CLI on existing
job images. It uses structured JSON output with strict server validation.

The Docker app keeps a persistent queue in Postgres. The host worker downloads
one selected image at a time from your local app, calls Antigravity in read-only
plan mode, and saves validated structured results back to Maply. Credentials stay
with Antigravity on your Mac. Leave the worker terminal running while tagging.
Use `--api-url http://127.0.0.1:8001` if you changed the app port;
`--agy-bin /path/to/agy` (or `MAPLY_AGY_BIN`) overrides executable discovery.
`--once` processes at most one image; `--max-tasks 2` runs a bounded pilot.

In a job:

1. Open **Tagging progress** on the map. Search and select one or more streets,
   or click a road section. Choose all sides, left, right or intersection views.
   **Show selection on map** zooms to those streets or that section.
2. Press **Tag selected streets** or **Tag this section**. Only saved images
   (`captured`, `accepted`, `needs_check`) are eligible; retakes and missing images
   are excluded. Completed images and already active work are skipped.
3. The overview lines show unlabelled, queued, running, partial, completed and
   failed analysis. Counts describe **saved images analysed**, not capture
   coverage or verified wheelchair access. Left/right follow the segment's
   stored direction. A section is a planned road segment, not a surveyed footpath.
4. Open an image from its section or **Photos** to inspect observations, each
   tag's confidence and evidence, review reasons, and highlighted image regions.
   Coloured, numbered regions are visible by default; the matching legend lets
   you focus a section, and unchecking **Show feature regions** reveals the original image.
   Individual images can also be submitted from this view. Filter Photos by
   tagging status to find completed or failed work.
5. Open **Live tagging activity** for the current model/stage, elapsed time,
   provider tool events, reported token counts, the ordered queue and recent
   outcomes. Open or cancel individual queued images here. Activity is streamed
   from the server, with polling as a fallback; elapsed time is not an ETA.

The activity panel also shows the account allowance returned by Antigravity's
supported `/usage` command: remaining percentage per model group/window, reset
time and when it was checked. The host worker refreshes this in the background
at most every five minutes. Unavailable values are shown as unavailable; image
capacity is not estimated from token allowances. These limits are shared with
other usage of the same account.

An image's high-level confidence is the mean of its known observation scores,
including footpath presence and all feature tags. Unknowns are excluded from
that mean and counted separately; an entirely unknown image has no score.
Street/job confidence is the mean of the scored image means, so images with more
tags do not dominate. Cards also show the lowest tag score, unknown counts and
tagged-image coverage. Scores describe model certainty, not accessibility or
measured accuracy. Existing valid results receive these summaries automatically.

Only provider tool/status events, numeric token usage and a preview of the
returned annotation are exposed. Raw tool arguments and internal reasoning are
not logged to the activity panel. Events persist per task attempt; retries show
the current attempt. The full annotation is accepted only after validation.

**Retry failed / cancelled** queues unsuccessful selected images and any still
unlabelled images. **Cancel selected work** invalidates queued/running results;
an in-flight CLI call may finish, but its result will not be applied. Pending
work survives app restarts. A disconnected worker's running task becomes failed
after its five-minute lease expires; retry it explicitly. Authentication, quota,
validation and timeout errors are shown rather than counted as completed.
The worker validates annotations before submission. An invalid annotation gets
one correction attempt using the same image and the specific validation errors
(at most two model calls per task). If correction fails, the image stays failed
with the reason shown; invalid or contradictory labels are never accepted.

The schema covers footpath presence, separate surface sections, driveway
pedestrian crossings and both joins, kerb ramps and crossing context, slopes,
clearance and local hazards. Every known tag has a model-reported confidence
between 0 and 1 and a visual explanation. Unknown tags have null confidence and
an explanation. Confidence is **not calibrated accuracy**. Absent footpaths stop
detail tagging; obscured areas remain unknown. Camera coordinates are never
presented as precise feature coordinates. Unknowns, low confidence and potential
route barriers are flagged for review, even when analysis completed successfully.

Results are linked to the image SHA256. Changed images cannot receive an old
in-flight result, and stale labels are excluded from display/export. Successful
attempts are also saved under `data/jobs/<job>/labels/` with their model, imagery
date, tagging date and source hash. **Write dataset file** includes current labels
and queue state in the export.

## 4. Use the dataset

Everything for a job lives in one folder on your Mac:

```
data/jobs/<job>/
  images/<street>/<shot>_<lat>_<lng>_h<heading>.jpg   the photos
  thumbs/<street>/...                                  480 px previews
  shots.csv                                            every planned shot
  dataset/manifest.jsonl                               written by "Write dataset file"
  dataset/schema.json                                  field descriptions + label schema
  labels/<shot>_<task>_<attempt>.json                   successful annotation attempts
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

`labels` is null until an image is analysed. Completed labels use
`maply-accessibility/1`: footpath presence, feature records with image regions,
per-tag values/confidence/evidence, review reasons and provenance. `schema.json`
contains the model-output JSON schema; stored labels also include server-added
provenance. The `tagging` field records processing state independently of capture
status.

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
* Divided roads are recognised from the map alone (same name, parallel, under
  30 m apart). Where the two carriageways have different names, or one of them is
  outside the boundary, both sides of the remaining line are still photographed.
* Photos include Google's on-screen controls around the edges. Crop fixed
  margins before analysis if they get in the way.
* The extension reads the camera position and imagery date from Google's page
  (address bar and visible text). If Google changes its page, values that
  can't be read are recorded as unknown and the photo is flagged.
