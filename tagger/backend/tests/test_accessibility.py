import gzip
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.accessibility import (
    METRICS, Encoder, imagery_month, epoch_minutes, observations, pack_tag, unpack_tag, write_snapshot,
)

JOB_ID = '00000000-0000-0000-0000-000000000001'


def tag(value, visibility='clear', confidence=.9):
    return dict(value=value, confidence=None if value == 'unknown' else confidence, visibility=visibility, evidence='Image evidence')


def labels(*features, footpath='present', reasons=(), **provenance):
    return dict(schema_version='maply-accessibility/1', footpath=tag(footpath), features=list(features), notes=[],
                provenance={'image_sha256': 'current', **provenance}, review_reasons=list(reasons))


def feature(kind='footpath_section', **tags):
    return dict(id='f', kind=kind, region=None, location_description='here', tags={key: tag(value) for key, value in tags.items()})


def shot(payload=None, **changes):
    values = dict(id='00000000-0000-0000-0000-00000000000a', code='P1-L1', segment_key='seg', street='Example',
                  side='left', status='captured', labels=payload, lat=-33.8, lng=151.2, heading=90.4,
                  camera_lat=-33.81, camera_lng=151.21, image_file='images/a.jpg', sha='current',
                  pano_id='pano-1', image_date_iso='2021-03', image_date_type='available',
                  captured_at=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), capture_source='automation')
    return SimpleNamespace(**(values | changes))


def decode_signals(packed):
    return {metric: (packed >> (2 * i)) & 3 for i, metric in enumerate(METRICS)}


def feature_tags(encoder, index):
    names = encoder.tag_names.values
    out = {}
    for packed in encoder.features['tags'][index]:
        name, value, visibility, confidence = unpack_tag(packed)
        out[names[name]] = (encoder.tag_values[names[name]].values[value], visibility, confidence)
    return out


# --- Derived concern rules (unchanged semantics) ----------------------------------

def test_unknown_and_absent_details_are_not_negative_observations():
    result = observations(labels(feature(clear_passage='unknown', surface='unknown')))
    assert result['passage'] == [False, False]
    assert result['surface'] == [False, False]
    assert result['slope'] == [False, False]
    assert observations(labels(footpath='absent'))['footpath'] == [True, True]


def test_clear_passage_and_surface_are_known_without_a_concern():
    result = observations(labels(feature(clear_passage='unobstructed', surface='concrete')))
    assert result['passage'] == [True, False]
    assert result['surface'] == [True, False]


def test_hazards_beside_route_do_not_inflate_concerns():
    result = observations(labels(feature('hazard', type='tree', route_relation='beside_route', clear_passage='blocked')))
    assert result['hazards'] == [False, False]
    assert result['passage'] == [False, False]
    assert observations(labels(feature('hazard', type='tree', route_relation='unknown')))['hazards'] == [False, False]
    assert observations(labels(feature('hazard', type='tree', route_relation='on_route')))['hazards'] == [True, True]


def test_crossing_presence_is_not_confused_with_footpath_presence():
    assert observations(labels(feature(presence='absent')))['crossing'] == [False, False]
    assert observations(labels(feature('kerb_ramp', presence='absent')))['crossing'] == [True, True]
    assert observations(labels(feature('crossing', transition_type='level_or_raised_crossing')))['crossing'] == [True, False]


def test_quality_requires_condition_evidence_not_just_concrete():
    assert observations(labels(feature(surface='concrete')))['quality'] == [False, False]
    assert observations(labels(feature(condition='no_visible_defect')))['quality'] == [True, False]
    assert observations(labels(feature(texture='rough')))['quality'] == [True, True]
    assert observations(labels(feature(continuity='interrupted')))['quality'] == [True, True]
    assert observations(labels(feature('driveway', condition='visible_defect')))['quality'] == [False, False]
    assert observations(labels(footpath='absent'))['quality'] == [False, False]
    assert observations(labels(feature(condition='no_visible_defect'), feature(condition='unknown')))['quality'] == [False, False]


# --- Granular v2 encoding --------------------------------------------------------

def test_tag_packing_round_trips_every_field():
    for args in ((0, 0, 0, 0.0), (28, 31, 7, 1.0), (5, 3, 1, .816), (12, 4, 2, None)):
        name, value, visibility, confidence = unpack_tag(pack_tag(*args))
        assert (name, value, visibility) == args[:3]
        assert confidence == (None if args[3] is None else round(args[3] * 100))


