#!/usr/bin/env bash
# Deploy only the fleet source and saved-photo target; preserve cloud state.
set -euo pipefail
cd "$(dirname "$0")/../.."
archive=$(mktemp /tmp/maply-fleet.XXXXXX)
remote_archive="/tmp/$(basename "$archive").tar"
trap 'rm -f "$archive"' EXIT
COPYFILE_DISABLE=1 tar -cf "$archive" \
  backend/app/capture.py backend/tests/test_capture.py \
  backend/tests/test_cloud_supervisor.py backend/tests/test_remote_worker.py \
  frontend/src/CapturePanel.tsx scripts/cloud/supervisor.py \
  scripts/cloud/remote_worker.py scripts/cloud/bootstrap_worker.sh \
  scripts/cloud/provision_worker.py scripts/cloud/deploy_fleet.sh scripts/cloud/README.md
scp "$archive" "maply-cloud:$remote_archive"
ssh maply-cloud "sudo bash -s -- '$remote_archive'" <<'REMOTE'
set -euo pipefail
exec 9>/run/lock/maply-fleet-deploy.lock
flock -n 9
cd /opt/maply
release=$(date -u +%Y%m%dT%H%M%SZ)
backup="/opt/maply/data/deployments/fleet-$release"
mkdir -p "$backup"
tar -cf "$backup/source.tar" backend/app/capture.py frontend/src/CapturePanel.tsx scripts/cloud/supervisor.py
docker image tag maply-app:local "maply-app:before-fleet-$release"
tar --no-same-owner -xf "$1" -C /opt/maply
rm -f "$1"
echo "Building app; backup is $backup"
docker compose build app > "$backup/build.log" 2>&1
echo 'Build passed. Stopping workers before replacing the API.'
systemctl stop maply-capture
docker compose up -d --no-deps app
ready=false
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:8000/api/capture/runs > /dev/null; then ready=true; break; fi
  sleep 2
done
[[ $ready == true ]] || { echo 'API did not become ready; workers remain stopped.' >&2; exit 1; }
systemctl start maply-capture
if systemctl is-enabled --quiet maply-accessibility.timer; then
  systemctl start maply-accessibility.service
fi
echo 'Fleet deployed. Ordinary runs require UI resume; YOLO runs continue.'
REMOTE
