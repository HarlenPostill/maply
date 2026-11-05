# Maply cloud deployment

This is historical infrastructure documentation. Maply now runs from the local
development checkout with `./scripts/dev.sh`. The user is retiring the cloud
workers and VPS. None of these services are required for local use. Do not
provision, deploy, or restore cloud services during ordinary local development.

Target: Ubuntu 24.04 x86_64 VPS, initially 2 vCPU / 4 GB RAM / 40 GB disk.
One persistent X11 Chrome desktop, app and PostGIS run on the same VM. OpenRouter
inference stays remote. Validate actual Street View rendering before enabling
paid capture. No GPU server is assumed.

The user explicitly selected the laptop snapshot as the migration source. Keep
the original backup and old server intact. Never copy private keys to the VM.

## Prepare on the laptop

```bash
python3 scripts/prepare_cloud_transfer.py \
  --bundle data/transfers/maply-server-transfer-20261025 \
  --output data/transfers/maply-cloud-transfer
```

This verifies the old archives and packages current source. The bundle contains
the saved provider key and must travel only over SSH. It does not start the
laptop app/database. SSH must already work with a key and sudo access on the VM.

```bash
ssh VM 'umask 077; mkdir -p ~/maply-transfer'
scp data/transfers/maply-cloud-transfer/* VM:maply-transfer/
ssh -t VM 'sudo bash ~/maply-transfer/bootstrap.sh packages'
ssh -t VM 'sudo python3 ~/maply-transfer/server_transfer.py restore --bundle ~/maply-transfer --project /opt/maply'
ssh -t VM 'sudo bash /opt/maply/scripts/cloud/bootstrap.sh configure'
```

Restore refuses a nonempty destination, checks every archive and image against
its manifest, verifies database inventory, revokes stale leases, pauses capture
runs and disables paid background tagging. Do not run a second restore over a
successful one. Both app and database remain bound to loopback.

## Connect and verify

The laptop's port 8000 already belongs to the old Mac tunnel. Use port 18000 for
cloud review until the client tunnel is intentionally migrated.

```bash
ssh -N -L 18000:127.0.0.1:8000 -L 15900:127.0.0.1:5900 VM
```

Open <http://127.0.0.1:18000>. Open `vnc://127.0.0.1:15900` in macOS Screen Sharing
to view Chrome. VNC has no public listener and is accessible only via the SSH
tunnel (local VM users can also reach it). Do not forward VNC or the app to a
public interface. Disconnecting the viewer does not stop the desktop.

Check job/image counts and sample photos against the transfer manifest. In the
remote Chrome desktop, finish any first-run/consent UI and verify Street View.
The screenshot preflight requires a run UUID syntactically but makes no model
calls and does not claim work:

```bash
ssh -t VM 'sudo -u maply env DISPLAY=:99 XAUTHORITY=/home/maply/.Xauthority /opt/maply/backend/.venv/bin/python /opt/maply/scripts/capture_worker.py --run 00000000-0000-0000-0000-000000000000 --check'
```

After verifying the restore, enable background tagging if wanted:

```bash
ssh -t VM 'sudo python3 /opt/maply/scripts/server_transfer.py activate --project /opt/maply'
```

Create/resume a run in Maply. The cloud supervisor starts its workers automatically
and handles one running run at a time across all enrolled desktops. Pause/stop releases the desktops to another
queued run. Keep this desktop dedicated to capture while it runs. After a service
restart or server reboot, existing running runs are paused and require an explicit
resume in Maply. A failed worker pauses its run instead of restarting paid calls.
Runs created with **YOLO mode** are the exception: worker failures, service
restarts and model stop requests are recorded on the run and it keeps going, until
its photo, request, cost or duration goal is reached or you pause/stop it.

```bash
ssh -t VM 'sudo journalctl -u maply-capture -f'
ssh -t VM 'sudo systemctl stop maply-capture'
```