def test_feature_tags_keep_not_recorded_unknown_and_known_distinct():
    payload = labels(dict(id='a', kind='footpath_section', region=None, location_description='x', tags={
        'presence': tag('present'), 'surface': tag('concrete'), 'condition': tag('unknown', 'occluded'),
        'texture': tag('slightly_textured', 'partial', .72)}))
    encoder = Encoder()
    encoder.add_shot(shot(payload))
    tags = feature_tags(encoder, 0)
    assert tags['surface'] == ('concrete', 0, 90)
    assert tags['condition'] == ('unknown', 3, None)
    assert tags['texture'] == ('slightly_textured', 1, 72)
    # Continuity was never recorded for this feature, so it is absent rather than unknown.
    assert 'continuity' not in tags
    assert encoder.features['kind'] == [0] and encoder.features['photo'] == [0]


def test_every_planned_photo_is_listed_with_its_own_annotation_state():
    encoder = Encoder()
    encoder.add_shot(shot(labels(feature(surface='asphalt')), id='a'))
    encoder.add_shot(shot(labels(), id='b', sha='replaced-image'))  # labels describe an older image
    encoder.add_shot(shot(None, id='c', status='no_imagery', image_file=None, sha=None, pano_id=None,
                          image_date_iso=None, image_date_type=None, camera_lat=None, camera_lng=None, captured_at=None))
    encoder.add_shot(shot(labels(), id='d', status='retake'))
    p = encoder.photos
    assert p['id'] == ['a', 'b', 'c', 'd']
    assert p['annotation'] == [1, 2, 0, 2]
    assert p['footpath'] == [0, -1, -1, -1]
    assert p['signals'][1:] == [-1, -1, -1]
    assert encoder.lookups['status'].values[p['status'][2]] == 'no_imagery'
    # Planned position is used only as a labelled fallback.
    assert (p['lng'][2], p['lat'][2], p['position'][2]) == (151.2, -33.8, 1)
    assert (p['lng'][0], p['lat'][0], p['position'][0]) == (151.21, -33.81, 0)
    # Untagged photos contribute no features.
    assert encoder.features['photo'] == [0]


def test_dates_are_separate_and_never_substituted():
    encoder = Encoder()
    encoder.add_shot(shot(labels(tagged_at='2026-11-04T01:41:40+00:00', model='m/one', review_confirmation='automatic')))
    encoder.add_shot(shot(labels(), id='b', image_date_iso='', image_date_type='not_available'))
    encoder.add_shot(shot(labels(), id='c', image_date_iso=None, image_date_type=None, captured_at=None))
    p = encoder.photos
    assert p['imagery_month'] == [2021 * 12 + 2, -1, -1]
    assert [encoder.lookups['imagery_date_type'].values[i] if i >= 0 else None for i in p['imagery_date_type']] == ['available', 'not_available', None]
    assert p['captured'][0] == epoch_minutes(datetime(2026, 11, 2, 10, tzinfo=timezone.utc))
    # A missing capture time stays missing; the imagery or annotation time is not used instead.
    assert p['captured'][2] == -1
    assert p['tagged'] == [epoch_minutes('2026-11-04T01:41:40+00:00'), -1, -1]
    assert encoder.lookups['review_confirmation'].values[p['review_confirmation'][0]] == 'automatic'
    assert p['review_confirmation'][1] == -1


def test_imagery_month_rejects_malformed_values():
    assert imagery_month('2007-11') == 2007 * 12 + 10
    for value in ('', None, 'Mar 2021', '2021-13', '2021', 202103):
        assert imagery_month(value) == -1


def test_signals_and_review_counts_are_per_photo():
    payload = labels(feature(condition='visible_defect', texture='rough'), feature(condition='visible_defect'),
                     reasons=['uncertain boundary', 'f.condition: partial evidence'])
    original = deepcopy(payload)
    encoder = Encoder()
    encoder.add_shot(shot(payload))
    assert payload == original
    signals = decode_signals(encoder.photos['signals'][0])
    assert signals['surface'] == 2 and signals['quality'] == 2 and signals['slope'] == 0 and signals['footpath'] == 1
    assert encoder.photos['review_reasons'] == [2]
    assert len(encoder.features['kind']) == 2


