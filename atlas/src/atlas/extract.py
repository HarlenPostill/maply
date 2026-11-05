"""Read the tagger database (read-only) and reduce it to production facts.

Kept: street geometry, walking-relevant OSM tags, survey locations, imagery
month, footpath presence and the known tag values of each feature.
Dropped: photos, thumbnails, model confidence, evidence text, image regions,
prompts, queue state and capture telemetry.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import psycopg

SCHEMA_VERSION = 'maply-accessibility/1'
OSM_KEYS = ('sidewalk', 'sidewalk:both', 'sidewalk:left', 'sidewalk:right', 'surface', 'lit',
            'maxspeed', 'width', 'kerb', 'footway', 'lanes', 'oneway')
SAVED = ('captured', 'accepted', 'needs_check')
NO_DATA = ('no_imagery', 'blocked', 'skipped')


@dataclass
class Segment:
    key: str
    street_name: str | None
    highway: str
    length_m: float
    bearing_deg: float
    osm: dict
    wkt: str


@dataclass
class Feature:
    kind: str
    attrs: dict


@dataclass
class Observation:
    footpath: str | None
    observed_on: date | None
    needs_verification: bool
    features: list[Feature] = field(default_factory=list)


@dataclass
class Point:
    shot_id: str
    code: str
    segment_key: str
    side: str
    facing: str | None
    status: str
    heading: float
    lng: float
    lat: float
    observation: Observation | None = None


@dataclass
class Dataset:
    segments: list[Segment]
    points: list[Point]
    coverage: dict[str, str] = field(default_factory=dict)  # name -> MultiPolygon GeoJSON
    skipped: dict[str, int] = field(default_factory=dict)

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for item in [*self.segments, *self.points]:
            digest.update(json.dumps(asdict(item), sort_keys=True, default=str).encode())
        for name in sorted(self.coverage):
            digest.update(name.encode() + self.coverage[name].encode())
        return digest.hexdigest()

    def stats(self) -> dict:
        observed = [p.observation for p in self.points if p.observation]
        kinds: dict[str, int] = {}
        for o in observed:
            for f in o.features:
                kinds[f.kind] = kinds.get(f.kind, 0) + 1
        statuses: dict[str, int] = {}
        for p in self.points:
            statuses[p.status] = statuses.get(p.status, 0) + 1
        footpath = {'present': 0, 'absent': 0, 'unknown': 0}
        for o in observed:
            footpath[o.footpath or 'unknown'] += 1
        return {'segments': len(self.segments), 'survey_points': len(self.points),
                'points_by_status': dict(sorted(statuses.items())), 'observations': len(observed),
                'footpath': footpath, 'features_by_kind': dict(sorted(kinds.items())),
                'needs_verification': sum(o.needs_verification for o in observed),
                'skipped_source_rows': self.skipped}


def imagery_month(value) -> date | None:
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}', value):
        year, month = map(int, value.split('-'))
        if 1990 <= year <= 2100 and 1 <= month <= 12:
            return date(year, month, 1)
    return None


def observation_from_labels(labels: dict, status: str, imagery: str | None) -> Observation:
    footpath = labels.get('footpath', {}).get('value')
    footpath = footpath if footpath in ('present', 'absent') else None
    features = []
    for feature in labels.get('features', []):
        attrs = {name: tag['value'] for name, tag in sorted(feature.get('tags', {}).items())
                 if tag.get('value') and tag['value'] != 'unknown'}
        features.append(Feature(kind=feature['kind'], attrs=attrs))
    return Observation(footpath=footpath, observed_on=imagery_month(imagery),
                       needs_verification=status == 'needs_check' or footpath is None,
                       features=features)


def point_from_row(row: dict) -> Point:
    result = row['result'] or {}
    labels = row['labels']
    status = row['status']
    saved = status in SAVED and bool(result.get('file'))
    current = (saved and labels and labels.get('schema_version') == SCHEMA_VERSION
               and result.get('sha256') and labels.get('provenance', {}).get('image_sha256') == result['sha256'])
    lng, lat = result.get('camera_lng'), result.get('camera_lat')
    if not (isinstance(lng, (int, float)) and isinstance(lat, (int, float))
            and -180 <= lng <= 180 and -90 <= lat <= 90):
        lng, lat = row['lng'], row['lat']
    observation = observation_from_labels(labels, status, result.get('image_date_iso')) if current else None
    return Point(
        shot_id=str(row['id']), code=row['code'], segment_key=row['segment_key'], side=row['side'],
        facing=row['footpath'] or None,
        status='observed' if observation else status if status in NO_DATA else 'pending',
        heading=float(row['heading']), lng=float(lng), lat=float(lat), observation=observation)


def load_coverage(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    features = data.get('features', [data]) if data.get('type') == 'FeatureCollection' else [data]
    out = {}
    for feature in features:
        geometry = feature.get('geometry', feature)
        name = (feature.get('properties') or {}).get('name') or path.stem.replace('_', ' ')
        if geometry.get('type') == 'Polygon':
            geometry = {'type': 'MultiPolygon', 'coordinates': [geometry['coordinates']]}
        if geometry.get('type') == 'MultiPolygon':
            out[name] = json.dumps(geometry)
    return out


def extract(conn: psycopg.Connection, coverage_file: Path | None = None) -> Dataset:
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    segments: dict[str, Segment] = {}
    duplicates = 0
    for key, street, osm, length_m, bearing, wkt in conn.execute("""
            SELECT s.key, s.street, s.osm, s.length_m, s.bearing_deg, ST_AsText(s.geom)
            FROM segments s JOIN jobs j ON j.id = s.job_id ORDER BY j.created_at, s.ordinal"""):
        if key in segments:
            duplicates += 1
            continue
        tags = (osm or {}).get('osm_tags') or {}
        segments[key] = Segment(
            key=key, street_name=None if not street or street == 'Unnamed street' else street,
            highway=(osm or {}).get('highway') or tags.get('highway') or 'unknown',
            length_m=float(length_m), bearing_deg=float(bearing),
            osm={k: tags[k] for k in OSM_KEYS if k in tags}, wkt=wkt)
    points, seen, unplaced = [], set(), 0
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("""SELECT s.id, s.code, s.segment_key, s.side, s.footpath, s.status, s.heading,
                              s.lat, s.lng, s.result, s.labels - 'provenance' || jsonb_build_object(
                                'provenance', jsonb_build_object('image_sha256', s.labels->'provenance'->'image_sha256')) AS labels
                       FROM shots s JOIN jobs j ON j.id = s.job_id
                       WHERE s.side IN ('left', 'right')
                       ORDER BY j.created_at, s.ordinal""")
        for row in cur:
            if row['segment_key'] not in segments or row['id'] in seen:
                unplaced += 1
                continue
            seen.add(row['id'])
            if row['labels'] is not None and row['labels'].get('schema_version') is None:
                row['labels'] = None
            points.append(point_from_row(row))
    conn.rollback()
    return Dataset(segments=list(segments.values()), points=points,
                   coverage=load_coverage(coverage_file) if coverage_file else {},
                   skipped={'duplicate_segments': duplicates, 'unplaced_points': unplaced})
