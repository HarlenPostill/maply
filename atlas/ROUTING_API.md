# Maply route preview API (`maply-route-preview/1`)

A local, read-only HTTP service that plans **unverified wheelchair route previews** over the
production atlas. It is for destination search and route planning only. It never offers
navigation or turn-by-turn guidance.

- Every route is `"status": "unverified_preview"`. `navigation_allowed` is always `false`.
- No field says or implies that anything is verified. Camera observations are evidence, not
  verification. Every crossing and every connector is unverified.
- The payloads in [`docs/routing-examples/`](docs/routing-examples) are produced by the real API
  from a synthetic test town (`tests/test_routing_examples.py`). The test fails when they drift
  from the code. Regenerate them with `UPDATE_ROUTING_EXAMPLES=1 uv run pytest tests/test_routing_examples.py`.

## Run it locally

```bash
cd atlas
uv sync
uv run python -m atlas designations   # after a street-file change: writes var/osm_way_designations.json
uv run python -m atlas serve          # http://127.0.0.1:8765  (--host, --port, --refresh-s)
```

`serve` reads `ATLAS_DATABASE_URL` (environment or `atlas/.env`):

- The session is `default_transaction_read_only=on`, with a 120 s statement timeout.
- The snapshot is read in one `REPEATABLE READ, READ ONLY` transaction that ends in a rollback.
- Nothing is written to the atlas, and no migration runs.

The server binds to loopback and loads the snapshot in the background. Every `--refresh-s`
(default 10 minutes) it checks three inputs and swaps in a new graph atomically:

| Input | How it is checked | On change |
| --- | --- | --- |
| Atlas content | A read-only md5 content marker over every table the snapshot reads: releases, coverage, segments, routing edges and nodes, observations and their features, including geometry, attrs, `footpath` and `needs_verification`. It takes about 2–3 s of database time in production. | Full snapshot reload |
| Designations file | Its content digest | Graph rebuilt from the current snapshot |
| Server date (drives imagery age) | | Graph rebuilt from the current snapshot |

An unreadable designations file keeps the current graph. The free-plan Railway database sleeps,
so the first load can take about 30 s, and connections are retried with backoff. Until a snapshot
is loaded, every endpoint answers `503 data_unavailable` with `Retry-After`.

`designations` reads `../tagger/data/osm/city_of_sydney_streets.geojson` (read-only) and writes an
OSM-derived file to `atlas/var/` (gitignored, ODbL, attribution kept). The running service only
reads that file and never touches the tagger. Without the file, no street gets the
pedestrian or shared-zone allowance, and `designation_unavailable` is listed in `limits`.

## Conventions

- Requests are JSON with `{"lat", "lng"}` points. Responses use GeoJSON `[lng, lat]` (WGS84).
  Lengths are metres, rounded to 0.1.
- Imagery months are `"YYYY-MM"`. `as_of_date` is the server date used for imagery age.
- Bodies are at most 4 KB of `application/json`. Unknown fields are rejected, and so is NaN.
- Nothing about a request is stored. Logs hold only the request id, method, path, status, error
  code and duration: no coordinates and no connection details. Responses carry
  `Cache-Control: no-store` and an `X-Request-Id`.
- Every response carries `api_version`, `release` and `graph_version`. `graph_version` is
  `r<release>-<12 hex>`: a sha256 over every effective input that can change a response. That is the
  full snapshot content (release, coverage, segment, edge and node geometry and lengths, observation
  positions, values and feature attrs), the designations content, the policy parameters and the
  as-of date. It changes when any of them changes.

## `GET /v1/health`

`200 {"status": "ready", "api_version", "release_id", "graph_version"}`, or
`503 data_unavailable` while loading.

## `GET /v1/coverage`

See [`coverage.json`](docs/routing-examples/coverage.json).

