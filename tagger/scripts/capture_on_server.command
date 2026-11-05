#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir/backend"
echo 'Create or resume a capture run in Maply, then paste its run ID here.'
read 'maply_run_id?Capture run ID: '
if [[ -z "$maply_run_id" ]]; then
  echo 'No run ID supplied.'
  exit 1
fi
exec /usr/bin/caffeinate -di uv run --extra capture python ../scripts/capture_worker.py --run "$maply_run_id"
