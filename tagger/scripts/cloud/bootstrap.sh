#!/usr/bin/env bash
# Ubuntu 24.04 x86_64. First install packages, then configure after restore.
set -euo pipefail
stage=${1:-configure}
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || {
  echo 'This installer requires Ubuntu 24.04 x86_64.' >&2; exit 1;
}
if [[ $stage == packages ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y docker.io docker-compose-v2 python3-venv curl ca-certificates \
    xvfb openbox x11vnc xdotool x11-utils xclip scrot python3-tk fonts-liberation dbus-x11
  chrome_deb=$(mktemp --suffix=.deb)
  trap 'rm -f "$chrome_deb"' EXIT
  curl -fL --retry 3 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o "$chrome_deb"
  apt-get install -y "$chrome_deb"
  systemctl enable --now docker
  # A single Chrome session and image builds can exceed 4 GB briefly.
  if [[ ! -f /swapfile ]] && [[ $(swapon --show --noheadings | wc -l) -eq 0 ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  exit 0
fi
[[ $stage == configure ]] || { echo 'Usage: bootstrap.sh packages|configure' >&2; exit 1; }
[[ -f /opt/maply/data/server-transfer-receipt.json ]] || {
  echo 'Restore and verify the backup into /opt/maply first.' >&2; exit 1;
}
id maply >/dev/null 2>&1 || useradd --create-home --shell /bin/bash maply
chown -R maply:maply /opt/maply
runuser -u maply -- python3 -m venv /opt/maply/backend/.venv
runuser -u maply -- /opt/maply/backend/.venv/bin/pip install -e '/opt/maply/backend[capture]'
install -m 755 /opt/maply/scripts/cloud/desktop.sh /usr/local/bin/maply-desktop
install -m 644 /opt/maply/scripts/cloud/maply-desktop.service /etc/systemd/system/
install -m 644 /opt/maply/scripts/cloud/maply-capture@.service /etc/systemd/system/
install -m 644 /opt/maply/scripts/cloud/maply-capture.service /etc/systemd/system/
install -m 644 /opt/maply/scripts/cloud/maply-app.service /etc/systemd/system/
systemctl daemon-reload
if ! grep -q '^CAPTURE_MANAGED=' /opt/maply/.env; then
  echo 'CAPTURE_MANAGED=true' >> /opt/maply/.env
fi
cd /opt/maply
docker compose up -d app
systemctl enable --now maply-app maply-desktop maply-capture
echo 'Ready. App: loopback port 8000. Desktop: loopback port 5900. Access both through SSH.'
echo 'Create or resume runs in Maply to capture on the cloud desktop.'
