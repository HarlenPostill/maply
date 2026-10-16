"""Coordinate handling.

* Distances, interpolation and offsets are computed in a projected CRS
  (default GDA2020 / MGA zone 56, EPSG:7856, which covers the City of Sydney).
  Latitude/longitude degrees are never treated as metres.
* Camera headings are relative to *true* north, while MGA grid north differs
  from true north by the meridian convergence (about 1 degree in Sydney). Bearings
  are therefore computed as geodesic azimuths on the GRS80 ellipsoid.
* WGS84 <-> GDA2020 differ by well under a metre at present; that is far below
  Street View positional uncertainty and is documented rather than corrected.
"""

from __future__ import annotations

import math
from functools import lru_cache

from pyproj import CRS, Geod, Transformer
import shapely
from shapely.geometry.base import BaseGeometry

WGS84 = 4326
DEFAULT_PROJECTED_EPSG = 7856  # GDA2020 / MGA zone 56

GEOD = Geod(ellps="GRS80")


@lru_cache(maxsize=8)
def _transformers(epsg: int) -> tuple[Transformer, Transformer]:
    fwd = Transformer.from_crs(WGS84, epsg, always_xy=True)
    inv = Transformer.from_crs(epsg, WGS84, always_xy=True)
    return fwd, inv


def area_of_use(epsg: int = DEFAULT_PROJECTED_EPSG) -> tuple[float, float, float, float]:
    aou = CRS.from_epsg(epsg).area_of_use
    assert aou is not None
    return (aou.west, aou.south, aou.east, aou.north)


def within_area_of_use(lon: float, lat: float, epsg: int = DEFAULT_PROJECTED_EPSG) -> bool:
    w, s, e, n = area_of_use(epsg)
    return w <= lon <= e and s <= lat <= n


def to_projected(geom: BaseGeometry, epsg: int = DEFAULT_PROJECTED_EPSG) -> BaseGeometry:
    fwd, _ = _transformers(epsg)
    return shapely.transform(geom, fwd.transform, interleaved=False)


def to_wgs84(geom: BaseGeometry, epsg: int = DEFAULT_PROJECTED_EPSG) -> BaseGeometry:
    _, inv = _transformers(epsg)
    return shapely.transform(geom, inv.transform, interleaved=False)


def project_point(lon: float, lat: float, epsg: int = DEFAULT_PROJECTED_EPSG) -> tuple[float, float]:
    fwd, _ = _transformers(epsg)
    return fwd.transform(lon, lat)


def unproject_point(x: float, y: float, epsg: int = DEFAULT_PROJECTED_EPSG) -> tuple[float, float]:
    _, inv = _transformers(epsg)
    return inv.transform(x, y)


# --------------------------------------------------------------------------
# Angles. All headings are degrees clockwise from true north in [0, 360).
# --------------------------------------------------------------------------


def normalize_heading(h: float) -> float:
    v = math.fmod(h, 360.0)
    if v < 0:
        v += 360.0
    # avoid returning 360.0 because of floating error
    return 0.0 if abs(v - 360.0) < 1e-9 else v


def heading_delta(a: float, b: float) -> float:
    """Signed smallest rotation from heading b to heading a, in (-180, 180]."""
    d = math.fmod(a - b, 360.0)
    if d <= -180.0:
        d += 360.0
    elif d > 180.0:
        d -= 360.0
    return d


def geodesic_azimuth(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    fwd_az, _back, _dist = GEOD.inv(lon1, lat1, lon2, lat2)
    return normalize_heading(fwd_az)


def geodesic_distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    _f, _b, dist = GEOD.inv(lon1, lat1, lon2, lat2)
    return float(dist)


# --------------------------------------------------------------------------
# Side-of-street convention
#
# "left" and "right" are ALWAYS relative to the stored direction of a segment
# (first vertex -> last vertex), never relative to the direction someone
# travels. A view whose heading points to the left of the segment bearing
# is a left-side view even when approached from the other end.
# --------------------------------------------------------------------------

SIDE_LEFT = "left"
SIDE_RIGHT = "right"
SIDE_NONE = "none"


def perpendicular_heading(segment_bearing: float, side: str) -> float:
    if side == SIDE_LEFT:
        return normalize_heading(segment_bearing - 90.0)
    if side == SIDE_RIGHT:
        return normalize_heading(segment_bearing + 90.0)
    raise ValueError(f"side must be left or right, got {side!r}")


def side_of_heading(heading: float, segment_bearing: float, tolerance_deg: float = 5.0) -> str:
    """Which side of the stored segment direction a view heading points toward."""
    d = heading_delta(heading, segment_bearing)
    if abs(d) <= tolerance_deg or abs(abs(d) - 180.0) <= tolerance_deg:
        return SIDE_NONE
    return SIDE_LEFT if d < 0 else SIDE_RIGHT


def offset_sign(side: str) -> float:
    """Shapely's offset_curve uses positive distance for the LEFT of line direction."""
    if side == SIDE_LEFT:
        return 1.0
    if side == SIDE_RIGHT:
        return -1.0
    raise ValueError(side)
