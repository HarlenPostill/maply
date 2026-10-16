# Data boundaries: what stays local, what can leave the machine

This tool supports a research approval that limits Street View data to a stated
research purpose, **local storage and local use**, with no publication or
unrelated reuse. This document describes exactly how the software behaves. It
is not legal advice and does not replace the approval's terms.

## 1. Stored only on this machine

| Data | Location | Notes |
|---|---|---|
| Raw screenshots / images | `data/projects/<project>/jobs/<job>/images/<observation>.png` | Raw frames are always kept; crops never replace them |
| Thumbnails, analysis crops | `.../thumbs/`, `.../crops/` | Crops record the raw image checksum and crop geometry |
| Worker live previews | `.../workers/<worker>-latest.jpg` | Overwritten in place; served to the local dashboard only |
| Plans, tasks, leases, observations, metadata, audit events, egress log | PostgreSQL volume `pgdata` | Bound to `127.0.0.1:5433` |
| Browser profiles (cookies, consent state) | `data/browser-state/<target>/profile-<n>/` | One profile per concurrently running worker; never shared |
| Local dataset packages | `data/exports/<project>-<timestamp>/` | Manifest + relative paths + checksums |
| Orphaned files (crash leftovers) | `data/orphans/` | Never counted as captures |

* `data/` is bind-mounted into containers, **excluded from every image build**
  (`.dockerignore`) and **gitignored**. The dataset is never baked into an image
  or committed.
* All published ports bind to `127.0.0.1`. The API rejects requests whose Host
  header is not a loopback name (DNS-rebinding protection), refuses
  cross-origin state-changing requests and has no CORS.
* There is **no upload, publish or share function** anywhere in the code base.
  "Export" writes a directory under `data/exports/` for local downstream tools.
* Retention: the per-project `retention_days` drives a preview and an explicit,
  confirmed "apply retention" action that deletes local images, records and exports
  older than the cutoff.

## 2. What can be transmitted, and when

| Destination | Sent by | What is sent | When |
|---|---|---|---|
| Planning basemap tile server (default OpenStreetMap) | Your browser (dashboard) | Tile requests (z/x/y) | Whenever the Plan/Coverage map is open. Set `BASEMAP_URL=none` to disable. |
| Google Maps (web) | Local Chromium worker | Ordinary page requests, exactly as a person browsing | Only jobs with target `google_maps` |
| Google Street View Static API | Worker | Metadata/image requests with your API key | Only method `streetview_static_api`, if permitted and configured |
| OpenAI API | Worker | Model check (`models.retrieve`, no images); task text; **screenshots only if permitted** | Only `openai_agent` jobs and the startup check |

Nothing else is contacted. The worker's Chromium runs with an **allowlist of
hosts** for the active site; all other requests are aborted, downloads are
disabled and popup windows are closed. A web page therefore cannot make the
worker send data to a third party, and an agent cannot navigate it away from
the site.

## 3. Cloud image processing switch

`ALLOW_CLOUD_IMAGE_PROCESSING` (operator, environment) **and** the project's
"allow third-party processing" + processor list `openai` (dashboard) must both
be enabled before any research screenshot can be sent to OpenAI.

Enforcement is at the **HTTP transport layer** (`app/providers/egress.py`),
below the OpenAI SDK, its retries and any request builder:

* Every outgoing request body is scanned for image content (data URLs,
  `input_image` / `computer_screenshot` / `image_url` items, base64 PNG/JPEG/WEBP
  signatures, raw image bytes, multipart image parts).
* If images are present and the rule does not allow them, the request is
  **refused before any byte is sent** (`CloudImageTransmissionBlocked`), the
  job is paused with the reason, and the decision is logged as an `EgressEvent`
  (never the payload).
* A request whose body cannot be inspected is treated as image-bearing (fail closed).
* An unknown data classification is treated as research imagery.
* The OpenAI client can only be constructed in `app/providers/openai_adapter.py`,
  always with the guarded transport (enforced by a test that scans the code).
* Local logs pass through a redaction filter that removes data URLs and long
  base64 runs; SDK loggers are held at WARNING so request bodies are never logged.
* No tracing or telemetry SDK is installed or imported (enforced by a test);
  `OPENAI_AGENTS_DISABLE_TRACING=1` is set defensively, and `DO_NOT_TRACK=1`.
* Playwright tracing/video is not used. Screenshots are never sent to a remote browser service.

With the switch **off**, deterministic browser collection, manual mode and the
Static API adapter all keep working; `openai_agent` jobs are refused at creation
time with an explanation (and blocked at runtime if configuration changes).

`ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING` separately permits sending **synthetic
mock imagery** (never research data) so the OpenAI adapter can be tested
against the local mock site. A page is classed as synthetic only when the job's
target is the mock site, whose host allowlist contains only the local mock origin.

## 4. When cloud processing is enabled

Only the *necessary* model inputs are sent:

* The model is consulted only for **recovery** (the page is not already in a
  verified Street View state). Normal captures are deterministic and send nothing.
* The first request is text only; screenshots go back only in answer to the
  model's `computer_call`.
* Requests are stateless (`store=false`) and history is trimmed to the last
  few turns, so older screenshots are not re-sent indefinitely.
* `store=false` is a supported **request-level data control. It is not a
  zero-retention guarantee.** OpenAI's documentation states that abuse-monitoring
  logs may be retained (up to 30 days by default) unless your organisation has a
  separate Zero Data Retention arrangement, and that image inputs (including
  computer-use screenshots) are subject to safety scanning. Confirm the
  arrangement that applies to your organisation before enabling the switch.
* The dashboard header always shows the active mode (`LOCAL ONLY` or
  `CLOUD IMAGE PROCESSING ENABLED`), and the egress log lists every allowed or
  blocked request.