| Field | Meaning |
| --- | --- |
| `release` | Atlas release `id`, `published_at`, `schema_version` and a 12-character `fingerprint`. |
| `snapshot` | `graph_version`, `content_sha256` (atlas snapshot), `designations_sha256`, `loaded_at`, `as_of_date`, `community_observations`. |
| `area` | Survey boundary (`geometry`, `bbox`, `name`). `approximate: true`, `enforcement: "strict"`. |
| `survey` | `imagery_from`, `imagery_to`, `imagery_median`, and counts: `observations`, `undated_observations`, `older_than_3y_observations`, `flagged_uncertain_observations`. |
| `graph` | `street_m`. Footpath side pieces with metres by evidence and by exclusion reason (`excluded_footpath_side_m`, for example `fixed_barrier_flagged`), crossings excluded by reason (`excluded_crossings`), and `eligible_footpath_side_m`. `corners`, `crossings`, `shared_surface_transitions`, `connectors`, `connector_m`, `eligible_components`, `largest_eligible_component_share`. `designation` gives the source file, the share of street length matched to OSM ways, and metres per designation. |
| `policy` | `version`, the `settled` rules, `pending_decisions` and the engineering `parameters`. |
| `limits` | `[{code, message}]`, every known limitation. Show these to the user. |
| `snap_max_m` | 40. The largest endpoint gap the service will bridge. This is an engineering limit, not an accessibility claim. |
| `navigation_allowed` | Always `false`. |

## `POST /v1/route-preview`

Request ([`route-preview-request.json`](docs/routing-examples/route-preview-request.json)):

```json
{"origin": {"lat": -33.8998, "lng": 151.2014}, "destination": {"lat": -33.8995, "lng": 151.2022},
 "expected_graph_version": "r1-…"}
```

`expected_graph_version` is optional. If it is present and differs from the loaded snapshot, the
answer is `409 snapshot_changed`.

Response `200` ([`route-preview.json`](docs/routing-examples/route-preview.json)):

| Field | Meaning |
| --- | --- |
| `status`, `navigation_allowed` | Always `"unverified_preview"` and `false`. |
| `endpoints.origin` / `endpoints.destination` | `requested`, `matched`, `gap_m`, `street_name`, `side` (`left`/`right` of the line direction, or `null` when matched to a junction), `facing` (compass side), `side_ambiguous`. |
| `summary.route_m` | `street_m + connector_m`. It excludes endpoint gaps and crossings, whose lengths are not measured. |
| `summary.endpoint_gap_m` | Both endpoint gaps. Never part of the route and never assessed. |
| `summary.evidence_m` | `observed_m`, `uncertain_m` and `unknown_m`. These **partition `street_m`**. |
| `summary.designation_m` | `ordinary_m`, `designated_pedestrian_m`, `designated_shared_m` and `ambiguous_m`. These partition `street_m`. |
| `summary.imagery_age_m` | `within_3y_m`, `older_than_3y_m`, `undated_m` and `no_observation_m`. These partition `street_m`. |
| `summary.imagery_months` | `{from, to}` of the dated observations on the route, or `null`. |
| `summary.crossings` | `count` (street crossings) and `shared_surface_transitions` (across designated streets). All are unverified. |
| `summary.connectors.count` | Inferred links used. |
| `summary.observations` | `count` of camera observations on the route, and how many are `flagged_uncertain`. |
| `slope_observations` | Counts of feature observations per slope category. See below. |
| `width` | `{"status": "not_measured"}`. |
| `warnings` | `[{code, severity, leg, at, observed_on, message}]`, with `severity` `prominent` or `info`, ordered by leg. At most 300, with `warnings_truncated` set beyond that. |
| `legs` | The route in order. See below. |
| `geometry` | One LineString of the footpath legs and connectors, excluding endpoint gaps. Footpath legs are drawn `display_offset_m` (4 m) to their side of the centreline. That is a display choice, not a surveyed footpath position. |
| `limits` | The limitation codes that apply to this route. |

Legs:

