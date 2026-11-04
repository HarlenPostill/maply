"""Read-only, per-segment image observations for the live visualiser.

Publish with `python -m app.accessibility --output /app/frontend/dist/assets/accessibility`.
Snapshots are atomic and never call capture/tagging recovery or paid providers.
"""
from __future__ import annotations

import argparse
import json
import os
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Job, Segment, Shot

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
    """Visible footpath quality, never an inferred accessibility score."""
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


def empty_counts():
    return dict(planned=0, saved=0, tagged=0, review=0,
                metrics={key: dict(observed=0, flagged=0, examples=[]) for key in METRICS}, examples=[])


def add_photo(counts, row):
    counts['planned'] += 1
    saved = row.status in ('captured', 'accepted', 'needs_check') and bool(row.image_file)
    counts['saved'] += int(saved)
    labels = row.labels
    if not saved or not labels or labels.get('schema_version') != 'maply-accessibility/1':
        return
    if not row.sha or labels.get('provenance', {}).get('image_sha256') != row.sha:
        return
    counts['tagged'] += 1
    counts['review'] += int(bool(labels.get('review_reasons')))
    sid = str(row.id)
    if len(counts['examples']) < 6:
        counts['examples'].append(sid)
    values = observations(labels)
    for metric, (observed, flagged) in values.items():
        target = counts['metrics'][metric]
        target['observed'] += int(observed)
        target['flagged'] += int(flagged)
        if flagged and len(target['examples']) < 6:
            target['examples'].append(sid)
    return values


def photo_point(row, values):
    camera = (row.camera_lng, row.camera_lat)
    observed = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in camera)
    observed = observed and -180 <= camera[0] <= 180 and -90 <= camera[1] <= 90
    return dict(id=str(row.id), key=row.segment_key, code=row.code, street=row.street or 'Unnamed street',
                side=row.side, coordinates=list(camera if observed else (row.lng, row.lat)),
                position_source='camera' if observed else 'planned', heading=row.heading,
                metrics={key: 1 if flagged else 0 if known else -1 for key, (known, flagged) in values.items()})


def snapshot(session, job):
    groups = defaultdict(empty_counts)
    points = []
    rows = session.execute(select(
        Shot.id, Shot.segment_key, Shot.side, Shot.status, Shot.labels,
        Shot.code, Shot.street, Shot.lat, Shot.lng, Shot.heading,
        Shot.result['camera_lat'].label('camera_lat'), Shot.result['camera_lng'].label('camera_lng'),
        Shot.result['file'].astext.label('image_file'), Shot.result['sha256'].astext.label('sha'),
    ).where(Shot.job_id == job.id).order_by(Shot.ordinal).execution_options(yield_per=500))
    for row in rows:
        values = add_photo(groups[(row.segment_key, row.side)], row)
        if values is not None:
            points.append(photo_point(row, values))
    segments = []
    for key, street, geometry in session.execute(select(
        Segment.key, Segment.street, func.ST_AsGeoJSON(Segment.geom),
    ).where(Segment.job_id == job.id).order_by(Segment.ordinal)):
        for side in ('left', 'right', 'none'):
            if (key, side) in groups:
                segments.append(dict(key=key, street=street or 'Unnamed street', side=side,
                                     coordinates=json.loads(geometry)['coordinates'], **groups[(key, side)]))
    return dict(version=1, generated_at=datetime.now(timezone.utc).isoformat(),
                job_id=str(job.id), job_name=job.name, segments=segments, points=points)


def publish(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    # One consistent, read-only snapshot. No queue state, leases or labels are mutated.
    with Session(get_engine()) as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout = '20s'"))
        for job in session.scalars(select(Job).order_by(Job.created_at)):
            payload = snapshot(session, job)
            path = output / f'{job.id}.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload, separators=(',', ':')))
            os.replace(temporary, path)
            print(f'{job.id}: {len(payload["segments"])} segment sides', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    publish(parser.parse_args().output)