def test_shared_panoramas_get_the_same_group():
    encoder = Encoder()
    for i, pano in enumerate(('p1', 'p2', 'p1', None)):
        encoder.add_shot(shot(labels(), id=str(i), pano_id=pano))
    assert encoder.photos['pano'] == [0, 1, 0, -1]


def test_segment_table_keeps_only_planned_segments_and_osm_context():
    encoder = Encoder()
    encoder.add_shot(shot(labels(), segment_key='keep'))
    table = encoder.segment_table([
        ('skip', 'Other', {'coordinates': [[151.0, -33.0], [151.1, -33.1]]}, 5, {}),
        ('keep', 'Main St', {'coordinates': [[151.1234567, -33.1], [151.2, -33.2]]}, 41.26, {'highway': 'residential', 'osm_tags': {'sidewalk': 'both'}}),
    ])
    assert table['key'] == ['keep']
    assert table['coordinates'] == [[[151.123457, -33.1], [151.2, -33.2]]]
    assert table['length_m'] == [41.3]
    assert encoder.lookups['highway'].values[table['highway'][0]] == 'residential'
    assert encoder.lookups['osm_sidewalk'].values[table['osm_sidewalk'][0]] == 'both'


def test_payload_columns_are_parallel(tmp_path):
    encoder = Encoder()
    encoder.add_shot(shot(labels(feature(surface='concrete'), feature('hazard', type='bin', route_relation='on_route'))))
    encoder.add_shot(shot(None, id='b', status='pending', image_file=None))
    payload = encoder.payload(SimpleNamespace(id=JOB_ID, name='Job', slug='job'), [])
    lengths = {len(column) for column in payload['photos'].values()}
    assert lengths == {2}
    assert {len(column) for column in payload['features'].values()} == {2}
    assert payload['vocabulary']['kind_tags']['hazard'][0] == 'type'
    json.dumps(payload)


def test_snapshot_is_rewritten_only_when_content_changes(tmp_path):
    encoder = Encoder()
    encoder.add_shot(shot(labels()))
    payload = encoder.payload(SimpleNamespace(id=JOB_ID, name='Job', slug='job'), [])
    first = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert write_snapshot(tmp_path, payload, first)
    path = tmp_path / f'{JOB_ID}.json'
    body = path.read_bytes()
    assert json.loads(gzip.decompress((tmp_path / f'{JOB_ID}.json.gz').read_bytes())) == json.loads(body)
    later = datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert not write_snapshot(tmp_path, payload, later)
    meta = json.loads((tmp_path / f'{JOB_ID}.meta.json').read_text())
    assert meta['checked_at'] == later.isoformat() and meta['generated_at'] == first.isoformat()
    assert meta['digest'] == json.loads(body)['digest'] and meta['photos'] == 1
    assert path.read_bytes() == body
    encoder.add_shot(shot(None, id='new', status='pending', image_file=None))
    assert write_snapshot(tmp_path, encoder.payload(SimpleNamespace(id=JOB_ID, name='Job', slug='job'), []), later)
    assert json.loads(path.read_text())['generated_at'] == later.isoformat()
    assert not list(tmp_path.glob('*.tmp'))


def test_snapshot_routes_serve_meta_and_compressed_local_data_without_cache(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, 'data_dir', tmp_path)
    url = f'/assets/accessibility/{JOB_ID}.json'
    with TestClient(main.app) as client:
        assert client.get(url).status_code == 404
        assert client.get(f'/assets/accessibility/{JOB_ID}.meta.json').status_code == 404
        folder = tmp_path / 'accessibility'
        folder.mkdir()
        encoder = Encoder()
        encoder.add_shot(shot(labels()))
        write_snapshot(folder, encoder.payload(SimpleNamespace(id=JOB_ID, name='Job', slug='job'), []))
        response = client.get(url, headers={'Accept-Encoding': 'gzip'})
        assert response.status_code == 200
        assert response.headers['content-encoding'] == 'gzip'
        assert response.json()['photos']['id'] == ['00000000-0000-0000-0000-00000000000a']
        assert response.headers['cache-control'] == 'no-store'
        plain = client.get(url, headers={'Accept-Encoding': 'identity'})
        assert 'content-encoding' not in plain.headers and plain.json()['version'] == 2
        meta = client.get(f'/assets/accessibility/{JOB_ID}.meta.json')
        assert meta.json()['digest'] == plain.json()['digest'] and meta.headers['cache-control'] == 'no-store'
        assert client.get('/assets/accessibility/not-a-job.json').status_code == 422