| `kind` | Fields |
| --- | --- |
| `footpath_side` | `street_name`, `side`, `facing`, `designation`, `designation_basis`, `osm_way_ids` (the OSM ways matched alongside, as provenance), `length_m`, `evidence_m` and `imagery_age_m` (each partitions the leg), `observations`, `flagged_uncertain`, `imagery_months`, LineString. |
| `crossing` | `crossed_street`, `at`, `length_m: null`, `evidence: "unverified"`, Point. `nearby_observations` counts camera observations within 25 m of the junction that mention a kerb ramp (present, absent, raised lip or step) or a crossing (unmodified kerb, steps, level or ramp). Those counts are context only. A camera position cannot place a ramp at a particular kerb, so they never verify the crossing. |
| `shared_surface_transition` | Moving across a designated pedestrian street or shared zone at a junction. Same fields as a crossing. |
| `connector` | `length_m`, `evidence: "inferred"`, LineString. |
| `endpoint_gap` | `which`, `length_m`, `evidence: "not_assessed"`. The straight line from the requested point to the match. It never claims that this ground is passable. |

Warning codes:

| Severity | Codes |
| --- | --- |
| `prominent` | `crossing_unverified`, `shared_surface_transition_unverified`, `connector_inferred`, `footpath_unknown`, `steep_slope_observed`, `temporary_obstruction_observed`, `flagged_observation` (when the flagged photo showed a missing footpath or an unrecorded-permanence blockage), `blocking_permanence_unknown`, `narrowed_passage`, `footpath_interrupted`, `missing_surface_observed`, `driveway_lip_or_channel`, `endpoint_side_ambiguous` |
| `info` | `endpoint_gap_not_assessed`, `flagged_observation` (otherwise), `hazard_observed`, `surface_concern`, `driveway_dips`, `driveway_tilts_sideways`, `driveway_interrupted`, `no_separate_footpath` |

### Evidence lengths

Each camera observation stands for the stretch of its footpath side that is closer to it than to
any neighbouring observation, up to 10 m either way. Survey points are about 14 m apart.

- An unflagged observation makes its stretch `observed`.
- An observation flagged `needs_verification` makes it `uncertain`.
- Anything no observation reaches is `unknown`. A missing fact is never treated as known.

The 10 m reach is a reporting allocation, not a claim about what the camera saw. Imagery age uses
the newest dated observation in each stretch.

### Slope observations

```json
"slope_observations": {
  "unit": "feature_observations",
  "footpath":  {"longitudinal": {"apparently_level": 0, "gentle": 0, "moderate": 0, "steep": 1}, "cross": {…}},
  "driveway":  {"longitudinal": {…}, "cross": {…}},
  "kerb_ramp": {"slope": {…}, "longitudinal": {…}, "cross": {…}},
  "crossing":  {"longitudinal": {…}, "cross": {…}},
  "observations_without_slope": 8
}
```

These count features on route observations, by category, straight from the imagery tags. They are
not lengths, gradients or percentages. An observation with no slope tag counts in
`observations_without_slope`, never as level. Slopes never change the route. Each steep
observation adds a prominent warning. In production, about 3% of observations carry any slope
tag, and none says `steep`.

### Errors

`{"api_version", "error": {"code", "message", "details"}}`. Examples are in
[`errors.json`](docs/routing-examples/errors.json).

| HTTP | `code` | `details` |
| --- | --- | --- |
| 400 | `invalid_request` | `{"fields": ["origin.lat", …]}` (field names only, never values) |
| 413 | `payload_too_large` | `{"max_bytes": 4096}` |
| 415 | `unsupported_media_type` | |
| 422 | `outside_coverage` | `{"which": "origin" \| "destination"}` |
| 422 | `too_far_from_network` | `{"which", "max_m": 40}` |
| 422 | `no_eligible_footpath_nearby` | `{"which", "max_m", "excluded_nearby", "excluded_reasons"}`. The reasons are `fixed_barrier`, `fixed_barrier_flagged`, `footpath_absent` and `footpath_ends`. |
| 422 | `same_location` | `{"distance_m"}` (under 5 m apart) |
| 404 | `no_route` | `{"reason": "disconnected" \| "blocked_by_exclusions", "origin_in_main_network", "destination_in_main_network"}` |
| 409 | `snapshot_changed` | `{"graph_version"}` |
| 408 | `request_timeout` | `{"timeout_s": 10.0}` (the body did not arrive in time) |
| 429 | `rate_limited` | `Retry-After` in seconds. Public deployment only (see below). |
| 503 | `busy` | `Retry-After: 2` (4 concurrent searches) |
| 503 | `data_unavailable` | `{"reason": "loading" \| "database_unreachable" \| "integrity_failed" \| "misconfigured"}`, `Retry-After: 30` |
| 504 | `route_timeout` | `{"timeout_s": 3.0}` |
| 404 / 405 | `not_found` / `method_not_allowed` | |
| 500 | `internal_error` | |

