<img src="assets/icon.svg" width="64" height="64" alt="">

# maply

Plan Google Street View photos of every footpath in the City of Sydney, then
capture them quickly yourself with a Chrome extension. Each photo is saved with
the metadata an image model will need later: street, OSM tags, footpath side,
planned and actual camera position and heading, distance to the nearest
intersection, and imagery date.

Maply runs locally. Planning and manual Chrome capture need no API keys. Automatic capture and image tagging use your saved OpenRouter key and any compatible image model.

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

## Automatic capture with a native desktop worker

Each job now has **Automatic capture in Chrome**. This is a model-independent
visual loop: screenshots go to an OpenRouter image model, and a host worker
executes validated mouse drags using native macOS input. It uses no Playwright,
WebDriver, CDP, headless browser, page scripting or Street View image-download API.
The worker saves a screenshot of the visible Chrome window through Maply's
existing image storage and camera checks. The browser window, including its
visible chrome, is captured at up to 1920×1440 pixels.

### First run

1. Rebuild the local app with `docker compose up -d --build app`. Startup applies
   migrations through `0009_openrouter_only`; existing jobs and photos are retained.
2. Save your OpenRouter key in **Model settings** if it is not already configured.
   Capture reuses that key and selects the model for combined capture/tagging; standalone tagging has its own model setting.
3. Open Sydney, expand **Automatic capture in Chrome**, and create a run. The
   defaults are a 200-photo pilot, 1,000 model requests, US$5 reported cost,
   eight hours, six assessments per photo, and 0.9 minimum model confidence.
4. On the capture Mac, use a dedicated Chrome profile/window and pause the Maply
   capture extension in that profile. Keep Chrome on the primary display, at
   least 800×600, with an awake, unlocked desktop. Grant the terminal/Python
   **Accessibility**, **Automation** (Chrome and System Events), and **Screen
   Recording** permissions in macOS System Settings. Restart the terminal after
   changing permissions if macOS requires it.
5. Copy the run's command from the job page, or run:

   ```bash
   cd backend
   uv run --extra capture python ../scripts/capture_worker.py --run RUN_UUID
   ```

   Add `--check` to inspect foreground Chrome, clipboard URL reading and screen
   capture without model calls or navigating to a shot. For overnight operation,
   prevent idle sleep with macOS `caffeinate`:

   ```bash
   caffeinate -di uv run --extra capture python ../scripts/capture_worker.py --run RUN_UUID
   ```

The worker opens a new tab after a five-second countdown. Do not use its desktop
for other work during capture. Moving the pointer to a screen corner triggers
PyAutoGUI's fail-safe. Ctrl+C stops the worker and attempts to pause its run.
The adapter stops if Chrome loses foreground focus, the window geometry changes,
or the active URL leaves Google Maps. It never executes arbitrary model code,
keyboard commands, page URLs or clicks. Its only model-controlled physical action
is a small drag inside the image; it does not move along the street or zoom.

### Models, limits and review

The default combined model is `google/gemini-3.1-flash-lite`. Select another image model supporting
JSON Schema structured outputs, or enter its full OpenRouter ID. Optional fallback
IDs are sent to OpenRouter in order for provider failures. The actual returned
model, token usage, reported USD cost, decision and visual evidence are recorded.
Prices and discounts are not selected automatically: pause a run, choose **Edit
model and limits**, save, then resume. Combined capture uses its selected model for both decisions and labels; the standalone tagging model is unchanged. The API uses asynchronous HTTP, allowing independent desktop workers to
assess shots concurrently.

The photo limit counts saved photos, blocked/no-imagery outcomes and review
outcomes. Request slots are reserved before network calls. Reported-cost limits
are checked after each response: one request, or requests already in flight, can
exceed that threshold. This is not a prepaid hard currency cap; use OpenRouter's
own credit/key limits for that. Missing cost reporting, malformed output, provider
errors and access/login challenges pause the run instead of silently retrying.
Changing limits requires pausing first; the duration restarts when edited.

A save needs sufficient model confidence and a parsed Street View camera URL
within 40 m and 15° of the plan, on the correct side where known. Ambiguous views,
loading that never resolves and exhausted adjustment attempts go to the review
backlog. Passed views have a separate screenshot under `capture-review/`, outside
the accepted dataset. Review them in Photos or use the extension backlog mode. Confidence is model-reported; inspect a sample of accepted captures
before a large unattended run. An image being saved does not certify its framing.

