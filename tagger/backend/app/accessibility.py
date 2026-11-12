"""Read-only, granular dataset snapshots for the local accessibility atlas.

Publish with `python -m app.accessibility --output <data>/accessibility`.
Snapshots are atomic and never call capture/tagging recovery or paid providers.

Snapshot version 2 is columnar so every planned photo and every annotated
feature tag can be explored in the browser without per-photo requests:

* ``photos`` holds one entry per planned shot (tagged or not) in parallel arrays.
* ``features`` holds one entry per annotated feature, with its tags packed as
  integers (see ``pack_tag``). Tags that were never recorded are simply absent,
  so "not recorded", "unknown" and a known value stay distinct.
* Imagery month, capture time and annotation time are separate columns and are
  never substituted for one another.

A small ``<job>.meta.json`` manifest is rewritten on every pass. The full
snapshot is only rewritten when its content digest changes, so the browser can
poll the manifest cheaply and download the snapshot only when data changes.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Job, Segment, Shot
from app.tag_schema import SCHEMA_VERSION, TAG_VALUES

VERSION = 2
METRICS = ('footpath', 'quality', 'passage', 'surface', 'slope', 'crossing', 'hazards')
RULES = {
    'passage': {'clear_passage': {'narrowed', 'blocked'}},
    'surface': {'condition': {'visible_defect'}, 'texture': {'rough', 'very_rough'},
                'surface': {'loose_gravel', 'earth', 'sand', 'grass', 'rounded_cobblestones'}},
    'slope': {'cross_slope': {'moderate', 'steep'}, 'longitudinal_slope': {'moderate', 'steep'},
              'slope': {'moderate', 'steep'}},
    'crossing': {'presence': {'absent'}, 'transition_type': {'unmodified_kerb', 'steps'},
                 'bottom_join': {'raised_lip', 'step', 'open_gap', 'channel'}},
}
SAVED = ('captured', 'accepted', 'needs_check')
SIDES = ('left', 'right', 'none')
VISIBILITY = ('clear', 'partial', 'blurred', 'occluded', 'outside_frame')
KINDS = tuple(TAG_VALUES)
# Annotation state of a planned photo.
ANNOTATION = ('none', 'current', 'stale')
PRESENCE = ('present', 'absent', 'unknown')
NULL_CONFIDENCE = 127
MONTH = re.compile(r'^(\d{4})-(\d{2})$')


def known(tag):
    return tag and tag.get('value') != 'unknown' and tag.get('confidence') is not None and tag.get('visibility') in ('clear', 'partial', 'blurred')


def observations(labels):
    """Each photo contributes at most once to each denominator and signal count."""
    values = {key: [False, False] for key in METRICS}
    fp = labels.get('footpath')
    if known(fp):
        values['footpath'] = [True, fp['value'] == 'absent']
    for feature in labels.get('features', []):
        tags = feature['tags']
        # Features explicitly beside the route never contribute route concerns.
        if tags.get('route_relation', {}).get('value') == 'beside_route' or tags.get('affected_width', {}).get('value') == 'outside_route':
            continue
        for metric, rules in RULES.items():
            if metric == 'crossing' and feature['kind'] not in ('crossing', 'kerb_ramp'):
                continue
            for name, concerns in rules.items():
                tag = tags.get(name)
                if known(tag):
                    values[metric][0] = True
                    values[metric][1] |= tag['value'] in concerns
        if feature['kind'] == 'hazard' and known(tags.get('type')):
            relation = tags.get('route_relation', {}).get('value')
            if relation in ('on_route', 'partially_encroaching'):
                values['hazards'] = [True, True]
    values['quality'] = quality_observation(labels)
    return values


def quality_observation(labels):
    """Visible footpath concerns, never an inferred accessibility score or grade."""
    if labels.get('footpath', {}).get('value') != 'present':
        return [False, False]
    sections = [f for f in labels.get('features', []) if f['kind'] == 'footpath_section'
                and f['tags'].get('presence', {}).get('value') not in ('absent', 'unknown')
                and f['tags'].get('route_relation', {}).get('value') != 'beside_route']
    concerns = {**RULES['surface'], 'continuity': {'interrupted', 'ends'},
                'interruption': {'missing_surface', 'steps', 'barrier', 'construction', 'ends_in_road'}}
    if any(known(f['tags'].get(name)) and f['tags'][name]['value'] in values
           for f in sections for name, values in concerns.items()):
        return [True, True]
    # Concrete alone says nothing about condition. Require an explicit clear
    # no-defect observation for every visible section before using the zero category.
    no_defect = bool(sections) and all(
        known(f['tags'].get('condition')) and f['tags']['condition']['value'] == 'no_visible_defect'
        and f['tags']['condition']['visibility'] == 'clear' for f in sections)
    return [no_defect, False]


def pack_signals(values) -> int:
    """Two bits per metric in METRICS order: 0 not assessed, 1 no concern recorded, 2 concern."""
    packed = 0
    for i, metric in enumerate(METRICS):
        observed, flagged = values[metric]
        packed |= (2 if flagged else 1 if observed else 0) << (2 * i)
    return packed


def tag_vocabulary() -> dict[str, list[str]]:
    """Union of every allowed value per tag name, in schema order."""
    names: dict[str, list[str]] = {}
    for tags in TAG_VALUES.values():
        for name, values in tags.items():
            target = names.setdefault(name, [])
            target.extend(v for v in values if v not in target)
    return names


def pack_tag(name: int, value: int, visibility: int, confidence: float | None) -> int:
    """((name·32 + value)·8 + visibility)·128 + confidence percent (127 = none)."""
    percent = NULL_CONFIDENCE if confidence is None else max(0, min(100, round(confidence * 100)))
    return ((name * 32 + value) * 8 + visibility) * 128 + percent


def unpack_tag(packed: int) -> tuple[int, int, int, int | None]:
    percent = packed % 128
    packed //= 128
    visibility = packed % 8
    packed //= 8
    return packed // 32, packed % 32, visibility, None if percent == NULL_CONFIDENCE else percent


def epoch_minutes(value) -> int:
    """Minutes since the Unix epoch, or -1 when not recorded. Accepts datetimes or ISO strings."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return -1
    if not isinstance(value, datetime):
        return -1
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() // 60)