If the client disconnects, the search is cancelled and the server answers 499 to nobody.

## Routing model

- **Side-aware graph.** Each street piece between junctions becomes a left and a right footpath
  element. At each junction the street arms are ordered by angle, and each gap between neighbouring
  arms is a corner. Following one side round a corner is free. Reaching another corner means
  crossing one arm, which is an unverified `crossing` (or a `shared_surface_transition` across a
  designated street). Steps reported near a junction exclude every crossing there (see the
  policy). Crossings exist only at junctions of three or more arms and at street ends
  that meet nothing. **There are no mid-block crossings**, including where two streets meet end to
  end at a bend.
- **Street ends.** A street end that meets nothing has separate left and right corners, joined by
  one unverified crossing.
- **Connectors.** These are the atlas's inferred links. Each joins the corners either side of where
  it attaches, and at a street end it joins both corners. Always `evidence: "inferred"`.
- **Endpoint matching** is an engineering step, not an accessibility claim:
  - Both endpoints must be inside the boundary.
  - A point is matched only to the footpath side it lies on, within 40 m of the centreline.
  - The straight line to the centreline must not cross or touch any other street centreline. Passing
    through a street's interior vertex, a junction or along a street counts as touching. Only
    contact within 0.05 m of either end of the gap is allowed: the requested point itself, and the
    matched point on its own street or junction.
  - If the footpath beside the point is excluded, no match more than 5 m farther away is allowed.
    That stops matching from bypassing the exclusion.
  - A point within 0.5 m of the centreline tries both sides and sets `side_ambiguous`.
  - A point beyond a street end is matched to the junction corner whose angle contains it (both
    corners at a dead end).
  - Up to 8 candidates are tried. The gap counts three times its length in route choice.
- **Costs.** Footpath metres. Uncertain stretches ×1.3 and unknown ×1.5, so observed footpath is
  preferred. 30 m-equivalent per crossing, 10 m per shared-surface transition, connectors ×1.5.
  Slope and imagery age never change cost.
- **Limits.** 4 concurrent searches, a 3 s compute budget, cancellation on disconnect, 4 KB
  bodies that must arrive within 10 s. The public deployment adds rate limits (below).

## Policy `manual_chair_v1`

All settled by the user; `policy.pending_decisions` is empty. "Flagged" means `needs_verification`.