The app and Chrome desktop start automatically after VM reboot. The desktop is
1600×1200; keep the Chrome window at its initial size. Every input is checked
against the browser identity, focus, geometry and URL. To inspect a stopped
worker, use `systemctl status` and `journalctl`; restarting the desktop changes
its window identity, so inspect it before resuming a paused run. The optional
`maply-capture@RUN_UUID` unit is for manual diagnosis with the supervisor stopped.
The Linux worker focuses Chrome's address bar directly because Maps can retain
keyboard focus through the address shortcut. It leaves the address bar by clicking
the empty title bar; Escape can reach Maps and close Street View. It reuses its
selected tab on resume, retries a missed address-copy
shortcut up to three times, and waits for black loading frames to clear before
any model call. A black frame causes the planned shot to be reopened; after three
black loads, the worker marks that shot `no_imagery` without a model call and
continues the run. Temporary Google home-page redirects and unresolved camera
addresses retry with backoff capped at 30 seconds instead of pausing. Replay-safe
status and heartbeat requests also retry through connection failures and server
5xx responses. Unexpected pages, focus/permission failures and uncertain paid or
completion request outcomes still pause rather than risk unsafe input or duplicate
work. Repeated clipboard failure reports a cloud browser error rather than macOS
permission instructions. Chrome may initially copy the typed Maps request rather
than its resolved camera URL. The Linux adapter rereads it and refuses model
assessment until the observed camera address is available. Camera validation limits
remain unchanged. Normal worker shutdown after a pause/stop preserves the original
run reason; conditional failure pauses cannot overwrite an existing server or user
pause.

Provider daily snapshots supplement the laptop migration backup. Before doing
new substantial capture, set up a recurring off-VM database/image backup; the
initial transfer bundle will not include new cloud progress.

## Public HTTPS address

The public address is `https://maply.hrln-interactive.com`. Vercel hosts its DNS;
the exact `maply` A record points to `15.235.245.196`. It is not a Vercel deployment
and does not require a wildcard record. Remove conflicting A/AAAA/CNAME records
for that exact name. Caddy obtains and renews a certificate after DNS resolves
to the VPS. Its official package is installed with `install_https.sh`.

`Caddyfile` is the tracked template for `/etc/caddy/Caddyfile`. The systemd
drop-in `/etc/systemd/system/caddy.service.d/maply.conf` loads the private file
`/etc/caddy/maply.env`, which defines `MAPLY_PUBLIC_HOST`, `MAPLY_PUBLIC_USER`
and the bcrypt `MAPLY_PASSWORD_HASH`. The username is `harlen`. Never commit
passwords or password hashes. Preserve `/var/lib/caddy` for certificate storage.

All public routes, APIs and images require authentication. Cross-origin browser
mutations are rejected, and login credentials are stripped before proxying to
the loopback backend. Port 80 redirects to HTTPS. Backend/database/VNC ports
remain private; host workers continue to use loopback directly.

After changing gateway settings, validate with the environment loaded before
restarting Caddy. Check `sudo journalctl -u caddy` for certificate/DNS errors.
The existing app and desktop SSH tunnels remain available as private access.

## Deploy the accessibility atlas during capture

From the client checkout, run:

```bash
scripts/cloud/deploy_accessibility.sh
```

The script copies only the atlas source files, rebuilds the app image, and copies
its static assets into the running container. It publishes the HTML entry point
last and retains previous hashed assets for open capture pages. It does not
restart the app, capture supervisor, desktop, or database. The rebuilt image
contains the atlas for the next normal app restart.

The `maply-accessibility.timer` unit runs a read-only snapshot publisher 30 seconds
after the previous publish finishes. The publisher uses a repeatable-read,
read-only database transaction and atomically replaces each job's JSON snapshot.
It never recovers queues, changes leases, or calls a model. Snapshots use the
existing authenticated `/assets/accessibility/<job-id>.json` route, including
through the private SSH tunnel. No new listener or gateway rule is required.

Check publication and the running services:

```bash
ssh maply-cloud 'sudo systemctl status maply-accessibility.timer --no-pager'
ssh maply-cloud 'sudo journalctl -u maply-accessibility.service -n 20 --no-pager'
ssh maply-cloud 'sudo systemctl is-active maply-capture maply-desktop'
```

In Maply, open **Accessibility atlas**. The browser checks for a new snapshot
every 30 seconds while the page is visible. **Pause** stops refresh in that
browser, not capture. A failed refresh retains the previous view. A snapshot
older than two minutes displays **Snapshot delayed**. After a normal container
replacement, the timer regenerates snapshots automatically.