def imagery_month(value) -> int:
    """Provider-reported imagery month as year·12 + month-1, or -1 when undated."""
    match = MONTH.match(value) if isinstance(value, str) else None
    if not match:
        return -1
    year, month = int(match[1]), int(match[2])
    return year * 12 + month - 1 if 1 <= month <= 12 and 1990 <= year <= 2100 else -1


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Lookup:
    """Stable string → index table, serialised as a list."""

    def __init__(self, values=()):
        self.values: list[str] = list(values)
        self.index = {v: i for i, v in enumerate(self.values)}

    def __call__(self, value) -> int:
        if value is None or value == '':
            return -1
        value = str(value)
        if value not in self.index:
            self.index[value] = len(self.values)
            self.values.append(value)
        return self.index[value]


class Encoder:
    """Accumulates shots into columnar arrays. Pure: rows are plain attribute objects."""

    PHOTO_COLUMNS = (
        'id', 'code', 'segment', 'street', 'side', 'status', 'annotation', 'lng', 'lat', 'position',
        'heading', 'pano', 'imagery_month', 'imagery_date_type', 'captured', 'capture_source', 'tagged',
        'model', 'review_confirmation', 'image_source', 'review_reasons', 'footpath', 'footpath_visibility',
        'signals',
    )

    def __init__(self):
        self.photos = {key: [] for key in self.PHOTO_COLUMNS}
        self.features = dict(photo=[], kind=[], tags=[])
        self.vocab = tag_vocabulary()
        self.tag_names = Lookup(self.vocab)
        self.tag_values = {name: Lookup(values) for name, values in self.vocab.items()}
        self.lookups = {name: Lookup(values) for name, values in dict(
            status=('captured', 'accepted', 'needs_check', 'pending', 'no_imagery', 'blocked', 'skipped', 'retake'),
            imagery_date_type=('available', 'not_available'), capture_source=(), model=(),
            review_confirmation=('automatic', 'human'), image_source=(), highway=(), osm_sidewalk=(),
        ).items()}
        self.streets = Lookup()
        self.segments = Lookup()
        self.panos = Lookup()

    def add_shot(self, row) -> None:
        p = self.photos
        lk = self.lookups
        index = len(p['id'])
        camera = (getattr(row, 'camera_lng', None), getattr(row, 'camera_lat', None))
        observed = all(finite(v) for v in camera) and -180 <= camera[0] <= 180 and -90 <= camera[1] <= 90
        lng, lat = camera if observed else (row.lng, row.lat)
        saved = row.status in SAVED and bool(row.image_file)
        labels = row.labels if isinstance(row.labels, dict) else None
        current = bool(saved and labels and labels.get('schema_version') == SCHEMA_VERSION and row.sha
                       and labels.get('provenance', {}).get('image_sha256') == row.sha)
        provenance = labels.get('provenance', {}) if current else {}
        p['id'].append(str(row.id))
        p['code'].append(row.code)
        p['segment'].append(self.segments(row.segment_key))
        p['street'].append(self.streets(row.street or 'Unnamed street'))
        p['side'].append(SIDES.index(row.side) if row.side in SIDES else 2)
        p['status'].append(lk['status'](row.status))
        p['annotation'].append(1 if current else 2 if labels else 0)
        p['lng'].append(round(float(lng), 6))
        p['lat'].append(round(float(lat), 6))
        p['position'].append(0 if observed else 1)
        p['heading'].append(round(row.heading) % 360 if finite(row.heading) else -1)
        p['pano'].append(self.panos(getattr(row, 'pano_id', None)) if saved else -1)
        # Imagery month comes only from the saved capture result; it is never inferred.
        p['imagery_month'].append(imagery_month(getattr(row, 'image_date_iso', None)) if saved else -1)
        p['imagery_date_type'].append(lk['imagery_date_type'](getattr(row, 'image_date_type', None)) if saved else -1)
        p['captured'].append(epoch_minutes(getattr(row, 'captured_at', None)))
        p['capture_source'].append(lk['capture_source'](getattr(row, 'capture_source', None)))
        p['tagged'].append(epoch_minutes(provenance.get('tagged_at')))
        p['model'].append(lk['model'](provenance.get('model')))
        p['review_confirmation'].append(lk['review_confirmation'](provenance.get('review_confirmation')))
        p['image_source'].append(lk['image_source'](provenance.get('image_source')))
        if not current:
            for key in ('review_reasons', 'footpath', 'footpath_visibility', 'signals'):
                p[key].append(-1)
            return
        footpath = labels.get('footpath') or {}
        p['review_reasons'].append(len(labels.get('review_reasons') or []))
        p['footpath'].append(PRESENCE.index(footpath.get('value')) if footpath.get('value') in PRESENCE else 2)
        p['footpath_visibility'].append(VISIBILITY.index(footpath['visibility']) if footpath.get('visibility') in VISIBILITY else -1)
        p['signals'].append(pack_signals(observations(labels)))
        for feature in labels.get('features') or []:
            if feature.get('kind') not in KINDS:
                continue
            packed = []
            for name, tag in (feature.get('tags') or {}).items():
                if not isinstance(tag, dict) or name not in self.vocab:
                    continue
                value = self.tag_values[name](tag.get('value'))
                visibility = VISIBILITY.index(tag['visibility']) if tag.get('visibility') in VISIBILITY else 7
                confidence = tag.get('confidence') if finite(tag.get('confidence')) else None
                if value < 0 or value >= 32:
                    continue
                packed.append(pack_tag(self.tag_names(name), value, visibility, confidence))
            self.features['photo'].append(index)
            self.features['kind'].append(KINDS.index(feature['kind']))
            self.features['tags'].append(packed)

    def segment_table(self, rows) -> dict:
        """rows: (key, street, geojson geometry dict, length_m, osm dict). Only segments with planned shots are kept."""
        found = {}
        for key, street, geometry, length, osm in rows:
            if key in self.segments.index:
                found[key] = (street, geometry, length, osm or {})
        table = dict(key=[], street=[], length_m=[], highway=[], osm_sidewalk=[], coordinates=[])
        for key in self.segments.values:
            street, geometry, length, osm = found.get(key, ('', None, 0, {}))
            coordinates = [[round(x, 6), round(y, 6)] for x, y in (geometry or {}).get('coordinates', [])]
            table['key'].append(key)
            table['street'].append(self.streets(street or 'Unnamed street'))
            table['length_m'].append(round(float(length or 0), 1))
            table['highway'].append(self.lookups['highway'](osm.get('highway')))
            table['osm_sidewalk'].append(self.lookups['osm_sidewalk']((osm.get('osm_tags') or {}).get('sidewalk')))
            table['coordinates'].append(coordinates if len(coordinates) >= 2 else [])
        return table

    def payload(self, job, segment_rows) -> dict:
        segments = self.segment_table(segment_rows)
        return dict(
            version=VERSION, job_id=str(job.id), job_name=job.name, job_slug=getattr(job, 'slug', ''),
            vocabulary=dict(
                kinds=list(KINDS), visibility=list(VISIBILITY), sides=list(SIDES), annotation=list(ANNOTATION),
                presence=list(PRESENCE), metrics=list(METRICS), tag_names=self.tag_names.values,
                tag_values={name: lookup.values for name, lookup in self.tag_values.items()},
                kind_tags={kind: list(tags) for kind, tags in TAG_VALUES.items()},
                **{name: lookup.values for name, lookup in self.lookups.items()},
            ),
            streets=self.streets.values, segments=segments, photos=self.photos, features=self.features,
            pano_count=len(self.panos.values),
        )