| Rule | Trigger (raw `maply-accessibility/1` tags, on route) |
| --- | --- |
| **Fixed barriers and steps exclude, even when flagged** | Footpath `interruption: steps`. Driveway `entry_join`/`exit_join` of `step` or `open_gap`. A hazard that blocks (`clear_passage: blocked`, or `affected_width: full_width` with `bypass: not_visible`) and is `on_route` or `partially_encroaching`, with `persistence: fixed`, or of type `step` whatever its persistence tag says (a blocking step is built form). A flagged report gives the exclusion reason `fixed_barrier_flagged`, so the re-check uncertainty stays visible in diagnostics; an unflagged one gives `fixed_barrier`. |
| **Steps reported near a junction exclude every crossing at that junction** | A crossing `transition_type: steps`, or a kerb ramp `bottom_join: step`, on a camera observation within 25 m of the junction. This is a deliberately broad, conservative *association*, not a location. The camera position cannot say which arm, kerb or even which junction the steps belong to, when two junctions are within 25 m. Excluded crossings get `steps_reported_nearby` (or `steps_reported_nearby_flagged`). That means "steps were reported somewhere near here", **not** that each excluded crossing has steps. No production observation triggers this today. |
| Known missing footpath on an ordinary or ambiguous street: the side piece is **excluded**, even in previews | An unflagged observation with footpath `absent`, or a footpath section with `continuity: ends` or `interruption: ends_in_road`, anywhere on the piece. A flagged one is uncertain and does not exclude. |
| **Blockage of unrecorded permanence: eligible, with a prominent warning** | Footpath `interruption: barrier` or `construction`, footpath or driveway `clear_passage: blocked` (no permanence tag exists for these), or a blocking hazard with unknown `persistence` (other than a step). Warning `blocking_permanence_unknown`, flagged or not. It is never called fixed or temporary. |
| Temporary obstruction: eligible, with a prominent warning | A blocking hazard with `persistence: temporary` (other than a step). |
| Non-blocking hazard of any permanence: eligible, with an info warning | |
| Designated pedestrian street or shared zone | Missing footpath does not exclude. Fixed barriers and steps still do. Applies only when the per-way designation covers the whole piece. |
| Other flagged observations | Never exclude. The stretch is `uncertain`, and a warning says what was shown. |
| Imagery age | Reported only. |
| Steep slope | Eligible, with a prominent warning. No slope affects cost. |

Production effect (release 1):

| Rule | Footpath side excluded |
| --- | --- |
| Unflagged fixed barriers (6 observations) | 418 m |
| **Flagged** fixed barriers and steps (206 observations, including all 87 step reports) | 13.4 km |
| Known missing footpath | 83.6 km |
| Footpath ends | 146 m |

- Unknown-permanence blockages (65 observations, 61 of them flagged) now warn instead of excluding.
- No production observation reports steps at a crossing, so no crossings are excluded today.

## Designated streets

The tagger merged OSM ways by street name and kept the first way's `highway` for the whole name.
116 names mix `pedestrian`/`living_street` with ordinary classes, so `atlas.segments.highway` is
never used for the allowance. `python -m atlas designations` keeps each OSM way's own id,
geometry and class.

Each street piece is sampled every 2 m (1 m in from each end). For each sample, **every** same-name
way that runs alongside counts: within 3 m, with the sample projecting onto the way rather than
beyond its ends. So input order never breaks a tie, and a way that merely starts at a junction
does not count for the piece that leaves it.

| `designation_basis` | Samples | Designation |
| --- | --- | --- |
| `per_way_match` | all `pedestrian` | `designated_pedestrian` |
| `per_way_match` | all `living_street` | `designated_shared` |
| `per_way_match` | all ordinary classes | `ordinary` |
| `conflicting_ways` | any sample with ways of different classes alongside it, for example a pedestrian way and a residential way over the same geometry | `ambiguous`, with no allowance |
| `mixed_along_piece` | classes change along the piece | `ambiguous` |
| `unmatched_samples` | a sample with no way alongside | `ambiguous` |
| `designations_unavailable` | no file | `ambiguous` |

Matched way ids are kept as provenance (`osm_way_ids` on legs). Duplicate ways of the same
class agree. In production every sample matched at least one way:

| Designation | Length |
| --- | --- |
| ordinary | 373.5 km |
| designated pedestrian | 43.9 km |
| designated shared | 13.6 km |
| ambiguous | 5.1 km (453 m `conflicting_ways`, 4.6 km `mixed_along_piece`) |

## Known limits (`limits` codes)

- `preview_only`: nothing is verified, and nothing is fit to navigate by.
- `crossings_inferred_at_junctions`: crossings are placed at street junctions. There are no
  crossing locations, signals, kerb heights or widths, and none at bends between two streets.
- `camera_position_not_feature_position`: observations sit at the Street View camera, about
  1.3 m from the centreline.
- `no_footway_network`: only seven road classes are mapped. No park, plaza or laneway paths,
  service roads or steps.
- `no_grade_separation`: bridges and tunnels are unknown. Streets that cross on the map are
  treated as meeting.
