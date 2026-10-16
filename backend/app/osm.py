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


# OSM tags kept with each street: useful context for later accessibility analysis.
KEEP_TAGS = (
    "highway", "name", "sidewalk", "sidewalk:left", "sidewalk:right", "sidewalk:both", "footway",
    "kerb", "surface", "oneway", "lanes", "maxspeed", "lit", "width", "bus", "cycleway",
)


def ways_to_lines(elements: list[dict]) -> list[tuple[LineString, str, str, dict]]:
    """(line, name, highway, osm) for each way; osm = {"way_id": .., "tags": {...}}."""
    out = []
    for el in elements:
        geom = el.get("geometry") or []
        if len(geom) >= 2:
            tags = el.get("tags") or {}
            osm = {"way_id": el.get("id"), "tags": {k: v for k, v in tags.items() if k in KEEP_TAGS}}
            out.append((LineString([(p["lon"], p["lat"]) for p in geom]), tags.get("name") or "Unnamed street", tags.get("highway", ""), osm))
    return out


@lru_cache(maxsize=1)
def _local_index(path: str, mtime: float):
    gj = json.loads(open(path).read())
    lines = [
        (shape(f["geometry"]), f["properties"].get("name") or "Unnamed street", f["properties"].get("highway", ""),
         {"way_id": f["properties"].get("way_id"), "tags": f["properties"].get("tags", {})})
        for f in gj["features"]
    ]
    return lines, STRtree([x[0] for x in lines])


def local_streets_available() -> bool:
    return get_settings().streets_file.resolve().is_file()


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

    return {"type": "FeatureCollection", "features": streets_from_candidates(candidates, area, highways), "source": source, "attribution": "Street data © OpenStreetMap contributors (ODbL)"}


def streets_from_candidates(candidates, area, highways=HIGHWAYS) -> list[dict]:
    """Clip ways to the area and merge the pieces of each street, keeping OSM ids and tags."""
    by_name: dict[str, list[LineString]] = {}
    meta: dict[str, dict] = {}
    for line, name, kind, osm in candidates:
        if kind not in highways:
            continue
        clipped = line.intersection(area) if area is not None else line
        pieces = [p for p in getattr(clipped, "geoms", [clipped]) if isinstance(p, LineString) and p.length > 0]
        if not pieces:
            continue
        by_name.setdefault(name, []).extend(pieces)
        m = meta.setdefault(name, {"highway": kind, "way_ids": [], "tags": {}})
        if osm.get("way_id") is not None:
            m["way_ids"].append(osm["way_id"])
        for k, v in (osm.get("tags") or {}).items():
            m["tags"].setdefault(k, v)  # first value wins; per-way detail stays in the street file
    features = []
    for name, parts in sorted(by_name.items()):
        merged = linemerge(MultiLineString(parts)) if len(parts) > 1 else parts[0]
        for part in getattr(merged, "geoms", [merged]):
            if to_projected(part).length >= 15:  # ignore stubs
                m = meta[name]
                features.append({"type": "Feature", "properties": {"name": name, "highway": m["highway"], "osm_way_ids": sorted(set(m["way_ids"])), "osm_tags": m["tags"]}, "geometry": mapping(part)})
    return features


def whole_scope_streets(scope) -> dict:
    """Every street in the approved boundary, from the local street file (required: too big for Overpass per click)."""
    local = get_settings().streets_file.resolve()
    if not local.is_file():
        raise StreetLookupError("The local street file is missing. Run scripts/fetch_streets.py once, then try again.")
    lines, tree = _local_index(str(local), local.stat().st_mtime)
    candidates = [lines[i] for i in tree.query(scope)]
    features = streets_from_candidates(candidates, scope)
    # Order streets so neighbouring streets are photographed one after another
    # (Hilbert curve over street midpoints), instead of alphabetically.
    w, s_, e, n = scope.bounds
    features.sort(key=lambda f: _hilbert(shape(f["geometry"]).interpolate(0.5, normalized=True), w, s_, e, n))
    return {"type": "FeatureCollection", "features": features, "source": "local street file", "attribution": "Street data © OpenStreetMap contributors (ODbL)"}


def _hilbert(pt, w, s, e, n, order: int = 10) -> int:
    size = 1 << order
    x = min(size - 1, max(0, int((pt.x - w) / (e - w) * size)))
    y = min(size - 1, max(0, int((pt.y - s) / (n - s) * size)))
    d, sq = 0, size // 2
    while sq > 0:
        rx, ry = int(x & sq > 0), int(y & sq > 0)
        d += sq * sq * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = sq - 1 - x, sq - 1 - y
            x, y = y, x
        sq //= 2
    return d