Pause/stop revokes reservations. In-flight inference can still finish and incur
charges, but its stale result cannot trigger an action or save. A crashed worker's
reservation expires after five minutes; old tokens cannot save after reassignment.
Three interrupted attempts send the photo to review. Progress, leases, costs and
model decisions live in PostgreSQL and survive restarts. Run status describes the
queue; a running run requires a connected terminal worker to make progress.

### Multiple workers

Run the same command on separate Macs, each with its own unlocked desktop. One
worker per login session is enforced by an OS lock. Atomic database reservations
prevent duplicate work across runs or workers, and the manual extension skips
photos currently reserved by a worker. Do not run multiple workers on one shared
mouse/keyboard desktop. From another Mac, securely tunnel the local API first:

```bash
ssh -N -L 18000:127.0.0.1:8000 YOUR_MAPLY_MAC
# In another terminal on that Mac's checkout:
cd backend
uv run --extra capture python ../scripts/capture_worker.py --server http://127.0.0.1:18000 --run RUN_UUID
```

The API key stays on the Maply server. Only screenshots, decisions and job data
cross the tunnel. Workers currently support macOS Chrome; Windows/Linux need a
separate native adapter. A VM must provide a working graphical desktop and be
validated for Street View rendering before use.

For diagnostics, `/api/capture/runs/RUN_UUID/events` returns the latest 100 model
assessment records. The worker makes no paid calls until it has a running run
and a claimed shot. Automated tests simulate desktop/model IO and use the test
database; they do not drive Street View or spend API credits.

## 3. Tag captured images with OpenRouter

Open **Model settings** and save an OpenRouter key plus a standalone tagging model.
The default is `google/gemini-3.1-flash-lite`. The app processes queued tagging work
itself: no separate tagging terminal, local model login or CLI installation is needed.
The key stays in `data/provider-settings.json` with owner-only permissions and is
excluded from Git. Existing keys and model preferences are preserved during upgrade.

Automatic capture asks for the capture decision and full accessibility tags in
one response for the final view. A `wait`, `drag` or `pass` response has no tags.
A successful save validates the labels against the existing tagging vocabulary,
binds them to the exact saved image checksum, writes label JSON, and marks tagging
complete. It does not enqueue another tagging call. If annotation validation fails,
the good photo is retained and queued for standalone OpenRouter tagging; these
separate retries use the standalone model and are billed outside the capture run's
request/cost limits. Unknown observations remain unknown, rather than being forced
through a correction prompt.

Use **Parallel sessions** to select 1–20 standalone OpenRouter requests at once.
The setting applies across jobs, is enforced atomically in Postgres, and lets
active requests finish when reduced. Requests use your OpenRouter credits.
Model changes apply when the next photo is claimed; running requests keep their
original model. To prevent the app-managed tagger from starting, set
`TAGGING_AUTOSTART=false` (also useful for isolated tests).

### Manual backlog

The model can `pass` whenever it cannot make a confident call. Ambiguous camera
views are placed in **Manual backlog** without claiming a successful capture.
Saved images with uncertain tags or review reasons remain saved with their
validated observations and appear in the same backlog. The backlog counter and
Photos filter use a shared server-side condition, so the photo gallery, capture
worker and extension agree on what needs human attention.

Click **Review backlog** in a job to inspect the photo, last-view evidence and
reason. The existing **Looks right**, **Needs a retake** and **Undo** controls
resolve its backlog entry; a retake returns it to pending capture. In the Chrome
extension, choose **Only manual review backlog** to step through those views by
hand. Saving a view, or explicitly marking no imagery/blocked, resolves that entry.
Skipping leaves it in the backlog. Automatic capture does not reclaim backlog
photos unless you explicitly return them to capture. Reload the extension and
its Maps tab after this upgrade.

### Caching

Capture instructions and tagging vocabulary share a stable prompt prefix; the
per-shot camera context follows it. One OpenRouter session ID groups a capture
run without carrying previous images in a growing conversation. Matching prefix
caching is provider/model dependent and is not guaranteed by the session ID.
Capture assessment events record cache read/write token counts when reported.
The combined request avoids a duplicate image analysis call even without a cache
hit. Switching models does not preserve cached tokens across models.

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

Activity shows queued/running/completed/failed work, validation evidence and token
usage. Cancelled or replaced tasks cannot apply stale results. Pending work and
manual review survive app restarts. Retakes invalidate annotations for old image
checksums. Confidence is model-reported, not calibrated or an accessibility guarantee.

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