- `width_not_measured`, `slope_categorical_only`, `imagery_age`, `approximate_boundary`.
- `endpoint_gap_not_assessed`: building entrances are not in the data.
- `osm_tags_merged_by_name`: OSM `sidewalk` and `width` tags are not used.
- `no_community_observations`, `connectors_inferred`, `designation_unavailable`.
- **Whole-piece exclusion.** One unflagged "absent" observation, or one reported fixed barrier or
  step even if flagged, excludes its whole side piece (about 47 m on average). In production that
  excludes 96.3 km of 872 km of footpath sides (some pieces have more than one reason).
- **Small islands.** 193 separate eligible components; the largest holds 95.8% of eligible
  footpath length. Isolated plazas such as St Andrews Place give `no_route` with
  `origin_in_main_network: false`.
- `atlas.observation_signals`, `segment_sides.score/grade` and `routing_network.cost` are not used.
  They mistake "observed, no concern" for "not observed" when a tag is missing. A fix to the
  database views is still a proposal: no migration has been written.

## Public deployment (`atlas/api/`)

The Railway service `atlas-api` in `maply-atlas` runs `python -m atlas.routing.production`. It
differs from `serve` only in configuration and limits:

- **Database login.** It reads `ATLAS_API_DATABASE_URL` from the environment only, never
  `atlas/.env` or `ATLAS_DATABASE_URL` (the owner). The login is `atlas_api`: a member of
  `atlas_read`, with no direct grants, `default_transaction_read_only = on`, a 120 s statement
  timeout, a 60 s idle-in-transaction timeout and a connection limit of 5, all stored on the role.
  It connects to `atlas-db` over Railway's private network with TLS. Before reading any data, each
  load checks the login's catalog facts: not `atlas`, `postgres`, `maply` or `tagger`; no superuser,
  role, database, replication or RLS-bypass rights; no create or owner rights on the database or any
  schema; no write, truncate, trigger, reference or maintain right on any atlas relation; no
  privileged built-in role; and the read-only default stored on the role. A failed check answers
  `503 data_unavailable` with reason `misconfigured`, and at startup it stops the process (exit 78).
- **Rate limits**, in memory, answered with `429 rate_limited` and `Retry-After` before the body is
  read:

  | Endpoint class | Per client | Whole service |
  | --- | --- | --- |
  | `POST /v1/route-preview` | burst 10, then 1 every 3 s | burst 30, then 4 a second |
  | `GET` (health, coverage) | burst 30, then 1 a second | burst 100, then 30 a second |

  This device-testing deployment shares the per-client budget across all clients. It does
  not trust proxy headers; neither rotating headers nor proxy peers changes that bucket.
  Per-client IP isolation can be enabled only after edge overwrite behaviour is verified.
  The independent global caps always apply. `api/check.sh` uses a bounded concurrent burst
  to check the shared limit; it makes no claim about header rewriting.
- **Server.** uvicorn with no access log, no proxy-header rewriting, no `Server` header, at most 64
  connections, and keep-alive longer than the edge's 60 s idle timeout. No OpenAPI or docs routes.
  The coverage body is built once per graph.
- **Readiness.** The first snapshot is loaded before the port opens, so the Railway health check
  (`/v1/health`, 300 s) passes only on a ready service. If the database stays unreachable, the
  service starts anyway, answers 503 and keeps retrying. It refreshes every 30 minutes
  (`ATLAS_API_REFRESH_S`).
- **Image.** `api/stage.sh` assembles the upload from an allowlist: the Dockerfile, `railway.toml`,
  `pyproject.toml`, `uv.lock`, `src/atlas/**/*.py`, `var/osm_way_designations.json` and a NOTICE with
  the OpenStreetMap (ODbL) attribution. It refuses anything else, including dotfiles, secrets,
  connection strings with passwords, and a designations file with unexpected fields. The image runs
  as a non-root user and holds only locked runtime dependencies.

Measured locally with the full release (52,483 observations): ready 6 s after start, about 270 MB
resident, about 400 MB at peak while a new snapshot is swapped in. The free plan allows 512 MB.

Deploy and redeploy: `atlas/api/provision.sh` (see the [README](README.md#route-preview-api-on-railway)).
