# maply atlas

The production database behind Petal Path and wheelchair routing. It holds the
facts from the Maply survey and nothing else: no photos, thumbnails, model
confidence, evidence text, image regions, prompts or queue state. The tagger in
[`../tagger`](../tagger/README.md) stays the source of truth for those.

PostgreSQL 17, PostGIS 3.5 and pgRouting 3.8 on Railway (`database/`), with TLS on
the public proxy. The schema is plain SQL in `migrations/`, so any app stack can use it.

## What is in it

Current dataset (City of Sydney, release 1):

| | |
| --- | --- |
| Street segments | 6,214 (436 km of centreline) |
| Survey points | 58,314: 52,483 observed, 2,404 no imagery, 2,616 skipped, 811 blocked |
| Observations | 52,483: footpath present 48,933, absent 3,545, undetermined 5 |
| Features | 49,648 footpath sections, 2,283 hazards, 2,122 driveways, 552 kerb ramps, 58 crossings |
| Footpath sides | 7,924 good, 777 fair, 731 poor, 366 no footpath, 952 unknown |
| Routing graph | 7,754 nodes, 9,434 edges; 92.8% of nodes in one connected network |

The database is about 76 MB.

## Schema (`atlas` schema)

```
segments ─┬─ survey_points ── observations ── observation_features
          ├─ segment_sides     materialized: per-side summary, score, grade
          └─ routing_edges ── routing_nodes
             routing_network   materialized: pgRouting-ready wheelchair costs
verification_targets           view: where a fresh look is most useful
```

