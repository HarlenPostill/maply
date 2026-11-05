#!/bin/bash
# Assemble the exact upload for the `atlas-api` Railway service from an allowlist.
#
#   atlas/api/stage.sh [DIR]      # DIR must not exist; default: a new temporary directory
#
# Prints the staged directory on stdout (the manifest goes to stderr). Deploy it with
#   railway up "$DIR" --path-as-root --no-gitignore --service atlas-api --ci
#
# Only these are staged: the API Dockerfile, railway.toml, pyproject.toml, uv.lock, the Python
# sources under src/atlas, the derived designations file and a NOTICE. Never .env, secrets,
# tests, migrations, docs, caches, the virtualenv, or tagger data. The result is checked.
set -euo pipefail
# Provisioning creates secrets with umask 077. This stage contains only public runtime
# files; its directories must be traversable by the image's non-root user.
umask 022
atlas="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:-}"
if [ -z "$dest" ]; then
  dest="$(mktemp -d "${TMPDIR:-/tmp}/atlas-api-stage.XXXXXX")"
else
  [ -e "$dest" ] && { echo "stage: $dest already exists" >&2; exit 1; }
  mkdir -p "$dest"
fi
case "$dest" in "$atlas"|"$atlas"/*) echo "stage: stage outside the repository" >&2; exit 1;; esac

designations="$atlas/var/osm_way_designations.json"
[ -s "$designations" ] || { echo "stage: run 'uv run python -m atlas designations' first" >&2; exit 1; }

install -m 644 "$atlas/api/Dockerfile" "$atlas/api/railway.toml" "$atlas/pyproject.toml" "$atlas/uv.lock" "$dest/"
(cd "$atlas" && find src/atlas -type f -name '*.py' -not -path '*/__pycache__/*' -print0) |
  while IFS= read -r -d '' f; do mkdir -p "$dest/$(dirname "$f")"; install -m 644 "$atlas/$f" "$dest/$f"; done
mkdir -p "$dest/var"
install -m 644 "$designations" "$dest/var/osm_way_designations.json"
cat > "$dest/NOTICE" <<'NOTICE'
var/osm_way_designations.json is derived from OpenStreetMap data.
© OpenStreetMap contributors. Available under the Open Database License (ODbL) 1.0:
https://www.openstreetmap.org/copyright
NOTICE

# Check the result: only allowlisted paths, nothing secret-looking, and a designations
# file holding only per-way street classes with its attribution.
python3 -I - "$dest" <<'PY'
import json, re, sys
from pathlib import Path
root = Path(sys.argv[1])
allowed = re.compile(r'^(Dockerfile|railway\.toml|pyproject\.toml|uv\.lock|NOTICE|var/osm_way_designations\.json|src/atlas/([a-z_]+/)*[a-z_]+\.py)$')
bad = re.compile(r'(^|/)\.|\.env|secret|passw|\.pem$|\.key$|token|credential', re.I)
files = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink())
problems = [f for f in files if not allowed.match(f) or bad.search(f) or (root / f).is_symlink()]
for f in files:
    # A password in a connection string, other than the loopback development default.
    for host in re.findall(r'postgres(?:ql)?://[^\s\'"/@:]+:[^\s\'"@]+@([^\s\'"/:]+)', (root / f).read_text()) if f.endswith('.py') else []:
        if host not in ('127.0.0.1', 'localhost'):
            problems.append(f + ' (embedded credentials)')
doc = json.loads((root / 'var/osm_way_designations.json').read_text())
if doc.get('format') != 'maply-osm-way-designations/1' or 'OpenStreetMap' not in str(doc.get('attribution')):
    problems.append('designations format or attribution')
if set(doc) - {'format', 'attribution', 'source_file', 'source_sha256', 'generated_at', 'ways'} or '/' in str(doc.get('source_file')):
    problems.append('designations has unexpected metadata')
if not doc.get('ways') or any(set(w) != {'way_id', 'name', 'highway', 'coords'} for w in doc['ways']):
    problems.append('designations has unexpected way fields')
if problems:
    sys.exit('stage: refusing: ' + ', '.join(problems))
total = sum((root / f).stat().st_size for f in files)
print(f'staged {len(files)} files, {total / 1e6:.2f} MB, {len(doc["ways"])} OSM ways ({doc["attribution"]}):', file=sys.stderr)
for f in files:
    print('  ' + f, file=sys.stderr)
PY
echo "$dest"