def snapshot(session, job) -> dict:
    encoder = Encoder()
    rows = session.execute(select(
        Shot.id, Shot.segment_key, Shot.side, Shot.status, Shot.labels, Shot.code, Shot.street,
        Shot.lat, Shot.lng, Shot.heading, Shot.captured_at, Shot.capture_source,
        Shot.result['camera_lat'].label('camera_lat'), Shot.result['camera_lng'].label('camera_lng'),
        Shot.result['file'].astext.label('image_file'), Shot.result['sha256'].astext.label('sha'),
        Shot.result['pano_id'].astext.label('pano_id'), Shot.result['image_date_iso'].astext.label('image_date_iso'),
        Shot.result['image_date_type'].astext.label('image_date_type'),
    ).where(Shot.job_id == job.id).order_by(Shot.ordinal).execution_options(yield_per=1000))
    for row in rows:
        encoder.add_shot(row)
    segments = ((key, street, json.loads(geometry) if geometry else None, length, osm) for key, street, geometry, length, osm in session.execute(select(
        Segment.key, Segment.street, func.ST_AsGeoJSON(Segment.geom, 6), Segment.length_m,
        Segment.osm.op('-')('osm_way_ids'),
    ).where(Segment.job_id == job.id).order_by(Segment.ordinal)))
    return encoder.payload(job, segments)


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()).hexdigest()[:20]