For rollback, use the previous `index.html` in the deployment directory printed
by the script. Copy it into the app container as `index.next.html`, then rename it
to `index.html`. Old assets remain available. To stop only snapshot publication,
run `sudo systemctl disable --now maply-accessibility.timer` on the VPS.

## Verify the atlas

Run the aggregation tests without a database:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_accessibility.py -q
```

For UI verification, export a snapshot with `python -m app.accessibility` on the
server and copy the resulting JSON file to the client. Use the existing SSH
tunnel for the read-only API requests:

```bash
npm --prefix frontend run build
MAPLY_SNAPSHOT=/absolute/path/to/job.json npm --prefix frontend run test:visualiser
```

The browser test serves the built frontend on an ephemeral loopback port. It
proxies GET requests only, and never starts a local Maply app or database. Its
refresh-failure and new-data checks modify the in-memory test snapshot only.
Screenshots are written to `/tmp/maply-visualiser-test`.

The atlas snapshot includes every current tagged photo in `points`, with its
observed camera coordinates or a planned-position fallback. The default 3D mode
renders one column per photo. Coincident cameras can be separated visually by up
to eight metres; stored coordinates remain unchanged. Segment summaries remain
available as a separate rendering mode.

Footpath quality uses visible footpath-section defects, rough surfaces, and
interruptions. A no-defect category requires an explicit clear no-defect tag for
every relevant section. Surface material alone does not establish quality.
Missing footpaths and unknown quality are excluded from the quality denominator.
The geographic heatmap groups photos into 50, 100, or 200 metre cells and exposes
each cell's source photos. Counts include overlapping images, not unique defects.

## Temporary capture fleet

OVH remains the only app, database and image store. Temporary Ubuntu 24.04 VMs
run Chrome/Xvfb only. No data bundles, database volumes, provider keys or laptop
private keys are copied to them. Their firewall allows SSH; VNC is loopback-only.

The coordinator owns `/home/maply/.ssh/capture_ed25519`. Generate it on OVH as
`maply` if absent; never transfer its private half. Provision a purchased worker
from this checkout using:

```bash
python3 scripts/cloud/provision_worker.py WORKER_IPV4
```

The script installs source and dependencies, enrolls the coordinator's public key,
and pins the worker host key obtained over the admin SSH connection. Use
`--skip-packages` for source/key updates on an already installed worker.
The enrolled key forces `remote_worker.py` and accepts only a run UUID. SSH permits
only remote forwarding to `127.0.0.1:18000`; it cannot run a shell or forward to
other services. This forwards the coordinator's loopback API into the worker.

Preflight a new desktop before enrollment (substitute its IPv4 address):

```bash
ssh -i ~/.ssh/maply_cloud_ed25519 root@WORKER_IPV4 \
  'sudo -u maply env DISPLAY=:99 XAUTHORITY=/home/maply/.Xauthority /opt/maply/backend/.venv/bin/python /opt/maply/scripts/capture_worker.py --run 00000000-0000-0000-0000-000000000000 --check'
```

On OVH, `/etc/maply/capture-fleet.json` is a JSON array of up to five worker IPv4
addresses. The supervisor rereads it every three seconds. Atomically replacing
this file adds/removes workers without restarting active capture. An empty array
returns to OVH-only capture. All desktops claim different shots from the same run;
photo/request/spend/duration controls remain shared. Reported spend can exceed
the cost threshold by the cost of requests already in flight. Request reservations
remain bounded. `max_photos` counts finished outcomes, including no-imagery, rather
than guaranteeing that many saved usable photos. Set `max_saved_photos` (Saved
photo target in the UI) to count saved photos outside the review queue. Zero
disables this additional goal for existing runs. The coordinator reserves the
remaining saved-photo slots across in-flight leases so concurrent workers cannot
overshoot this target. Set the handled-photo limit high enough to allow reviews
and no-imagery outcomes; request, reported-cost, duration and queue exhaustion
can still end the run before the saved-photo target.

The SSH stdin pipe ties remote worker lifetime to the coordinator connection.
Disconnecting or removing a worker terminates its process; unfinished leases expire
and can be reclaimed. Worker failures use existing pause/YOLO semantics and a
30-second supervisor retry delay. On service restart, ordinary runs still require
UI resume; YOLO runs continue. A VM does not start capture independently on boot.

Each capture worker runs under an independent process watchdog, on OVH and both
slots of every temporary VM. A worker has 300 seconds to complete a photo or an
idle status check, including startup, navigation retries and model calls. Only a
completed cycle renews this deadline; log output, lease heartbeats and retries do
not. On timeout, the watchdog sends SIGTERM, allows 10 seconds for shutdown, then
kills the worker process group if necessary to release the desktop lock. Chrome
and the other capture workers stay running. The supervisor records the failure
and restarts that slot after its existing 30-second delay for a running YOLO run.
An ordinary run pauses and requires **Resume** in Maply. Unfinished reservations
expire through the existing lease mechanism; the watchdog never replays a paid
request. A stuck desktop or network that survives a worker reset can cause further
timeouts. Watchdog activation and resets appear in `journalctl -u maply-capture`.

`scripts/capture_watchdog.py` must be deployed alongside `capture_worker.py`.
Existing processes pick up the watchdog when they next start. The process-level
checks require no database, desktop or paid calls:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_capture_watchdog.py backend/tests/test_remote_worker.py backend/tests/test_cloud_supervisor.py backend/tests/test_capture_linux.py -q
```

