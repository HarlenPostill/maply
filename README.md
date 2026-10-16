<img src="assets/icon.svg" width="64" height="64" alt="">

# maply

A local tool for **planning, supervising and inspecting** Street View image
collection for an authorised wheelchair-accessibility research project in the
City of Sydney. It covers collection and supervision only. Accessibility image
analysis and routing are separate downstream components, and the dashboard shows
**no accessibility scores**.

* **Plan:** draw a route or polygon (or import GeoJSON, or paste a Street View
  URL), choose sides, interval and views, then review and edit a persistent capture plan.
* **Collect:** a durable scheduler drives local Chromium workers (deterministic,
  OpenAI computer-use, manual, or the official Static API) under geographic,
  rate, capture, time and budget limits.
* **Verify:** every capture is checked by the application (page state, frame
  quality, position, orientation, imagery date, attribution, scope) before it is
  accepted. An agent's claim of success is never enough.
* **Inspect:** coverage (collection completeness vs verified coverage, gaps from
  actual camera positions), a local gallery with full metadata and provenance,
  accept / reject / recapture actions, and local dataset packaging.

Everything is stored on this machine. Cloud image processing is **off by
default** and blocked at the transport layer. See
[docs/DATA_BOUNDARIES.md](docs/DATA_BOUNDARIES.md).

## Quick start (Docker)

```bash
cp .env.example .env              # optional: defaults are LOCAL ONLY + synthetic-ready
docker compose up -d --build      # db + api + worker, all on 127.0.0.1
open http://127.0.0.1:8000        # dashboard
```

To try it without touching any live service, create a project, draw a short
route in the CBD, generate and approve a plan, and run a job with target
**synthetic**. The worker drives local Chromium against the built-in **mock
panorama site**. Every synthetic image is watermarked
`SYNTHETIC MOCK PANORAMA … NOT Google Street View`.

Persistent data lives in `./data` (images, thumbnails, previews, exports,
browser profiles; bind-mounted, gitignored, excluded from image builds) and
the `pgdata` volume (database).

## Development (host)

```bash
docker compose up -d db                           # PostGIS on 127.0.0.1:5433
cd backend
uv sync && uv run playwright install chromium
uv run alembic upgrade head
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8000   # API + mock site (+ dashboard if built)
uv run python -m app.workers.main                              # worker supervisor (WORKER_HEADLESS=false to watch)

cd ../frontend && npm install && npm run dev      # http://127.0.0.1:5173 (proxies to :8000)
npm run build                                     # dashboard served by the API at :8000
```

## Tests

```bash
cd backend && uv run pytest            # ~100 tests; needs the db service (uses database maply_test)
docker compose run --rm api pytest     # same, inside the container
```

| Area | Tests |
|---|---|
| Geometry & side conventions | `test_geometry.py`: metres vs degrees, true-north bearings, left/right relative to stored direction, reverse travel, stable segment IDs, crossings, approach labels, scope |
| Leases & scheduler | `test_scheduler_leases.py`: concurrent claims, fencing tokens, expired-lease recovery, bounded retries/backoff, pause/cancel, shared rate limiter, permission limits |
| Deduplication & crash recovery | `test_dedup_and_recovery.py`: stale completions rejected, partial unique index, tampered/missing artefacts, panorama reuse, dead-slot recovery, orphan files |
| Cloud-image blocking | `test_egress.py`: detection, decision matrix, real OpenAI SDK through a mock transport, no retries of blocked requests, single construction point, no tracing deps, log redaction |
| Verification | `test_verification.py`: URL parsing, explicit-date-only policy, blank/transition frames, offsets, scope, unknown-date policies |
| End to end (real Chromium + supervisor) | `test_integration_workers.py`: synthetic job with faults, pause/resume + `kill -9` restart, CAPTCHA manual intervention, OpenAI agent loop vs a local fake provider (allowed and blocked), two workers |
| API & guards | `test_api.py`, `test_static_api.py` |

Live Google/OpenAI checks are **not** part of the suite. See
[docs/LIVE_INTEGRATION.md](docs/LIVE_INTEGRATION.md).

## Documentation

* [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md): day-to-day use
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design, geographic model, state machine
* [docs/DATA_BOUNDARIES.md](docs/DATA_BOUNDARIES.md): what stays local, what can be transmitted
* [docs/LIVE_INTEGRATION.md](docs/LIVE_INTEGRATION.md): remaining setup for Google and OpenAI, plus bounded live checks

## Limitations (current state)

* **No live integration has been exercised.** The Google Maps page adapter and
  the OpenAI adapter are implemented from documentation and tested only against
  local stand-ins.
* The shipped City of Sydney scope polygon is a **hand-digitised approximation**.
  Replace it before live collection.
* Intersection detection is heuristic (route endpoints, turns, crossings of
  drawn routes). There is no road-network extraction.
* Footpath offset lines are planning aids, not surveyed geometry. The
  `geometry_consistent` association status does not mean the footpath is
  visible. Only operator review (`operator_verified`) asserts that.
* Frame checks (blank / uniform / unstable) are heuristics. Unusual imagery may
  need review.
