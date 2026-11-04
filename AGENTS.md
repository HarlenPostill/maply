# Maply local development

This checkout, `/Users/harlenpostill/.t3/projects/maply`, is the active app and
source workspace. The user has returned Maply to full local development and is
retiring the temporary workers and final VPS. Work locally unless explicitly
asked to work on a remote server.

## Run the app

- `./scripts/dev.sh` builds and starts local PostGIS and the backend with Python
  reload, then runs Vite at `http://127.0.0.1:5173` with frontend hot reload.
- The local API and built app use `http://127.0.0.1:8000`. This is a Docker
  listener, never an SSH tunnel. Extension and desktop capture requests go here.
- `docker compose up -d --build` runs the built app without development reload.
- `docker compose stop` stops the local containers. Docker restarts running
  containers when its engine restarts.
- The root `.env` selects Compose project `maply-local-final` so it retains the
  verified database volume `maply-local-final_pgdata`. Preserve this setting.
- PostGIS binds to `127.0.0.1:5433`. The app binds to loopback only. Host backend
  settings in `backend/.env` point at this database and this checkout's `data/`.
- Automatic paid tagging is disabled. Capture uses a local Chrome desktop and
  requires an explicit run and worker. No cloud supervisor is required.
- The primary UI reviews this completed job. The separate review worker only
  processes explicitly started batches. The user requested automatic acceptance
  for Review next batches: save every valid result, including unknown as unknown.
  Single-photo reviews retain manual confirmation. Record automatic acceptance
  honestly in provenance. Do not start paid review batches for implementation testing.
  Restart pauses unfinished batches; tests use stubbed provider responses.
- The local `atlas` Compose service publishes read-only accessibility snapshots
  into `data/accessibility/` every 30 seconds. Both local ports serve them.

## Data and backups

The current dataset lives in this checkout's `data/jobs/`. It includes 52,483
saved photos and 52,457 annotations from the completed cloud runs. The full
photo SHA256, thumbnail, and annotation check passed after the local cutover.
Run `python3 scripts/verify_local_copy.py .` to verify current database references.
Its receipt is `data/local-verification.json`.

The user explicitly requested one source of truth and removal of the old backup
sets on 4 November 2026. The earlier cloud snapshots, restored workspace archive,
cutover backup, transfer bundles, deployment archives, and historical local
`maply_pgdata` volume have been removed after a fresh complete verification.
`data/backup-cleanup-receipt.json` records this cleanup. The active volume remains
`maply-local-final_pgdata`. Do not recreate partial backups or extra dataset
copies. Create a fresh backup from this active workspace only when requested.

`data/local-server.json` records the active workspace and database volume.
Never replace this dataset with old remote data. Never delete the active volume
with `docker compose down -v` or run tests against the real `maply` database.
Tests must use an isolated test database and temporary data directories.

## Retired infrastructure

The cloud tunnel and completed cloud tagging LaunchAgents are disabled and their
plists have been removed from `~/Library/LaunchAgents`. The legacy tunnel helpers
require an explicit config and reject local ports 8000 and 5173. Do not recreate
cloud forwarding or deploy to the old VPS as part of ordinary development.

`scripts/cloud/` retains historical provisioning and transfer tools. Cloud DNS,
Caddy, desktop profiles, fleet settings, and server credentials are not local
runtime dependencies. Preserve private keys and passwords in their existing
private files. Never commit data, secrets, or transfer bundles. Map tiles,
Street View, OpenStreetMap downloads, and OpenRouter inference still use their
external services; saved photos, annotations, and the database are all local.