def write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_snapshot(output: Path, payload: dict, now: datetime | None = None) -> bool:
    """Write meta always; rewrite the snapshot (and its gzip twin) only when content changed."""
    now = now or datetime.now(timezone.utc)
    job_id = payload['job_id']
    path, meta_path = output / f'{job_id}.json', output / f'{job_id}.meta.json'
    content = digest(payload)
    previous = {}
    try:
        previous = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        pass
    changed = previous.get('digest') != content or previous.get('version') != VERSION or not path.is_file()
    generated = now.isoformat() if changed else previous.get('generated_at', now.isoformat())
    if changed:
        body = json.dumps(dict(payload, digest=content, generated_at=generated), separators=(',', ':')).encode()
        write_atomic(path, body)
        write_atomic(path.with_name(path.name + '.gz'), gzip.compress(body, 6, mtime=0))
    counts = dict(photos=len(payload['photos']['id']), features=len(payload['features']['kind']))
    write_atomic(meta_path, json.dumps(dict(version=VERSION, job_id=job_id, digest=content, generated_at=generated,
                                            checked_at=now.isoformat(), **counts)).encode())
    return changed


def publish(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    # One consistent, read-only snapshot. No queue state, leases or labels are mutated.
    with Session(get_engine()) as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout = '25s'"))
        for job in session.scalars(select(Job).order_by(Job.created_at)):
            payload = snapshot(session, job)
            changed = write_snapshot(output, payload)
            print(f'{job.id}: {len(payload["photos"]["id"])} photos, {len(payload["features"]["kind"])} features'
                  f'{" (updated)" if changed else " (unchanged)"}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    publish(parser.parse_args().output)
