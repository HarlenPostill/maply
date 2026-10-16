<img src="assets/icon.svg" width="64" height="64" alt="">

# maply

Plan Google Street View photos of footpaths and kerbs in the City of Sydney,
then hand the work to **Codex** in the ChatGPT app. Codex uses computer use in
*your own browser* to take each photo, and maply checks the results.

maply never browses, calls a model, or needs an API key. You stay in control
of every hand-off.

## How it works

1. **Block out an area** on the map (or draw individual streets). maply finds
   the streets inside it from a local copy of OpenStreetMap.
2. **Plan the photos.** Choose the spacing, which footpaths, and how many views
   per point. Every photo appears on the map as a small cone showing where the
   camera will look.
3. **Copy a prompt into Codex.** Each prompt covers up to 20 photos (you can
   change this). For every photo it gives:
   * a Street View link in Google's documented format
     (`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=LAT,LNG&heading=H&pitch=P&fov=F`)
     that opens at the planned spot, already facing the planned direction;
   * a plain description, e.g. *"west footpath of George Street, straight across; look W (277°)"*;
   * the exact file to save, e.g. `images/P007-L1_-33.874278_151.205800_h277.png`.

   Codex opens each link, lines up the view, saves a screenshot under that name,
   and logs the address-bar URL it ended on to `results/prompt-01.jsonl`.
4. **Check photos.** maply matches each saved file to its planned shot and
   reads the logged URL. Google rewrites that URL to the camera's real position
   and heading, so maply can flag photos taken too far away, facing the wrong
   way, or showing the other footpath. You can mark a photo *Looks right* or
   *Needs a retake*, and copy a prompt that covers only the missing photos.

## Start

```bash
docker compose up -d --build
open http://127.0.0.1:8000
```

The first time, download the City of Sydney street network so that "Block out
an area" works offline (a minute or two; uses OpenStreetMap's Overpass service):

```bash
cd backend && uv sync && uv run python ../scripts/fetch_streets.py
```

Without that file, maply asks the public Overpass servers each time, and they
are often busy. "Draw streets" always works.

### Codex setup

* Give Codex computer use, with access to **Google Chrome** (or set `BROWSER_APP`
  in `.env`).
* The prompt suggests `screencapture` to save just the browser window. macOS
  asks once for **Screen Recording** permission for the app that runs it.
* Paste one prompt per Codex task. When it finishes, press **Check photos**.

## Job folders

Everything for a job lives in one folder on your Mac:

```
data/jobs/<job-name>/
  job.json        the plan: every shot, its link, heading and file name
  shots.csv       the same, for spreadsheets
  prompts/        copies of each prompt you generated
  images/         screenshots Codex saved (one per shot)
  results/        Codex's logs, one JSON line per shot
  thumbs/         small previews maply makes when checking
  manifest.json   written on every check: plan + results, with relative paths
```

The folder is a complete, portable record for downstream analysis tools.
Nothing is uploaded anywhere by maply. Codex's computer use does send
screenshots of your screen to OpenAI as part of how it works; that happens in
the ChatGPT app, under your account.

## Conventions

* Distances are in metres (computed in GDA2020 / MGA zone 56), and headings
  are true-north compass bearings.
* "Left/right of arrow" is relative to the direction each street is drawn.
  Prompts describe footpaths by compass side (north, east, …) so Codex never
  needs to know the drawing direction.
* Camera positions are where Street View's camera stood, never the location of
  a kerb or ramp in the photo.
* Imagery dates come only from what Codex reads off the screen ("Image capture
  Mar 2023"). Otherwise the date is recorded as unknown.

## Development

```bash
docker compose up -d db
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd ../frontend && npm install && npm run dev     # http://127.0.0.1:5173
cd ../backend && uv run pytest                   # a few fast checks, under a second
```

## Limitations

* The City of Sydney boundary in `config/` is a hand-drawn approximation.
  Streets outside it are left out. Replace it with the official boundary if precision matters.
* Divided roads appear in OpenStreetMap as two one-way lines, which plans
  photos on both sides of each carriageway. Untick or redraw them if that's unwanted.
* The checks rely on Codex logging the final address-bar URL. If it doesn't,
  the photo is still matched by file name, but flagged as not lined-up-checked.
