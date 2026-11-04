from copy import deepcopy
from types import SimpleNamespace

from app.accessibility import add_photo, empty_counts, observations


def test_snapshot_route_serves_current_local_data_without_cache(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(main.settings, 'data_dir', tmp_path)
    job_id = '00000000-0000-0000-0000-000000000001'
    url = f'/assets/accessibility/{job_id}.json'
    with TestClient(main.app) as client:
        assert client.get(url).status_code == 404
        folder = tmp_path / 'accessibility'
        folder.mkdir()
        payload = {'job_id': job_id, 'points': []}
        (folder / f'{job_id}.json').write_text(json.dumps(payload))
        response = client.get(url)
        assert response.status_code == 200
        assert response.json() == payload
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/assets/accessibility/not-a-job.json').status_code == 422


def tag(value, visibility='clear'):
    return dict(value=value, confidence=None if value == 'unknown' else .9, visibility=visibility, evidence='Image evidence')


def labels(*features, footpath='present'):
    return dict(schema_version='maply-accessibility/1', footpath=tag(footpath), features=list(features),
                provenance={'image_sha256': 'current'}, review_reasons=[])


def feature(kind='footpath_section', **tags):
    return dict(kind=kind, tags={key: tag(value) for key, value in tags.items()})


def photo(payload, **changes):
    values = dict(id='photo', labels=payload, status='captured', image_file='images/photo.png', sha='current')
    return SimpleNamespace(**(values | changes))


def test_multiple_defects_count_as_one_photo():
    payload = labels(feature(condition='visible_defect', texture='rough'), feature(condition='visible_defect'))
    counts = empty_counts()
    add_photo(counts, photo(payload))
    assert counts['tagged'] == 1
    assert counts['metrics']['surface'] == dict(observed=1, flagged=1, examples=['photo'])


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


def test_replaced_image_and_unavailable_photos_are_not_tagged():
    counts = empty_counts()
    payload = labels()
    add_photo(counts, photo(payload, sha='new-image'))
    add_photo(counts, photo(payload, status='retake'))
    add_photo(counts, photo(payload, image_file=None))
    assert (counts['planned'], counts['saved'], counts['tagged']) == (3, 1, 0)


def test_review_counts_and_current_labels_are_recomputed_without_mutating_inputs():
    payload = labels(feature(surface='concrete'))
    payload['review_reasons'] = ['uncertain boundary']
    original = deepcopy(payload)
    counts = empty_counts()
    add_photo(counts, photo(payload))
    assert counts['review'] == 1
    assert payload == original
    payload['features'][0]['tags']['surface'] = tag('loose_gravel')
    refreshed = empty_counts()
    add_photo(refreshed, photo(payload))
    assert refreshed['metrics']['surface']['flagged'] == 1
    assert counts['metrics']['surface']['flagged'] == 0


def test_quality_requires_condition_evidence_not_just_concrete():
    assert observations(labels(feature(surface='concrete')))['quality'] == [False, False]
    assert observations(labels(feature(condition='no_visible_defect')))['quality'] == [True, False]
    assert observations(labels(feature(texture='rough')))['quality'] == [True, True]
    assert observations(labels(feature(continuity='interrupted')))['quality'] == [True, True]
    assert observations(labels(feature('driveway', condition='visible_defect')))['quality'] == [False, False]
    assert observations(labels(footpath='absent'))['quality'] == [False, False]
    assert observations(labels(feature(condition='no_visible_defect'), feature(condition='unknown')))['quality'] == [False, False]


def test_every_current_photo_has_its_own_point_and_actual_camera_position():
    from app.accessibility import photo_point
    rows = [photo(labels(feature(condition='visible_defect')), id=f'photo-{i}',
                  segment_key='same-segment', code=f'P{i}', street='Example', side='left',
                  lat=-33.8, lng=151.2, camera_lat=-33.81, camera_lng=151.21, heading=90) for i in range(3)]
    counts = empty_counts()
    points = [photo_point(row, add_photo(counts, row)) for row in rows]
    assert len(points) == counts['tagged'] == 3
    assert len({p['id'] for p in points}) == 3
    assert all(p['coordinates'] == [151.21, -33.81] and p['position_source'] == 'camera' for p in points)
    assert points[0]['metrics']['quality'] == 1
    assert points[0]['metrics']['slope'] == -1
    rows[0].camera_lat = None
    fallback = photo_point(rows[0], observations(rows[0].labels))
    assert fallback['coordinates'] == [151.2, -33.8]
    assert fallback['position_source'] == 'planned'
