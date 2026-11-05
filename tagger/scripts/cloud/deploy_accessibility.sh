#!/usr/bin/env bash
# Build the atlas and atomically publish static files without restarting capture or app.
set -euo pipefail
cd "$(dirname "$0")/../.."
archive=$(mktemp /tmp/maply-accessibility.XXXXXX)
remote_archive="/tmp/$(basename "$archive").tar"
trap 'rm -f "$archive"' EXIT
COPYFILE_DISABLE=1 tar -cf "$archive" \
  backend/app/accessibility.py backend/tests/test_accessibility.py \
  frontend/src/App.tsx frontend/src/visualiser frontend/package.json frontend/package-lock.json \
  frontend/tests/visualiser.mjs frontend/tests/atlas-data.mjs \
  scripts/cloud/maply-accessibility.service scripts/cloud/maply-accessibility.timer \
  scripts/cloud/deploy_accessibility.sh scripts/cloud/README.md
scp "$archive" "maply-cloud:$remote_archive"
ssh maply-cloud "sudo bash -s -- '$remote_archive'" <<'REMOTE'
set -euo pipefail
exec 9>/run/lock/maply-accessibility-deploy.lock
flock -n 9
cd /opt/maply
app_container=$(docker compose ps -q app)
test -n "$app_container"
started=$(docker inspect -f '{{.State.StartedAt}}' "$app_container")
release=$(date -u +%Y%m%dT%H%M%SZ)
backup="/opt/maply/data/deployments/accessibility-$release"
mkdir -p "$backup"
docker cp "$app_container:/app/frontend/dist/index.html" "$backup/index.html"
tar -cf "$backup/source.tar" frontend/src/App.tsx frontend/package.json frontend/package-lock.json
# The archive names source files explicitly: it contains no data, credentials or env files.
tar --no-same-owner -xf "$1" -C /opt/maply
rm -f "$1"
docker compose build app
scratch=$(mktemp -d /tmp/maply-accessibility-assets.XXXXXX)
build_container=$(docker create maply-app:local)
cleanup() { docker rm "$build_container" >/dev/null; rm -rf "$scratch"; }
trap cleanup EXIT
docker cp "$build_container:/app/frontend/dist/." "$scratch/"
if systemctl is-enabled --quiet maply-accessibility.timer; then
  systemctl start maply-accessibility.service
fi
docker cp backend/app/accessibility.py "$app_container:/app/backend/app/accessibility.next.py"
docker exec "$app_container" mv /app/backend/app/accessibility.next.py /app/backend/app/accessibility.py
# Keep old hashed assets for browsers that already have the capture workspace open.
docker cp "$scratch/assets/." "$app_container:/app/frontend/dist/assets/"
if systemctl is-enabled --quiet maply-accessibility.timer; then
  systemctl start maply-accessibility.service
else
  docker exec "$app_container" python -m app.accessibility --output /app/frontend/dist/assets/accessibility
fi
docker cp "$scratch/index.html" "$app_container:/app/frontend/dist/index.next.html"
docker exec "$app_container" mv /app/frontend/dist/index.next.html /app/frontend/dist/index.html
install -m 644 scripts/cloud/maply-accessibility.service scripts/cloud/maply-accessibility.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now maply-accessibility.timer
test "$started" = "$(docker inspect -f '{{.State.StartedAt}}' "$app_container")"
curl --fail --silent http://127.0.0.1:8000/ >/dev/null
printf 'Published accessibility atlas; app start time unchanged: %s\nRollback index: %s/index.html\n' "$started" "$backup"
REMOTE
