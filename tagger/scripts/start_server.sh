#!/bin/zsh
# Run only on the permanent server Mac. A LaunchAgent starts this after login.
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    colima start
  else
    echo 'Start Docker Desktop, then run this command again.' >&2
    exit 1
  fi
fi
cd "$project_dir"
docker compose up -d
# Keep the dedicated server awake while this login session is active.
exec /usr/bin/caffeinate -i
