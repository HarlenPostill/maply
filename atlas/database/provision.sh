#!/bin/bash
# Create (or reuse) the Railway project and database, then load the atlas.
# Safe to re-run: existing project, service, volume and password are reused.
#
#   atlas/database/provision.sh
#
# Requires: railway CLI logged in, a Railway plan on the workspace, uv, and the
# local tagger database running (cd tagger && docker compose up -d).
set -euo pipefail
cd "$(dirname "$0")/.."   # atlas/

WORKSPACE="${RAILWAY_WORKSPACE:-Harlen's Projects}"
PROJECT="${RAILWAY_PROJECT:-maply-atlas}"
SERVICE=atlas-db

if ! railway status --json >/dev/null 2>&1; then
  railway init --name "$PROJECT" --workspace "$WORKSPACE" --json
fi
vars() { railway variables --service "$SERVICE" --json 2>/dev/null || echo '{}'; }
var() { vars | python3 -c "import json,sys; print(json.load(sys.stdin).get('$1') or '')"; }

if ! railway service link "$SERVICE" >/dev/null 2>&1; then
  railway add --service "$SERVICE" --json
  railway service link "$SERVICE"
fi
if ! railway volume list 2>/dev/null | grep -q 'Mount path: /var/lib/postgresql/data'; then
  railway volume add --mount-path /var/lib/postgresql/data --json
fi

if [ -z "$(var POSTGRES_PASSWORD)" ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' \
    | railway variable set POSTGRES_PASSWORD --stdin --service "$SERVICE" --skip-deploys
fi
railway variable set --service "$SERVICE" --skip-deploys \
  POSTGRES_USER=atlas POSTGRES_DB=atlas \
  'DATABASE_URL=postgresql://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{POSTGRES_DB}}' \
  'DATABASE_PUBLIC_URL=postgresql://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}/${{POSTGRES_DB}}?sslmode=require'

railway up ./database --path-as-root --service "$SERVICE" --detach

if [ -z "$(var RAILWAY_TCP_PROXY_DOMAIN)" ]; then
  echo
  echo "One manual step: Railway dashboard -> $PROJECT -> $SERVICE -> Settings -> Networking"
  echo "-> TCP Proxy -> port 5432. Waiting for the proxy address..."
  until [ -n "$(var RAILWAY_TCP_PROXY_DOMAIN)" ]; do sleep 10; done
fi

url="$(var DATABASE_PUBLIC_URL)"
umask 077
printf 'ATLAS_DATABASE_URL=%s\n' "$url" > .env
echo "Wrote atlas/.env (private, gitignored)."

echo "Waiting for the database to accept connections..."
until uv run python -c "import psycopg; psycopg.connect('$url', connect_timeout=5).close()" 2>/dev/null; do sleep 10; done

uv run python -m atlas publish
uv run python -m atlas verify
