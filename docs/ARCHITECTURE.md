# Architecture

```
 Browser (dashboard, React)  ──HTTP 127.0.0.1:8000──►  api (FastAPI)
                                                         │  REST API, built dashboard, local files,
                                                         │  synthetic mock panorama site (/mock)
                                                         ▼
                                                  PostgreSQL + PostGIS  ◄──── worker supervisor
                                                  (plans, tasks, leases,        (python -m app.workers.main)
                                                   observations, events)          │ N worker slots per job
                                                                                   ▼
                                                                    local Chromium (Playwright), one
                                                                    persistent profile per slot
```

Three services (`db`, `api`, `worker`) in `docker-compose.yml`. The worker can
also run on the host (headed Chromium) against the same database; slot
advisory locks make it safe to run both.

## Code map (`backend/app`)

| Module | Responsibility |
|---|---|
| `geo/projection.py` | WGS84 ↔ GDA2020/MGA56 (EPSG:7856), geodesic bearings (true north), side-of-street convention |
| `geo/planning.py` | Deterministic plan generation: segmentation, stable keys, sampling, view proposals, nodes, crossing approaches |
| `geo/streetview_url.py` | Documented Maps URLs builder; address-bar parser (provenance `page_url`) |
| `services/plans.py` | Persist / serialise / edit plans (sample moves, exclusions) |
| `scheduler/queue.py` | Claims (`SKIP LOCKED`), fenced leases, heartbeats, retries with backoff, expired-lease recovery, shared rate limiter |
| `scheduler/jobs.py` | Job lifecycle, permission checks against approval limits, capture/time/budget limits, completion |
| `capture/verification.py` | Frame checks (blank / uniform / unstable), policy checks (location, orientation, date, attribution, scope), association status |
| `capture/completion.py` | Transactional completion: lease check, artefact re-verification, observation insert, links, task state |
| `workers/runner.py` | Worker slot control loop, heartbeats, operator interventions, orphan handling |
| `workers/main.py` | Supervisor: dead-slot recovery, sweeps, spawning, crash backoff |
| `workers/deterministic.py` | Deterministic browser driver (documented URLs, no model) |
| `workers/openai_agent.py` | OpenAI computer-use recovery loop (application-owned) |
| `workers/manual.py` | Operator-driven capture via dashboard commands |
| `workers/static_api.py` | Optional official Street View Static API adapter |
| `workers/actions.py` | Action validation policy and execution |
| `workers/sites/*` | Site adapters (mock, Google Maps): navigation URLs, host allowlists, page-state observation |
| `providers/egress.py` | Cloud-image transmission guard + log redaction |
| `providers/openai_adapter.py` | The only OpenAI client; guarded transport; usage/cost estimates |
| `services/coverage.py` | Completeness vs verified coverage, gaps from actual camera positions, reuse stats |
| `services/export.py` | Local dataset packages, retention |
| `mock/panorama.py` | Synthetic panorama world and pages |

## Geographic model

Five separate concepts are stored separately:

1. **Requested sample position**: `samples.requested_geom`, a point on the operator's drawn route (and `original_geom` before edits).
2. **Actual camera position**: `observations.camera_geom` + `camera_position_provenance`, as reported by the source. It is never presented as the location of a kerb, ramp or obstruction.
3. **Intended footpath segment**: `segments` + `intended_side`, with an approximate offset line (`footpath_left_geom` / `footpath_right_geom`) as a planning aid only.
4. **View orientation**: planned (`planned_views`) vs observed (`heading_deg`, `pitch_deg`, `fov_deg` with provenance, or `unknown`).
5. **Association** between image and footpath: `association_status` is one of
   * `geometry_consistent`: camera offset ≤ limit and observed heading matches the plan (visibility not visually confirmed);
   * `uncertain`: camera far, heading mismatch, or reused panorama far from this sample;
   * `unresolved`: camera position or heading unknown;
   * `operator_verified`: an operator confirmed the footpath is visible.

**Segment direction and sides.** Each segment stores its geometry in a
direction (first → last vertex). `left`/`right` always mean left/right of that
stored direction. A view's side is computed from its heading relative to the
segment bearing, so labels stay correct when a worker approaches from the
other end, and for crossing approaches that look backwards.

**Stable segment IDs.** `seg_<sha1(rounded coordinates in stored direction)>`.
Re-planning identical geometry yields identical keys. Reversing a segment yields
a different key, because its sides swap.

**Distances.** All interpolation, offsets and gap measurements use EPSG:7856.
Headings are geodesic azimuths, because Street View headings are relative to true
north and MGA grid north differs by the meridian convergence (~1° in Sydney).

**Discrete panoramas.** Several samples may resolve to one panorama. When a
panorama already has an accepted capture for the same side, view kind and
heading (within tolerance), the later sample gets a `reused_same_panorama` link
instead of a new capture. The link is `uncertain` unless the camera is close to
that sample. Coverage reports distinct panoramas, shared panoramas, reuse
counts and **gaps measured from actual camera positions** projected onto each
segment side.

**Intersections.** Route endpoints, sharp turns and route crossings become
`plan_nodes` (marked `heuristic_candidate`, because no road network is used).
Each node gets separate `crossing_approach` samples, with views ahead and
toward both corners, recorded separately from street samples.

## Task state machine

```
planned ──start──► queued ──claim──► running ──complete──► captured ──finalize──► verified
                     ▲                 │  │                                  └──► needs_review
                     │   retry/backoff │  ├──► no_imagery
                     └─────────────────┘  ├──► manual_required ──operator resumes──► running
                     lease expiry ────────┘  └──► failed (retries exhausted / non-retryable)
              cancel: planned/queued/running ─► cancelled
```

* **Claim:** `FOR UPDATE SKIP LOCKED` on a running job; mints a new `lease_token` and a `task_attempts` row.
* **Heartbeat** renews the lease only if the token is current. A worker that loses its lease abandons the task at the next checkpoint.
* **Completion** row-locks the task and requires the current token in status `running`. Stale workers get `stale_rejected` and their files are moved to `orphans/`.
* **Duplicate prevention:** partial unique index `(job_id, planned_view_id) WHERE is_current`, plus idempotency key `task:view:lease_token`, plus fencing.
* **Recovery:** expired leases are requeued (counting a failure) or failed. On supervisor start, tasks held by slots whose advisory lock is free (a dead process) are recovered immediately.
* **Pause/cancel:** workers check job status at every checkpoint (before each request, each view and each model call), release their task without counting a failure, and stop claiming.
* **Limits:** captures (job and project), duration (accumulated running time), estimated model spend, approval expiry. Reaching one pauses the job with `pause_kind=limit` and a reason.
* **Rate:** a leaky bucket on `jobs.last_dispatch_at`, reserved atomically per source request, so it holds across workers and processes.

## Control loop (all drivers)

```
claim (lease) → navigate (documented URL; rate-limited) → observe page state
  → [manual states: consent / captcha / login → pause job, hand to operator]
  → [openai_agent only, if page not verified: guarded model call → validate actions → execute → observe]
  → for each planned view: reuse? else navigate on same panorama → two screenshots (stability)
      → frame checks → policy checks → atomic write (+thumb, optional crop) → record
  → completion transaction (lease + artefact + metadata verified by supervisor)
  → finalize (verified / needs_review) → limits
```

The agent never owns state. Its claim of success ("READY") only triggers an
observation, and the supervisor verifies stored artefacts itself.