# --- Real SQL path against an isolated database --------------------------------

@pytest.fixture(scope='module')
def isolated_database():
    import os
    import subprocess
    import sys
    from pathlib import Path
    from sqlalchemy.engine import make_url
    from app.db import configure_engine
    url = os.environ['DATABASE_URL']
    assert 'test' in (make_url(url).database or ''), 'Never use the real dataset for tests'
    try:
        configure_engine(url)
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=Path(__file__).resolve().parents[1],
                       env={**os.environ, 'ALEMBIC_DATABASE_URL': url}, check=True, capture_output=True)
    except Exception as error:  # pragma: no cover - environment without the test database
        pytest.skip(f'isolated test database unavailable: {error}')


def test_sql_snapshot_and_thumbnail_page_from_isolated_database(isolated_database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete
    from app import accessibility, main
    from app.db import session_scope
    from app.models import Job, Segment, Shot

    monkeypatch.setattr(main.settings, 'data_dir', tmp_path)
    with session_scope() as s:
        s.execute(delete(Job))
        job = Job(name='Atlas fixture', slug='atlas fixture', streets={}, params={}, batch_size=50)
        s.add(job)
        s.flush()
        s.add(Segment(job_id=job.id, key='seg', ordinal=0, street='Fixture St', bearing_deg=0, length_m=40,
                      geom='SRID=4326;LINESTRING(151.2 -33.9, 151.201 -33.9)',
                      osm={'highway': 'residential', 'osm_tags': {'sidewalk': 'both'}, 'osm_way_ids': [1, 2]}))
        common = dict(job_id=job.id, batch=1, segment_key='seg', street='Fixture St', kind='street', side='left',
                      view_kind='perpendicular', lat=-33.9, lng=151.2, geom='SRID=4326;POINT(151.2 -33.9)', heading=90,
                      pitch=0, fov=80, url='https://example.com', footpath='west')
        tagged = Shot(code='P1-L1', ordinal=1, filename='a.jpg', status='captured',
                      captured_at=datetime(2026, 11, 2, tzinfo=timezone.utc), capture_source='automation',
                      labels=labels(feature(surface='brick_pavers'), tagged_at='2026-11-03T00:00:00+00:00'),
                      result={'file': 'images/a.jpg', 'thumb': 'thumbs/a.jpg', 'sha256': 'current', 'pano_id': 'p',
                              'image_date_iso': '2019-04', 'camera_lat': -33.9001, 'camera_lng': 151.2001}, **common)
        pending = Shot(code='P2-L1', ordinal=2, filename='b.jpg', status='pending', result={}, **common)
        s.add_all([tagged, pending])
        s.flush()
        payload = accessibility.snapshot(s, job)
        ids = (str(tagged.id), str(pending.id), str(job.id))
    photos = payload['photos']
    assert photos['code'] == ['P1-L1', 'P2-L1'] and photos['annotation'] == [1, 0]
    assert photos['imagery_month'] == [2019 * 12 + 3, -1]
    assert payload['segments']['coordinates'] == [[[151.2, -33.9], [151.201, -33.9]]]
    assert payload['vocabulary']['highway'] == ['residential']
    surface = feature_tags(SimpleNamespace(tag_names=SimpleNamespace(values=payload['vocabulary']['tag_names']),
                                           tag_values={k: SimpleNamespace(values=v) for k, v in payload['vocabulary']['tag_values'].items()},
                                           features=payload['features']), 0)['surface']
    assert surface[0] == 'brick_pavers'
    with TestClient(main.app) as client:
        page = client.get(f'/api/jobs/{ids[2]}/thumbs', params={'ids': f'{ids[0]},{ids[1]}'}).json()
        assert page == {ids[0]: '/files/atlas%20fixture/thumbs/a.jpg?v=current'}
        assert client.get(f'/api/jobs/{ids[2]}/thumbs', params={'ids': 'nope'}).status_code == 422
    with session_scope() as s:
        s.execute(delete(Job))
