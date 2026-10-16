"""Find the streets inside a blocked-out area, from OpenStreetMap.

Uses the local street file if present (fast, offline; create it once with
scripts/fetch_streets.py), otherwise asks the public Overpass servers in turn.
Street data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
from functools import lru_cache

import httpx
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import linemerge

from app.config import get_settings
from app.geo.projection import to_projected

# Streets that normally have footpaths and Street View coverage.
HIGHWAYS = ("primary", "secondary", "tertiary", "residential", "unclassified", "living_street", "pedestrian")
USER_AGENT = "maply/0.2 (local accessibility research tool)"


class StreetLookupError(RuntimeError):
    pass


def area_km2(polygon) -> float:
    return to_projected(polygon).area / 1_000_000


def overpass_query(poly_or_bbox: str, highways=HIGHWAYS, timeout: int = 25) -> str:
    return f'[out:json][timeout:{timeout}];way["highway"~"^({"|".join(highways)})$"]({poly_or_bbox});out geom tags;'


def fetch_overpass(query: str, timeout: float = 30) -> list[dict]:
    errors = []
    for url in [u.strip() for u in get_settings().overpass_urls.split(",") if u.strip()]:
        try:
            r = httpx.post(url, data={"data": query}, timeout=timeout, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json().get("elements", [])
        except (httpx.HTTPError, ValueError) as e:
            errors.append(f"{httpx.URL(url).host}: {e.__class__.__name__}")
    raise StreetLookupError("OpenStreetMap’s street servers are busy (" + "; ".join(errors) + "). Try again in a minute, or draw the streets yourself.")


def ways_to_lines(elements: list[dict]) -> list[tuple[LineString, str, str]]:
    out = []
    for el in elements:
        geom = el.get("geometry") or []
        if len(geom) >= 2:
            tags = el.get("tags") or {}
            out.append((LineString([(p["lon"], p["lat"]) for p in geom]), tags.get("name") or "Unnamed street", tags.get("highway", "")))
    return out


@lru_cache(maxsize=1)
def _local_index(path: str, mtime: float):
    gj = json.loads(open(path).read())
    lines = [(shape(f["geometry"]), f["properties"].get("name") or "Unnamed street", f["properties"].get("highway", "")) for f in gj["features"]]
    return lines, STRtree([g for g, _, _ in lines])


def find_streets(area_geojson: dict, highways: tuple[str, ...] = HIGHWAYS) -> dict:
    settings = get_settings()
    area = shape(area_geojson)
    if area.geom_type not in ("Polygon", "MultiPolygon") or area.is_empty:
        raise StreetLookupError("Draw an area (a closed shape) first")
    km2 = area_km2(area)
    if km2 > settings.max_area_km2:
        raise StreetLookupError(f"That area is {km2:.1f} km². Draw a smaller area (up to {settings.max_area_km2:g} km²) or split it into several jobs.")

    local = settings.streets_file.resolve()
    if local.is_file():
        lines, tree = _local_index(str(local), local.stat().st_mtime)
        candidates = [lines[i] for i in tree.query(area)]
        source = "local street file"
    else:
        ring = area.exterior if area.geom_type == "Polygon" else max(area.geoms, key=lambda g: g.area).exterior
        poly = 'poly:"' + " ".join(f"{lat:.6f} {lng:.6f}" for lng, lat in ring.coords) + '"'
        candidates = ways_to_lines(fetch_overpass(overpass_query(poly, highways)))
        source = "OpenStreetMap Overpass"

    by_name: dict[str, list[LineString]] = {}
    kinds: dict[str, str] = {}
    for line, name, kind in candidates:
        if kind not in highways:
            continue
        clipped = line.intersection(area)
        by_name.setdefault(name, []).extend(p for p in getattr(clipped, "geoms", [clipped]) if isinstance(p, LineString) and p.length > 0)
        kinds[name] = kind
    features = []
    for name, parts in sorted(by_name.items()):
        if not parts:
            continue
        merged = linemerge(MultiLineString(parts)) if len(parts) > 1 else parts[0]
        for part in getattr(merged, "geoms", [merged]):
            if to_projected(part).length >= 15:  # ignore stubs
                features.append({"type": "Feature", "properties": {"name": name, "highway": kinds.get(name, "")}, "geometry": mapping(part)})
    return {"type": "FeatureCollection", "features": features, "source": source, "attribution": "Street data © OpenStreetMap contributors (ODbL)"}
