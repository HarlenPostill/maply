"""Download the City of Sydney street network once, so "Block out an area" works offline.

    cd backend && uv run python ../scripts/fetch_streets.py

Writes data/osm/city_of_sydney_streets.geojson (OpenStreetMap data, ODbL:
keep the attribution). Re-run whenever you want fresher street data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from shapely.geometry import mapping, shape  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.osm import fetch_overpass, overpass_query, ways_to_lines  # noqa: E402


def main() -> int:
    s = get_settings()
    scope = shape(json.loads(s.scope_geojson_path.read_text())["features"][0]["geometry"]).buffer(0.002)
    w, so, e, n = scope.bounds
    print("Downloading streets for the City of Sydney from OpenStreetMap (this can take a minute)…")
    lines = ways_to_lines(fetch_overpass(overpass_query(f"{so},{w},{n},{e}", timeout=120), timeout=180))
    feats = [
        {"type": "Feature", "properties": {"name": name, "highway": kind, "way_id": osm["way_id"], "tags": osm["tags"]}, "geometry": mapping(line)}
        for line, name, kind, osm in lines
        if line.intersects(scope)
    ]
    out = s.streets_file.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "attribution": "© OpenStreetMap contributors (ODbL)", "features": feats}))
    print(f"Saved {len(feats)} street pieces to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
