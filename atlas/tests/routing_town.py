"""A small synthetic town for routing tests. Coordinates are metres east/north of ORIGIN.

        P (200,200)
        |  Mall (pedestrian)
        N (200,100) ---- Top Street ---- NE (300,100)
        |                                 |
   Side Street                        East Street
        |                                 |
 W ---- M ---- J (200,0) ---- Main ------ E (300,0)
 (0,0) (100,0)

 Island Lane (0,300)-(100,300) is a separate component, more than 20 m from anything.
 Lower Lane (100,-30)-(200,-30) runs 30 m south of Main Street and is not joined to it.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

from atlas.routing.snapshot import EdgeRow, ObservationRow, Release, SegmentRow, Snapshot

ORIGIN = (151.20, -33.90)


def lnglat(x: float, y: float) -> tuple[float, float]:
    return (ORIGIN[0] + x / (111_195.08 * math.cos(math.radians(-33.90 + 0.0015))), ORIGIN[1] + y / 111_195.08)


def ll(x: float, y: float) -> dict:
    lng, lat = lnglat(x, y)
    return {'lat': lat, 'lng': lng}


NODES = {'W': (0, 0), 'M': (100, 0), 'J': (200, 0), 'E': (300, 0), 'N': (200, 100), 'NE': (300, 100), 'P': (200, 200),
         'I1': (0, 300), 'I2': (100, 300), 'L1': (100, -30), 'L2': (200, -30)}
# segment key -> (street name, highway in atlas, node path, OSM per-way class)
STREETS = {
    'main1': ('Main Street', 'tertiary', ['W', 'M'], 'tertiary'),
    'main2': ('Main Street', 'tertiary', ['M', 'J'], 'tertiary'),
    'main3': ('Main Street', 'tertiary', ['J', 'E'], 'tertiary'),
    'side': ('Side Street', 'residential', ['J', 'N'], 'residential'),
    'top': ('Top Street', 'residential', ['N', 'NE'], 'residential'),
    'east': ('East Street', 'residential', ['E', 'NE'], 'residential'),
    # The atlas says tertiary (merged by name); the OSM way says pedestrian.
    'mall': ('Mall', 'tertiary', ['N', 'P'], 'pedestrian'),
    'island': ('Island Lane', 'residential', ['I1', 'I2'], 'residential'),
    # 30 m south of Main Street and not joined to it: matching must never jump across Main Street to reach it.
    'lower': ('Lower Lane', 'residential', ['L1', 'L2'], 'residential'),
}


def present(**attrs):
    return [('footpath_section', {'presence': 'present', 'surface': 'concrete', 'path_type': 'dedicated_footpath',
                                  'continuity': 'continuous_visible_extent', **attrs})]


class Town:
    def __init__(self):
        self.extra: dict[tuple[str, str], list] = {}     # (segment, side) -> [(along, footpath, flagged, features, date)]
        self.observe_every = 10.0
        self.connectors: list[tuple[str, str]] = []
        self.observed_on = date(2024, 3, 1)
        self.vertices: dict[str, list[tuple[float, float]]] = {}   # extra interior vertices per straight segment
        self.moved: dict[str, tuple[float, float]] = {}            # node name -> replacement position

    def observe(self, seg: str, side: str, along: float, footpath='present', flagged=False, features=None, on=None):
        self.extra.setdefault((seg, side), []).append((along, footpath, flagged, features, on))
        return self

    def snapshot(self, default_footpath=True) -> Snapshot:
        at = {**NODES, **self.moved}
        node_ids = {name: i + 1 for i, name in enumerate(NODES)}
        nodes = {node_ids[n]: lnglat(*xy) for n, xy in at.items()}
        segments, edges, observations = {}, [], []
        oid = 0
        for sid, (key, (name, highway, path, _)) in enumerate(STREETS.items(), start=1):
            xys = [at[path[0]], *self.vertices.get(key, []), *[at[n] for n in path[1:]]]
            coords = [list(lnglat(*xy)) for xy in xys]
            length = sum(math.dist(a, b) for a, b in zip(xys, xys[1:]))
            segments[sid] = SegmentRow(sid, name, highway, length, coords)
            edges.append(EdgeRow(sid, 'street', sid, node_ids[path[0]], node_ids[path[-1]], 0.0, length, length, coords))
            for side in ('left', 'right'):
                rows = []
                if default_footpath:
                    along = 5.0
                    while along < length:
                        rows.append((along, 'present', False, None, None))
                        along += self.observe_every
                overrides = self.extra.get((key, side), [])
                override_pos = {round(a, 3) for a, *_ in overrides}
                rows = [r for r in rows if round(r[0], 3) not in override_pos] + overrides
                for along, footpath, flagged, features, on in rows:
                    oid += 1
                    x0, y0 = at[path[0]]
                    x1, y1 = at[path[-1]]
                    t = along / length
                    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
                    off = 1.0 if side == 'left' else -1.0
                    lng, lat = lnglat(x0 + t * (x1 - x0) + off * nx, y0 + t * (y1 - y0) + off * ny)
                    feats = present() if features is None and footpath == 'present' else (features or [])
                    observations.append(ObservationRow(oid, sid, side, along, lng, lat, on or self.observed_on, footpath,
                                                       flagged or footpath is None, 'maply_survey', feats))
        next_id = len(edges) + 1
        for a, b in self.connectors:
            coords = [list(lnglat(*NODES[a])), list(lnglat(*NODES[b]))]
            length = math.dist(NODES[a], NODES[b])
            edges.append(EdgeRow(next_id, 'connector', None, node_ids[a], node_ids[b], 0.0, length, length, coords))
            next_id += 1
        ring = [list(lnglat(x, y)) for x, y in ((-60, -60), (360, -60), (360, 360), (-60, 360), (-60, -60))]
        release = Release(1, datetime(2026, 11, 4, 11, 47, 49, tzinfo=timezone.utc), 'maply-accessibility/1', 'f' * 64, {})
        return Snapshot(release=release, coverage=[('Test Town (approximate)', {'type': 'MultiPolygon', 'coordinates': [[ring]]})],
                        segments=segments, edges=edges, nodes=nodes, observations=observations,
                        loaded_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc), marker=('test',))


def designations() -> dict:
    ways = []
    for i, (key, (name, _, path, osm_class)) in enumerate(STREETS.items(), start=100):
        ways.append({'way_id': i, 'name': name, 'highway': osm_class, 'coords': [list(lnglat(*NODES[n])) for n in path]})
    return {'format': 'maply-osm-way-designations/1', 'source_file': 'test', 'source_sha256': '0' * 64,
            'generated_at': '2026-11-05T00:00:00Z', 'ways': ways}


AS_OF = date(2026, 11, 5)
