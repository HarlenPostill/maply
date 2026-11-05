"""Atlas tests. Needs a disposable PostGIS + pgRouting database whose name contains "test":

    ATLAS_TEST_DATABASE_URL=postgresql://atlas:localonly@127.0.0.1:5442/atlas_test uv run pytest
"""
from __future__ import annotations

import math
import os
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest

from atlas.extract import Dataset, Feature, Observation, Point, Segment, observation_from_labels, point_from_row
from atlas.migrate import migrate
from atlas.publish import publish

URL = os.environ.get('ATLAS_TEST_DATABASE_URL')
ORIGIN = (151.20, -33.90)


def lnglat(x: float, y: float) -> tuple[float, float]:
    """Metres east/north of ORIGIN to lng/lat."""
    return (ORIGIN[0] + x / (111_320 * math.cos(math.radians(-ORIGIN[1]))), ORIGIN[1] + y / 110_946)


def line(*xy) -> str:
    return 'LINESTRING(' + ', '.join('%.9f %.9f' % lnglat(x, y) for x, y in xy) + ')'


def seg(key, name, *xy, highway='residential'):
    length = sum(math.dist(a, b) for a, b in zip(xy, xy[1:]))
    return Segment(key=key, street_name=name, highway=highway, length_m=length, bearing_deg=0, osm={}, wkt=line(*xy))


GOOD = [Feature('footpath_section', {'presence': 'present', 'surface': 'concrete', 'condition': 'no_visible_defect',
                                     'clear_passage': 'unobstructed', 'path_type': 'dedicated_footpath'})]
STEPS = [Feature('footpath_section', {'presence': 'present', 'surface': 'concrete', 'interruption': 'steps',
                                      'continuity': 'interrupted'})]


def points_for(segment: Segment, xy, footpath='present', features=GOOD, status='observed', needs=False):
    out = []
    for side in ('left', 'right'):
        lng, lat = lnglat(*xy)
        obs = None if status != 'observed' else Observation(
            footpath=footpath, observed_on=date(2021, 3, 1), needs_verification=needs,
            features=[Feature(f.kind, dict(f.attrs)) for f in (features if footpath == 'present' else [])])
        out.append(Point(shot_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f'{segment.key}/{side}/{xy}')),
                         code=f'{segment.key}-{side[0]}', segment_key=segment.key, side=side, facing=None,
                         status=status, heading=0, lng=lng, lat=lat, observation=obs))
    return out


def grid() -> Dataset:
    """A 200 x 100 m block. The bottom street has no footpath; the detour is good.

    P3 ---- top ---- P2
    |        |T       |
    left     north    right
    |                 |
    P0 -- bottom --- P1
    """
    segments = [
        seg('bottom', 'Bottom St', (0, 0), (200, 0)),
        seg('left', 'Left St', (0, 0), (0, 100)),
        seg('top', 'Top St', (0, 100), (200, 100)),
        seg('right', 'Right St', (200, 100), (200, 0)),
        seg('north', 'North St', (100, 100), (100, 150)),      # T-junction onto Top St's interior
        seg('lane', 'Gap Lane', (100, 158), (100, 220)),       # stops 8 m short of North St
    ]
    by = {s.key: s for s in segments}
    points = [
        *points_for(by['bottom'], (100, 0), footpath='absent'),
        *points_for(by['left'], (0, 50)),
        *points_for(by['top'], (50, 100)),
        *points_for(by['top'], (150, 100)),
        *points_for(by['right'], (200, 50)),
        *points_for(by['north'], (100, 125), features=STEPS, needs=True),
        *points_for(by['lane'], (100, 190), status='no_imagery'),
    ]
    return Dataset(segments=segments, points=points,
                   coverage={'Test block': '{"type":"MultiPolygon","coordinates":[[[[151.19,-33.91],[151.21,-33.91],[151.21,-33.89],[151.19,-33.89],[151.19,-33.91]]]]}'})


