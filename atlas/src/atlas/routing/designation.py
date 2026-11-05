"""Per-way OSM street designations, so the pedestrian / shared-zone allowance is trustworthy.

The tagger merged OSM ways by street name and kept the first way's highway class
for the whole name, so atlas.segments.highway cannot grant the allowance. The
tagger's street file still holds each way's own geometry and class. `build` copies
the relevant ways into a small OSM-derived file under atlas/var/ (read-only use of
the tagger file); the routing service only ever reads that file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from atlas.routing.geo import Projector, SegmentGrid, cumulative, locate, point_at

HIGHWAYS = ('primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'living_street', 'pedestrian')
DESIGNATED = {'pedestrian': 'designated_pedestrian', 'living_street': 'designated_shared'}
FORMAT = 'maply-osm-way-designations/1'
UNNAMED = 'Unnamed street'


def build(street_file: Path, output: Path) -> dict:
    raw = street_file.read_bytes()
    data = json.loads(raw)
    ways = []
    for f in data.get('features', []):
        p = f.get('properties') or {}
        g = f.get('geometry') or {}
        if p.get('highway') in HIGHWAYS and g.get('type') == 'LineString' and p.get('way_id') is not None:
            ways.append({'way_id': p['way_id'], 'name': p.get('name') or UNNAMED, 'highway': p['highway'],
                         'coords': [[round(x, 7), round(y, 7)] for x, y in g['coordinates']]})
    doc = {'format': FORMAT, 'attribution': data.get('attribution', '© OpenStreetMap contributors (ODbL)'),
           'source_file': street_file.name, 'source_sha256': hashlib.sha256(raw).hexdigest(),
           'generated_at': datetime.now(timezone.utc).isoformat(), 'ways': ways}
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc, separators=(',', ':')))
    tmp.replace(output)
    return {'ways': len(ways), 'output': str(output), 'source_sha256': doc['source_sha256'][:12]}


def load(path: Path | None) -> dict | None:
    if not path or not path.is_file():
        return None
    doc = json.loads(path.read_text())
    if doc.get('format') != FORMAT:
        raise ValueError(f'{path} is not a {FORMAT} file')
    return doc


def category(highway: str) -> str:
    return DESIGNATED.get(highway, 'ordinary')


class Designator:
    """Classifies a street piece from the same-name OSM ways that run alongside it.

    Every way within match_m of a sample counts, not just the nearest one, so input
    order never decides a tie. A way only counts where the sample projects onto it, not
    beyond its ends, so a way that merely starts at a junction does not count for the
    street piece that leaves it. If the ways alongside one sample disagree, the sample is
    a conflict and the whole piece is ambiguous.
    """

    def __init__(self, doc: dict | None, proj: Projector, spacing_m: float = 2.0, match_m: float = 3.0):
        self.available = doc is not None
        self.digest = content_digest(doc)
        self.source = None if doc is None else {'source_file': doc.get('source_file'),
                                                'source_sha256': (doc.get('source_sha256') or '')[:12],
                                                'generated_at': doc.get('generated_at'),
                                                'content_sha256': self.digest[:12]}
        self.spacing, self.match = spacing_m, match_m
        self.ways: list[tuple[int, str, list, list]] = []
        self.grids: dict[str, SegmentGrid] = {}
        for way in (doc or {}).get('ways', []):
            line = proj.line(way['coords'])
            index = len(self.ways)
            self.ways.append((way['way_id'], way['highway'], line, cumulative(line)))
            self.grids.setdefault(way['name'], SegmentGrid(cell=25.0)).add_line(line, index)

    def classify(self, street_name: str | None, line, cum) -> tuple[str, float, list[int], str]:
        """(designation, matched share of samples, matched OSM way ids, basis) for a projected piece."""
        if not self.available:
            return 'ambiguous', 0.0, [], 'designations_unavailable'
        grid = self.grids.get(street_name or UNNAMED)
        total = cum[-1]
        if total <= 2.0:
            samples = [total / 2]
        else:
            n = max(1, int((total - 2.0) // self.spacing))
            samples = [1.0 + i * (total - 2.0) / n for i in range(n + 1)]
        per_sample, matched, way_ids = [], 0, set()
        for along in samples:
            p = point_at(line, cum, along)
            found = set()
            if grid is not None:
                for index in {payload for _, _, payload in grid.near(p, self.match)}:
                    way_id, highway, wline, wcum = self.ways[index]
                    loc = locate(wline, wcum, p)
                    if loc.distance <= self.match and not loc.clamped:
                        found.add((way_id, category(highway)))
            if found:
                matched += 1
                way_ids.update(w for w, _ in found)
            per_sample.append({c for _, c in found})
        share = matched / len(samples)
        ids = sorted(way_ids)
        if any(len(c) > 1 for c in per_sample):
            return 'ambiguous', share, ids, 'conflicting_ways'
        if any(not c for c in per_sample):
            return 'ambiguous', share, ids, 'unmatched_samples'
        cats = set().union(*per_sample)
        if len(cats) > 1:
            return 'ambiguous', share, ids, 'mixed_along_piece'
        return cats.pop(), share, ids, 'per_way_match'


def content_digest(doc: dict | None) -> str:
    """Digest of exactly what the designator uses, so any change to the file changes it."""
    if doc is None:
        return 'none'
    ways = sorted((w['way_id'], w['name'], w['highway'], w['coords']) for w in doc.get('ways', []))
    return hashlib.sha256(json.dumps(ways, separators=(',', ':')).encode()).hexdigest()


class FileSource:
    """Reads the designations file, re-reading only when its size or mtime changes."""

    def __init__(self, path: Path | None):
        self.path = path
        self._stamp = None
        self._doc = None

    def current(self) -> dict | None:
        if not self.path or not self.path.is_file():
            self._stamp, self._doc = None, None
            return None
        st = self.path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            self._doc, self._stamp = load(self.path), stamp
        return self._doc
