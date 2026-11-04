#!/bin/zsh
# Run the local API with Python reload and the frontend with Vite hot reload.
set -eu
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build app atlas
if [[ ! -d frontend/node_modules ]]; then
  npm --prefix frontend ci
fi
echo 'Local app/API: http://127.0.0.1:8000'
echo 'Frontend development: http://127.0.0.1:5173'
exec npm --prefix frontend run dev