@pytest.fixture
def db():
    if not URL:
        pytest.skip('ATLAS_TEST_DATABASE_URL not set')
    assert 'test' in psycopg.conninfo.conninfo_to_dict(URL)['dbname'], 'Never run tests against a real atlas database'
    with psycopg.connect(URL) as conn:
        conn.execute('DROP SCHEMA IF EXISTS atlas CASCADE')
        conn.execute('DROP TABLE IF EXISTS public.schema_migrations')
        conn.commit()
        migrate(conn)
        yield conn


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


# --- transform ---------------------------------------------------------------

def test_labels_keep_only_known_values_without_confidence():
    labels = {'schema_version': 'maply-accessibility/1',
              'footpath': {'value': 'present', 'confidence': 0.9, 'evidence': 'x', 'visibility': 'clear'},
              'features': [{'id': 'f1', 'kind': 'footpath_section', 'region': [0, 0, 1, 1], 'location_description': 'x',
                            'tags': {'presence': {'value': 'present', 'confidence': 0.9, 'evidence': 'x', 'visibility': 'clear'},
                                     'surface': {'value': 'unknown', 'confidence': None, 'evidence': 'x', 'visibility': 'occluded'}}}],
              'notes': ['n'], 'review_reasons': ['r']}
    obs = observation_from_labels(labels, 'captured', '2021-03')
    assert obs.footpath == 'present' and obs.observed_on == date(2021, 3, 1) and not obs.needs_verification
    assert [(f.kind, f.attrs) for f in obs.features] == [('footpath_section', {'presence': 'present'})]
    unknown = observation_from_labels({**labels, 'footpath': {'value': 'unknown'}, 'features': []}, 'needs_check', None)
    assert unknown.footpath is None and unknown.needs_verification and unknown.observed_on is None


def test_stale_labels_and_missing_camera_position():
    row = dict(id=uuid.uuid4(), code='S1-L1', segment_key='k', side='left', footpath='west', status='captured',
               heading=90.0, lat=-33.9, lng=151.2,
               result={'file': 'a.jpg', 'sha256': 'new', 'camera_lat': None},
               labels={'schema_version': 'maply-accessibility/1', 'footpath': {'value': 'present'}, 'features': [],
                       'provenance': {'image_sha256': 'old'}})
    point = point_from_row(row)
    assert point.status == 'pending' and point.observation is None  # labels describe an older image
    assert (point.lng, point.lat) == (151.2, -33.9)                  # planned position fallback
    row['labels']['provenance']['image_sha256'] = 'new'
    assert point_from_row(row).status == 'observed'
    assert point_from_row({**row, 'status': 'no_imagery', 'result': {}}).status == 'no_imagery'


# --- database ----------------------------------------------------------------

def test_migrations_are_idempotent_and_history_is_immutable(db, tmp_path: Path):
    assert migrate(db) == []
    edited = tmp_path / '0001_atlas_schema.sql'
    edited.write_text('SELECT 1;')
    with pytest.raises(SystemExit):
        migrate(db, tmp_path)


def test_publish_grades_sides_and_builds_a_noded_graph(db):
    result = publish(db, grid())
    assert result['changed'] and publish(db, grid())['changed'] is False  # unchanged data is a no-op

    grades = dict(db.execute("""SELECT s.maply_key, d.grade FROM atlas.segment_sides d
                                JOIN atlas.segments s ON s.id = d.segment_id WHERE d.side = 'left'""").fetchall())
    assert grades == {'bottom': 'no_footpath', 'left': 'good', 'top': 'good', 'right': 'good',
                      'north': 'poor', 'lane': 'unknown'}
    assert one(db, "SELECT dominant_surface FROM atlas.segment_sides d JOIN atlas.segments s ON s.id = d.segment_id WHERE maply_key = 'top' AND side = 'left'")[0] == 'concrete'

    # North St's end splits Top St into two edges at a degree-3 node.
    assert one(db, "SELECT count(*) FROM atlas.routing_edges e JOIN atlas.segments s ON s.id = e.segment_id WHERE maply_key = 'top'")[0] == 2
    assert one(db, 'SELECT max(degree) FROM atlas.routing_nodes')[0] == 3
    # Gap Lane joins North St through an 8 m connector.
    kind, length = one(db, "SELECT kind, length_m FROM atlas.routing_edges WHERE kind = 'connector'")
    assert abs(length - 8) < 0.5
    assert one(db, "SELECT count(*) FROM atlas.routing_edges WHERE kind = 'connector'")[0] == 1
    assert one(db, "SELECT count(DISTINCT component) FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM atlas.routing_network')")[0] == 1


