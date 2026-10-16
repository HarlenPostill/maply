# Live integrations: setup and bounded checks

Everything in the automated test suite is synthetic: the local mock panorama
site and an in-process or local fake OpenAI endpoint. **Passing tests do not
demonstrate a live Google or OpenAI integration.** Use the steps below, inside
your approval's limits, to validate the live paths.

## A. Google Maps web (deterministic browser)

No credentials are needed. The worker loads Google Maps exactly as a browser
would, using the documented Maps URLs Street View action
(`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=…&heading=…&pitch=…&fov=…`).
It never calls tile endpoints or private APIs.

1. Replace the approximate scope (`config/city_of_sydney_approx.geojson`) with the
   official City of Sydney LGA boundary, or upload it in Settings → scope.
2. In Settings, record the approval reference and expiry date, and keep
   `max_requests_per_minute` conservative (the default ceiling is 10/min per project).
3. Run the worker **on the host in headed mode** for the first live job, so you can
   see the browser and handle any consent or challenge page yourself:
   ```bash
   cd backend
   uv sync && uv run playwright install chromium
   DATABASE_URL=postgresql+psycopg://maply:maply@127.0.0.1:5433/maply \
   MOCK_PANORAMA_BASE_URL=http://127.0.0.1:8000 WORKER_HEADLESS=false \
   uv run python -m app.workers.main
   ```
   (Stop the Docker `worker` service first, or leave it running: slot locks prevent
   duplicate slots, but you won't know which process takes the job.)
4. Create a plan with **1–3 samples** and a job with target `google_maps`, method
   `deterministic_browser`, 1 worker, max_captures 6. Or use the bounded script,
   which makes one sample and two views at 2 requests/min, and prints the recorded provenance:
   ```bash
   cd backend && uv run python ../scripts/live_check.py --confirm-approved-scope \
     --lat -33.87410 --lng 151.20680 --bearing-to-lat -33.87380 --bearing-to-lng 151.20685
   ```
5. Inspect each observation in the Gallery. Check:
   * `camera_position.provenance = page_url`, with a plausible camera offset;
   * observed heading/pitch/fov parsed from the URL (or `unknown`);
   * imagery date from an explicit "Image capture" label (`ui_text`), otherwise `unknown`.
     The adapter deliberately ignores copyright years;
   * attribution text captured (otherwise flagged `attribution_missing`);
   * that the raw frame shows imagery, not a consent page or a transition.
6. If Google shows a consent, sign-in or unusual-traffic page, the job pauses
   with `manual_intervention`. Resolve it yourself in the headed window (or with
   Run → worker → Manual control), then press **Resume**. Workers never bypass these.

**Known fragility.** The Google page adapter (`workers/sites/google_maps.py`)
reads the address-bar format and visible UI text. Neither is a published
contract, and both can change without notice. Each value carries provenance.
Missing values become `unknown`, and the capture is flagged for review rather
than guessed.

## B. OpenAI computer-use adapter

Implemented against the Responses API GA computer tool (`tools=[{"type":"computer"}]`,
batched `actions[]`, `computer_call_output` with `computer_screenshot`), checked
against developers.openai.com docs and openai-python 3.31 as of 2026-10-16.
`computer-use-preview` was shut down on 2026-07-23 and is not used.

1. In `.env`: set `OPENAI_API_KEY` and `OPENAI_COMPUTER_USE_MODEL` (a model whose
   docs page lists the computer tool, e.g. `gpt-6.1-sol`). Set the `OPENAI_PRICE_*`
   values from the pricing page for that model (they are used for estimates only).
2. Restart the worker. Settings → System → checks should show
   `openai_model_access: ok` (`models.retrieve`, no images).
   `OPENAI_STARTUP_CHECK=probe` additionally makes one tiny, text-only Responses call.
3. **Synthetic first.** Set `ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING=true`, then run a
   synthetic `openai_agent` job with mock faults, e.g. `{"fault_by_ordinal": {"1": "overlay"}}`,
   and a small model budget (e.g. $0.50). Confirm in the egress log that requests are
   `allowed / synthetic`, and check the model-usage numbers.
4. **Research imagery** requires *both* `ALLOW_CLOUD_IMAGE_PROCESSING=true` *and*
   the project setting "allow third-party processing" with processor `openai`.
   Review docs/DATA_BOUNDARIES.md §4 before enabling. `store=false` is not zero retention.
5. Provider safety checks (`pending_safety_checks`) are never auto-acknowledged.
   The task is handed to the operator and the job pauses.

**Not verified here.** No live OpenAI call was made while building this
software (no key was available). The request and response shapes follow the docs
and the SDK types. The automated tests drive the real SDK against a local fake
endpoint that mimics those shapes.

## C. Street View Static API (official, optional)

1. Enable it in Settings → permitted access methods (`streetview_static_api`) only if
   your approval covers it. Set `GOOGLE_MAPS_API_KEY` (and
   `GOOGLE_MAPS_URL_SIGNING_SECRET` if your key requires signed requests).
2. Job: target `google_maps`, method `streetview_static_api`. Metadata (free) gives
   pano ID, camera location, capture month and copyright (`static_api_metadata`
   provenance). Requested size is capped by `STATIC_API_MAX_SIZE` (default 640; verify against current Google limits).
3. Not tested live here. Covered by mock-transport tests only.