To retire workers: remove their addresses from the fleet file, verify their worker
processes have exited and their results are present on OVH, then **destroy** their
DigitalOcean Droplets. Powering off a bundled Droplet does not stop its billing.
Remove retired host entries from the coordinator's known_hosts. No capture data
needs restoring from these disposable workers.

Fleet regression checks use an isolated PostGIS database only:

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://maply:maply@127.0.0.1:15439/maply_fleet_test \
  .venv/bin/python -m pytest tests/test_capture.py tests/test_cloud_supervisor.py tests/test_remote_worker.py tests/test_capture_linux.py -q
```

Run `npm --prefix frontend run test:atlas-data` to check heatmap count conservation
and unknown-value handling. The UI test also checks the WebGL instance count
against the full snapshot photo count and opens evidence from a heatmap cell.


### Two desktops per temporary VM

For a provisioned capture-only VM, deploy `desktop.sh`, `remote_worker.py` and
`add_second_worker.sh` into `/opt/maply/scripts/cloud`, then run the latter as
root. It creates `maply2` with its own home/profile, display `:100`, loopback VNC
port 5901 and restricted reverse API tunnel on port 18001. The original `maply`
desktop stays on `:99`, VNC 5900 and API tunnel 18000. Each user retains its own
capture lock; never run two workers as the same user or on the same display.

The coordinator inventory accepts original IPv4 strings (slot 1) and objects
such as `{"host": "159.203.8.177", "slot": 2}` for slot 2. Enroll both entries to
run both desktops. At most two slots per host and five hosts are accepted.
The supervisor assigns all slots to the same active run, sharing its database
claims, saved-photo goal and reported AI budget. In-flight calls can still exceed
the budget slightly. Remove a slot entry to stop that worker; disable its
`maply-desktop-second` service afterward to release browser memory. Destroy the
VM in DigitalOcean when retiring it to stop VM billing.


Capture assessment requests use a synchronous FastAPI route so blocking database
row locks run in the request thread pool, not the main API event loop. The
provider coroutine has a private event loop in that request thread. Keep this
boundary when changing assessment code: putting synchronous lock acquisition
back on the main event loop can stall every worker and the pause/settings UI.
`backend/tests/test_capture_responsiveness.py` exercises this contention case.

### Sleep temporary VMs after a selected run

`sleep_after_run.py` runs as root on OVH from the one-shot
`maply-sleep-after-run.timer` (once per minute). Its private state is
`/var/lib/maply/sleep-after-run.json`: `run_id`, a fixed `hosts` list and a
`done` list. It requires the selected run to be completed and no other run to
be running. It removes those hosts from the fleet, then uses the coordinator
key with a restricted root authorization on each VM to schedule power-off one
minute later. That authorization permits only `/sbin/shutdown -h +1`, from the
OVH address. Successful hosts are recorded so retries target only failures.
The timer disables itself after all shutdown requests succeed. `--check` reports
eligibility without changing anything. Capture and the main OVH server stay up.
This is armed for a selected run, not automatically for future runs. To reuse,
power on the VMs, restore their fleet entries, update the run ID, clear `done`,
and enable/start the timer. Power-off does **not** stop DigitalOcean billing.