- **segments**: street centreline pieces with `street_name`, `highway` and
  walking-relevant OSM tags (`sidewalk*`, `surface`, `lit`, `maxspeed`, `width`, `kerb`).
  `left`/`right` are relative to the line direction. Ids are stable across
  publishes (keyed by the tagger's `maply_key`).
- **survey_points**: every planned survey location, including those without
  usable imagery (`status`: observed, pending, no_imagery, blocked, skipped).
  `facing` is the compass side of the street the footpath is on.
- **observations**: one look at one footpath side. `source` is `maply_survey` or
  `community`. `observed_on` is the Street View imagery month for survey data
  (15,310 survey photos had no published imagery date, so it is NULL there).
  `footpath` is present, absent or NULL. `needs_verification` marks results the
  review flagged as uncertain.
- **observation_features**: what was seen in the observation (`footpath_section`, `driveway`,
  `kerb_ramp`, `crossing`, `hazard`). `attrs` holds the known values only, in
  the `maply-accessibility/1` vocabulary, for example
  `{"surface": "concrete", "condition": "no_visible_defect", "clear_passage": "unobstructed"}`.
  Unknown values are left out.
- **observation_signals** (view): per observation, whether each concern was seen
  (`passage`, `surface`, `slope`, `crossing`, `hazard`, `barrier`, `quality`).
  NULL means not observed. Survey and community data share this definition.
- **segment_sides**: per footpath side, the planned/observed counts, the
  footpath status (present, partial, absent, unknown), the share of observations
  showing each concern, the dominant surface, the imagery date range, a 0–1
  `score`, a `grade`, and a line offset 5 m to that side for map rendering.
- **routing_network**: segments split at every junction. Short `connector`
  edges are added where a surveyed street stops up to 12 m short of another, and
  where an island (usually the far carriageway of a divided road) lies within
  20 m of the main network. `cost` is metres × the grade multiplier of the better
  footpath side: good 1.0, fair 1.3, unknown 1.6, poor 2.5, no footpath 8.0.
  Pedestrian streets and shared zones count as fair when there is no separate footpath.
- **verification_targets**: survey points ranked for Petal Path visits. The
  reasons are `no_data` (priority 3), `uncertain` (2), and `stale`, meaning imagery
  older than 3 years or undated (1). A newer confident observation of the point
  removes it from the list.
- **releases**: one row per publish, with the source fingerprint and counts.
- **coverage_areas**: the surveyed region (City of Sydney).

The score and grade definitions live in `atlas.side_score`, `atlas.side_grade`
and `atlas.grade_cost_multiplier`. They are a transparent first heuristic, built
to be tuned. Change them in a new migration, then call `atlas.refresh_derived()`.

## Commands

Run from this folder. `ATLAS_DATABASE_URL` comes from the environment or
`atlas/.env`, which is private and gitignored.

```bash
uv sync
uv run python -m atlas publish --dry-run  # extract from the local tagger and print counts
uv run python -m atlas migrate            # apply new migrations
uv run python -m atlas publish            # replace the survey layer (no-op if unchanged)
uv run python -m atlas verify             # compare production with a fresh tagger extract
uv run python -m atlas status             # summary of the production database
```

`publish` reads the tagger database inside a read-only transaction
(`MAPLY_DATABASE_URL`, default `127.0.0.1:5433`). It writes production in one
transaction. Segment and survey point ids are kept, community observations are
never touched, and segments still used by player data are not deleted. The
routing graph and derived views are rebuilt in the same transaction.

### After collecting more data

1. Make and capture a new job in the tagger as usual.
2. `uv run python -m atlas publish`, then `uv run python -m atlas verify`.

Segments from all tagger jobs are published. If two jobs plan the identical
geometry, the segment is kept once.

## Railway

`database/` builds the production image: the pinned `pgrouting/pgrouting:17-3.5-3.8.0`
base plus a self-signed TLS certificate kept on the volume. `database/provision.sh`
creates the `maply-atlas` project and the `atlas-db` service with a volume at
`/var/lib/postgresql/data` and a generated password. It deploys the image, writes
`atlas/.env`, then publishes and verifies. It is safe to re-run. The one manual
step is enabling the TCP proxy (port 5432) in the service's Networking settings,
and the script waits for it.

Live: project `maply-atlas`, service `atlas-db`, on the free plan (500 MB volume, so
WAL is capped at 128 MB; set `PG_MAX_WAL_SIZE` higher on a paid plan). Redeploy the
image with `railway up ./database --path-as-root --service atlas-db`.

Other Railway services in the project should connect over the private network
with `${{atlas-db.DATABASE_URL}}`. Use `DATABASE_PUBLIC_URL` (TLS) only from outside Railway.
Read-only app roles can be granted `atlas_read`; `atlas_api` (the route preview API) is one.

## Example queries

Nearest verification targets for a player:

```sql
SELECT survey_point_id, reason, priority, last_observed_on,
       ST_Distance(geom::geography, ST_MakePoint(:lng, :lat)::geography) AS metres
FROM atlas.verification_targets
WHERE ST_DWithin(geom::geography, ST_MakePoint(:lng, :lat)::geography, 500)
ORDER BY priority DESC, metres
LIMIT 20;
```

Wheelchair route between two points:

```sql
WITH a AS (SELECT id FROM atlas.routing_nodes ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:from_lng, :from_lat), 4326) LIMIT 1),
     b AS (SELECT id FROM atlas.routing_nodes ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:to_lng, :to_lat), 4326) LIMIT 1)
SELECT r.seq, n.street_name, n.grade, n.length_m, ST_AsGeoJSON(n.geom) AS geometry
FROM pgr_dijkstra('SELECT id, source, target, cost, reverse_cost FROM atlas.routing_network',
                  (SELECT id FROM a), (SELECT id FROM b), directed => false) r
JOIN atlas.routing_network n ON n.id = r.edge
ORDER BY r.seq;
```

Map layer of footpath grades in a bounding box:

```sql
SELECT segment_id, side, grade, footpath, dominant_surface, ST_AsGeoJSON(geom)
FROM atlas.segment_sides
WHERE geom && ST_MakeEnvelope(:west, :south, :east, :north, 4326);
```

Record a player observation, then refresh the derived views (batch refreshes in a real app):

```sql
INSERT INTO atlas.observations (source, survey_point_id, segment_id, side, geom, observed_on, footpath)
VALUES ('community', :point_id, :segment_id, :side, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), current_date, 'present')
RETURNING id;
INSERT INTO atlas.observation_features (observation_id, kind, attrs)
VALUES (:observation_id, 'footpath_section', '{"presence": "present", "surface": "concrete", "condition": "visible_defect"}');
SELECT atlas.refresh_derived();
```

## Route preview API

`uv run python -m atlas serve` runs a local, read-only HTTP API for unverified wheelchair route
previews (destination planning only, never navigation). Its contract, policy and limits are in
[`ROUTING_API.md`](ROUTING_API.md).

### Route preview API on Railway

`api/` deploys the same API as the public service `atlas-api` in `maply-atlas`, next to `atlas-db`
(which it never changes). It reads the atlas with the read-only login `atlas_api` over the private
network, and Railway serves it over HTTPS on a `*.up.railway.app` domain.

```bash
api/provision.sh            # first deploy and every redeploy; safe to re-run
api/provision.sh --rotate   # new API password, then redeploy
api/check.sh https://<domain>
```

`provision.sh` keeps the API password in `atlas/.secrets/atlas_api_password` (private,
gitignored, never printed). It creates or updates the role through the owner URL in `atlas/.env`,
creates the service in `eu-west` (the database's region), sets `ATLAS_API_DATABASE_URL` through
stdin, adds a domain, uploads only what `api/stage.sh` stages, waits for readiness and runs
`check.sh`. To check the login only: `uv run python -m atlas api-role --password-file
.secrets/atlas_api_password --check-only`.

On the free plan the service sleeps after 10 idle minutes (`sleepApplication` in
`api/railway.toml`). The first request after that wakes the database and reads a full snapshot,
which can take 30–90 s. Limits and safeguards are in [ROUTING_API.md](ROUTING_API.md#public-deployment-atlasapi).

## Known limits

- The route-preview API derives its own side-aware graph from the underlying facts. It does
  not use the legacy grades or costs below: missing attributes distort the signal averages,
  and choosing the better side does not establish a continuous footpath. See
  [ROUTING_API.md](ROUTING_API.md) for the implemented policy and deployment proposal.

- Crossings are not modelled as edges yet. Routes may switch between footpath
  sides wherever two streets meet, and connectors carry no kerb-ramp data.
- About 7% of edges sit in small islands more than 20 m from the main network,
  such as isolated plazas, park paths and lanes behind major roads.
- Grades aggregate all observations equally. Once community data arrives, weight
  recent observations above old imagery.

## Tests

The tests need a disposable PostGIS and pgRouting database whose name contains `test`. The production
image works:

```bash
docker build --platform linux/amd64 -t maply-atlas-db:local database
docker run -d --name maply-atlas-test --platform linux/amd64 -e POSTGRES_USER=atlas \
  -e POSTGRES_PASSWORD=localonly -e POSTGRES_DB=atlas_test -p 127.0.0.1:5442:5432 maply-atlas-db:local
ATLAS_TEST_DATABASE_URL=postgresql://atlas:localonly@127.0.0.1:5442/atlas_test uv run pytest
```
