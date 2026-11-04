#!/usr/bin/env bash
set -euo pipefail
export DISPLAY=${DISPLAY:-:99}
export XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority}
touch "$XAUTHORITY"
children=()
cleanup() {
  trap - EXIT TERM INT
  kill "${children[@]}" 2>/dev/null || true
  wait || true
}
trap cleanup EXIT TERM INT
# X11 TCP is disabled. VNC binds loopback and is reachable only over SSH.
Xvfb "$DISPLAY" -screen 0 1600x1200x24 -nolisten tcp -auth "$XAUTHORITY" &
children+=("$!")
ready=false
for attempt in {1..50}; do
  if xdpyinfo >/dev/null 2>&1; then ready=true; break; fi
  sleep .2
done
[[ $ready == true ]] || { echo 'X11 display failed to start.' >&2; exit 1; }
openbox &
children+=("$!")
for attempt in {1..50}; do
  if xprop -root _NET_SUPPORTING_WM_CHECK | grep -q 'window id'; then break; fi
  sleep .2
done
x11vnc -display "$DISPLAY" -localhost -rfbport "${MAPLY_VNC_PORT:-5900}" -forever -shared -nopw -noxdamage &
children+=("$!")
google-chrome-stable --no-first-run --no-default-browser-check --hide-crash-restore-bubble \
  --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader \
  --password-store=basic --disable-dev-shm-usage --window-position=0,0 \
  --window-size=1500,1100 --user-data-dir="$HOME/chrome-profile" \
  'https://www.google.com/maps/@-33.8688,151.2093,14z' &
children+=("$!")
# Any failed component restarts the whole desktop; capture never restarts itself.
wait -n "${children[@]}"
exit 1
