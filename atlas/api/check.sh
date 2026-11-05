#!/bin/bash
# Post-deploy checks for the public route preview API. Read-only: health, coverage and one
# route-preview request, and a probe that the per-client limiter sees the address set by
# Railway's edge, not one the client supplies.
#
#   atlas/api/check.sh https://<service>.up.railway.app
#
# The bounded burst verifies the deployed shared bucket despite rotating client headers.
# It does not infer how Railway rewrites headers.
set -euo pipefail
base="${1:?usage: check.sh https://host}"
case "$base" in https://*|http://127.0.0.1:*) ;; *) echo "Use the HTTPS address." >&2; exit 1;; esac

fail=0
health="$(curl -fsS --max-time 90 "$base/v1/health")" || { echo "health: not ready"; exit 1; }
echo "health: $health"
curl -fsS --max-time 30 -o /dev/null -w 'coverage: %{http_code} %{size_download} bytes %{time_total}s\n' "$base/v1/coverage"
headers="$(curl -sS --max-time 30 -D - -o /dev/null "$base/v1/health")"
grep -qi '^cache-control: no-store' <<<"$headers" || { echo "missing Cache-Control: no-store"; fail=1; }
grep -qi '^server: uvicorn' <<<"$headers" && { echo "server header exposed"; fail=1; }
# Two points on George Street outside Town Hall: any typed answer proves the route path works.
code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' -H 'content-type: application/json' \
  --data '{"origin":{"lat":-33.8731,"lng":151.2069},"destination":{"lat":-33.8717,"lng":151.2073}}' "$base/v1/route-preview")"
echo "route-preview: $code"
case "$code" in 200|404|422) ;; *) fail=1;; esac
for path in /docs /openapi.json; do
  [ "$(curl -sS -o /dev/null -w '%{http_code}' "$base$path")" = 404 ] || { echo "$path is exposed"; fail=1; }
done

python3 - "$base" <<'PYPROBE'
import concurrent.futures, json, subprocess, sys, time
base = sys.argv[1]
def request(i):
    result = subprocess.run(['curl', '-sS', '--max-time', '20', '-w', '\n%{http_code}',
        '-H', f'X-Real-IP: 198.51.100.{i}', '-H', f'X-Forwarded-For: 198.51.100.{i}',
        base + '/v1/health'], check=True, capture_output=True, text=True)
    body, status = result.stdout.rsplit('\n', 1)
    return int(status), json.loads(body).get('error', {}).get('code')
started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(request, range(1, 49)))
elapsed = time.monotonic() - started
if any(code not in (200, 429) or (code == 429 and error != 'rate_limited') for code, error in results):
    raise SystemExit('rate-limit check: unexpected response')
limited = sum(code == 429 for code, _ in results)
if not limited:
    raise SystemExit(f'rate-limit check inconclusive: no limits in {elapsed:.1f}s; inspect configuration')
print(f'shared rate limit: {limited} of 48 bounded requests limited despite rotating headers')
PYPROBE
exit "$fail"
