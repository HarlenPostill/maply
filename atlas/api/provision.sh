#!/bin/bash
# Create (or update) the public route preview service `atlas-api` in the `maply-atlas`
# Railway project and deploy it. Safe to re-run: the role, password, service, variables and
# domain are reused. `atlas-db`, its volume and its variables are never changed.
#
#   atlas/api/provision.sh            # provision and deploy
#   atlas/api/provision.sh --rotate   # also generate a new API password (then redeploys)
#
# Requires: railway CLI logged in and this folder linked to maply-atlas, uv, python3,
# atlas/.env holding the owner ATLAS_DATABASE_URL (written by database/provision.sh), and
# var/osm_way_designations.json. The API password is generated into
# atlas/.secrets/atlas_api_password (mode 600, gitignored) and is never printed or put on
# a command line.
set -euo pipefail
cd "$(dirname "$0")/.."   # atlas/

PROJECT="${RAILWAY_PROJECT:-maply-atlas}"
SERVICE=atlas-api
DB_SERVICE=atlas-db
ROLE=atlas_api
REGION="${ATLAS_API_REGION:-ams}"   # existing atlas-db region ID; do not add a second region
SECRET=.secrets/atlas_api_password

linked="$(railway status --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))')"
[ "$linked" = "$PROJECT" ] || { echo "Link this folder to $PROJECT first (railway link)." >&2; exit 1; }
[ -s .env ] || { echo "atlas/.env with the owner ATLAS_DATABASE_URL is required." >&2; exit 1; }

# 1. The password: generated once, kept private.
umask 077
mkdir -p .secrets && chmod 700 .secrets
if [ ! -s "$SECRET" ] || [ "${1:-}" = "--rotate" ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$SECRET.new"
  mv "$SECRET.new" "$SECRET"
  echo "Generated a new API password in atlas/$SECRET."
fi
chmod 600 "$SECRET"

# 2. The read-only login: atlas_read membership, read-only and timeout defaults on the role,
#    no direct grants. Run as the owner over the TLS proxy; prints role facts only.
uv run python -m atlas api-role --role "$ROLE" --password-file "$SECRET"

# 3. The service, in the database's region. Keep the folder linked to atlas-db as before.
services() { railway service list --json; }
if ! services | python3 -c "import json,sys; sys.exit(0 if any(s['name']=='$SERVICE' for s in json.load(sys.stdin)) else 1)"; then
  railway add --service "$SERVICE" --json >/dev/null
  railway service link "$DB_SERVICE" >/dev/null
fi
# Region aliases can resolve to a different region than the project's legacy default.
# Set all other current regions to zero so the free plan stays single-region.
regions="$(railway status --json)"
scale_args=()
while IFS= read -r region; do
  [ -z "$region" ] || scale_args+=("$region=0")
done < <(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); target=sys.argv[2]; service=sys.argv[3]
def walk(x):
    if isinstance(x,dict):
        if x.get("serviceName") == service:
            regions = (x.get("latestDeployment") or {}).get("meta", {}).get("serviceManifest", {}).get("deploy", {}).get("multiRegionConfig", {})
            for k in regions:
                if k != target: print(k)
            return
        for k,v in x.items():
            walk(v)
    elif isinstance(x,list):
        for v in x: walk(v)
walk(d)' "$regions" "$REGION" "$SERVICE")
railway service scale --service "$SERVICE" "${scale_args[@]}" "$REGION=1" >/dev/null

# 4. Variables. The database URL goes over stdin, through the private network, with TLS.
#    The variable references are resolved by Railway, so no address is copied here.
{ printf 'postgresql://%s:' "$ROLE"; cat "$SECRET"
  printf '@${{%s.RAILWAY_PRIVATE_DOMAIN}}:5432/${{%s.POSTGRES_DB}}?sslmode=require' "$DB_SERVICE" "$DB_SERVICE"; } \
  | railway variable set ATLAS_API_DATABASE_URL --stdin --service "$SERVICE" --skip-deploys >/dev/null
railway variable set PORT=8080 --service "$SERVICE" --skip-deploys >/dev/null

# 5. A Railway HTTPS domain on port 8080 (TLS terminates at Railway's edge).
domain() { railway domain list --service "$SERVICE" --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
def walk(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values(): yield from walk(v)
    elif isinstance(x, list):
        for v in x: yield from walk(v)
print(next((s.removeprefix("https://") for s in walk(d) if s.endswith(".up.railway.app")), ""))'; }
host="$(domain)"
if [ -z "$host" ]; then
  railway domain --service "$SERVICE" --port 8080 --json >/dev/null
  host="$(domain)"
fi
[ -n "$host" ] || { echo "No Railway domain found for $SERVICE." >&2; exit 1; }

# 6. Deploy exactly the staged allowlist, then wait for readiness and check the edge.
stage="$(api/stage.sh)"
trap 'rm -rf "$stage"' EXIT
railway up "$stage" --path-as-root --no-gitignore --service "$SERVICE" --ci \
  -m "atlas-api $(git rev-parse --short HEAD 2>/dev/null || echo local)"
echo "Waiting for https://$host/v1/health ..."
for _ in $(seq 1 60); do
  curl -fsS --max-time 20 "https://$host/v1/health" >/dev/null 2>&1 && break
  sleep 10
done
api/check.sh "https://$host"
echo "Deployed: https://$host  (set MAPLY_ROUTE_API_URL to this in the app build)"
