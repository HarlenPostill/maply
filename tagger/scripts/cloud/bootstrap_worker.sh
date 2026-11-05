#!/usr/bin/env bash
# Provision an empty Ubuntu capture VM. No app, database, data restore or paid work.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]]
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv curl ca-certificates xvfb openbox x11vnc \
  xdotool x11-utils xclip scrot python3-tk fonts-liberation dbus-x11 ufw
if ! command -v google-chrome-stable >/dev/null; then
  chrome_deb=$(mktemp --suffix=.deb)
  trap 'rm -f "$chrome_deb"' EXIT
  curl -fL --retry 3 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o "$chrome_deb"
  apt-get install -y "$chrome_deb"
fi
id maply >/dev/null 2>&1 || useradd --create-home --shell /bin/bash maply
chown -R maply:maply /opt/maply
[[ -x /opt/maply/backend/.venv/bin/python ]] || runuser -u maply -- python3 -m venv /opt/maply/backend/.venv
runuser -u maply -- /opt/maply/backend/.venv/bin/pip install -e '/opt/maply/backend[capture]'
install -m 755 /opt/maply/scripts/cloud/desktop.sh /usr/local/bin/maply-desktop
install -m 644 /opt/maply/scripts/cloud/maply-desktop.service /etc/systemd/system/
ufw allow OpenSSH
ufw --force enable
systemctl daemon-reload
systemctl enable --now maply-desktop
echo 'Capture desktop ready; capture starts only through the OVH supervisor.'
