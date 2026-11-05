"""Connection settings from the environment, or from atlas/.env when present."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent
MIGRATIONS = ROOT / 'migrations'
TAGGER = REPO / 'tagger'
COVERAGE_FILE = TAGGER / 'config' / 'city_of_sydney_approx.geojson'
STREET_FILE = TAGGER / 'data' / 'osm' / 'city_of_sydney_streets.geojson'
DESIGNATIONS_FILE = ROOT / 'var' / 'osm_way_designations.json'
DEFAULT_SOURCE_URL = 'postgresql://maply:maply@127.0.0.1:5433/maply'


def _dotenv() -> dict[str, str]:
    path = ROOT / '.env'
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _dotenv().get(name) or default


def target_url() -> str:
    url = setting('ATLAS_DATABASE_URL')
    if not url:
        raise SystemExit('Set ATLAS_DATABASE_URL (environment or atlas/.env) to the production database URL.')
    return url


def source_url() -> str:
    return setting('MAPLY_DATABASE_URL', DEFAULT_SOURCE_URL)
