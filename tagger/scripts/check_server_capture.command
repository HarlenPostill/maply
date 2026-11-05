#!/bin/zsh
# Run from Terminal on the server Mac so macOS associates permissions with Terminal.
set -u
set -o pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$project_dir/data/server-logs"
cd "$project_dir/backend"
echo 'Checking Chrome controls and screen capture. This makes no model requests.'
echo 'Allow macOS permissions when prompted; then rerun this check if needed.'
open -a '/Applications/Google Chrome.app' 'https://www.google.com/maps'
sleep 4
uv run --extra capture python ../scripts/capture_worker.py --run 00000000-0000-0000-0000-000000000000 --check 2>&1 | tee "$project_dir/data/server-logs/capture-check.log"
result=$?
echo "Check finished (exit $result)."
read 'reply?Press Return to close this window. '
exit "$result"