def test_wheelchair_route_avoids_street_without_footpath(db):
    publish(db, grid())
    p0, p1 = [one(db, 'SELECT id FROM atlas.routing_nodes ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1', *lnglat(*xy))[0]
              for xy in ((0, 0), (200, 0))]
    route = lambda cost: one(db, f"""SELECT round(sum(n.length_m)), array_agg(DISTINCT s.maply_key ORDER BY s.maply_key)
        FROM pgr_dijkstra('SELECT id, source, target, {cost} AS cost, {cost} AS reverse_cost FROM atlas.routing_network', %s, %s, directed => false) r
        JOIN atlas.routing_network n ON n.id = r.edge JOIN atlas.segments s ON s.id = n.segment_id""", p0, p1)
    assert route('length_m::float8') == (200, ['bottom'])
    assert route('cost') == (400, ['left', 'right', 'top'])


def test_community_observations_close_targets_and_survive_republish(db):
    publish(db, grid())
    targets = dict(db.execute('SELECT reason, count(*) FROM atlas.verification_targets GROUP BY 1').fetchall())
    assert targets == {'no_data': 2, 'uncertain': 2, 'stale': 10}

    point_id, segment_id = one(db, "SELECT p.id, p.segment_id FROM atlas.survey_points p WHERE status = 'no_imagery' AND side = 'left'")
    db.execute("""INSERT INTO atlas.observations (source, survey_point_id, segment_id, side, geom, observed_on, footpath)
                  SELECT 'community', id, segment_id, side, geom, current_date, 'present' FROM atlas.survey_points WHERE id = %s""", (point_id,))
    db.execute("""INSERT INTO atlas.observation_features (observation_id, kind, attrs)
                  SELECT currval(pg_get_serial_sequence('atlas.observations', 'id')), 'footpath_section', %s""",
               ('{"presence": "present", "condition": "no_visible_defect", "surface": "concrete"}',))
    db.execute('SELECT atlas.refresh_derived()')
    assert one(db, 'SELECT count(*) FROM atlas.verification_targets WHERE survey_point_id = %s', point_id)[0] == 0
    assert one(db, "SELECT grade FROM atlas.segment_sides WHERE segment_id = %s AND side = 'left'", segment_id)[0] == 'good'
    db.commit()

    # Republishing a survey that no longer includes Gap Lane keeps the player's data and ids.
    ids = dict(db.execute('SELECT maply_key, id FROM atlas.segments').fetchall())
    smaller = grid()
    smaller.segments = [s for s in smaller.segments if s.key != 'lane']
    smaller.points = [p for p in smaller.points if p.segment_key != 'lane']
    publish(db, smaller)
    assert dict(db.execute('SELECT maply_key, id FROM atlas.segments').fetchall()) == ids
    assert one(db, "SELECT count(*) FROM atlas.observations WHERE source = 'community'")[0] == 1
    assert one(db, "SELECT count(*) FROM atlas.survey_points WHERE segment_id = %s", segment_id)[0] == 0
    assert one(db, "SELECT grade FROM atlas.segment_sides WHERE segment_id = %s AND side = 'left'", segment_id)[0] == 'good'
