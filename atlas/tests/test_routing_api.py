"""HTTP contract: typed errors, limits, snapshot integrity markers, no request data in logs."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from atlas.routing.api import create_app
from atlas.routing.service import Service
from atlas.routing.snapshot import SnapshotError
from routing_town import AS_OF, Town, designations, ll


def service(town: Town | None = None) -> Service:
    svc = Service(lambda: (town or Town()).snapshot(), designations(), today=lambda: AS_OF)
    svc.refresh()
    return svc


@pytest.fixture
def client():
    return TestClient(create_app(service()))


def body(origin=(130, 6), destination=(170, 6), **extra):
    return {'origin': ll(*origin), 'destination': ll(*destination), **extra}


def code(response):
    return response.json()['error']['code']


def test_health_and_coverage(client):
    health = client.get('/v1/health').json()
    assert health['status'] == 'ready' and health['graph_version'].startswith('r1-')
    cov = client.get('/v1/coverage').json()
    assert cov['navigation_allowed'] is False and cov['area']['enforcement'] == 'strict' and cov['area']['approximate'] is True
    assert cov['snap_max_m'] == 40.0 and cov['release']['id'] == 1
    assert cov['policy']['pending_decisions'] == []
    assert cov['policy']['settled']['fixed_barrier'] == 'excluded_even_if_flagged'
    assert cov['policy']['settled']['blocking_permanence_unknown'] == 'eligible_with_prominent_warning'
    assert {lim['code'] for lim in cov['limits']} >= {'preview_only', 'crossings_inferred_at_junctions', 'width_not_measured'}
    assert 'designation_unavailable' not in {lim['code'] for lim in cov['limits']}


def test_route_preview(client):
    r = client.post('/v1/route-preview', json=body())
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-store'
    data = r.json()
    assert data['status'] == 'unverified_preview' and data['navigation_allowed'] is False
    assert data['graph_version'] == client.get('/v1/health').json()['graph_version']


def test_typed_request_errors(client):
    assert code(client.post('/v1/route-preview', content='{}', headers={'content-type': 'text/plain'})) == 'unsupported_media_type'
    big = client.post('/v1/route-preview', content='{"x": "' + 'a' * 5000 + '"}', headers={'content-type': 'application/json'})
    assert big.status_code == 413 and code(big) == 'payload_too_large'
    bad = client.post('/v1/route-preview', json={**body(), 'allow_unverified': True})
    assert bad.status_code == 400 and code(bad) == 'invalid_request' and bad.json()['error']['details']['fields'] == ['allow_unverified']
    lat = client.post('/v1/route-preview', json={'origin': {'lat': 99, 'lng': 151.2}, 'destination': ll(170, 6)})
    assert lat.status_code == 400 and lat.json()['error']['details']['fields'] == ['origin.lat']
    junk = client.post('/v1/route-preview', content='not json', headers={'content-type': 'application/json'})
    assert junk.status_code == 400
    nan = client.post('/v1/route-preview', content='{"origin": {"lat": NaN, "lng": 151.2}, "destination": {"lat": -33.9, "lng": 151.2}}',
                      headers={'content-type': 'application/json'})
    assert nan.status_code == 400
    assert code(client.get('/v1/nope')) == 'not_found'
    assert client.get('/v1/route-preview').status_code == 405 and code(client.get('/v1/route-preview')) == 'method_not_allowed'


def test_routing_errors_are_typed(client):
    r = client.post('/v1/route-preview', json=body(destination=(500, 0)))
    assert r.status_code == 422 and r.json()['error'] == {
        'code': 'outside_coverage', 'message': 'The destination is outside the surveyed area.', 'details': {'which': 'destination'}}
    assert code(client.post('/v1/route-preview', json=body(origin=(130, 50)))) == 'too_far_from_network'
    r = client.post('/v1/route-preview', json=body(origin=(50, 306)))
    assert r.status_code == 404 and r.json()['error']['details']['reason'] == 'disconnected'
    assert code(client.post('/v1/route-preview', json=body(destination=(131, 6)))) == 'same_location'


def test_snapshot_changed(client):
    r = client.post('/v1/route-preview', json=body(expected_graph_version='r1-000000000000'))
    assert r.status_code == 409 and code(r) == 'snapshot_changed'
    current = client.get('/v1/health').json()['graph_version']
    assert client.post('/v1/route-preview', json=body(expected_graph_version=current)).status_code == 200


def test_content_hash_tracks_the_data():
    a = Town().snapshot().content_hash
    b = Town().observe('main2', 'left', 55, footpath='absent').snapshot().content_hash
    assert a != b and a == Town().snapshot().content_hash


def test_unavailable_until_loaded_and_after_integrity_failure():
    svc = Service(lambda: Town().snapshot(), designations(), today=lambda: AS_OF)
    client = TestClient(create_app(svc))
    r = client.get('/v1/coverage')
    assert r.status_code == 503 and r.headers['retry-after'] == '30' and r.json()['error']['details'] == {'reason': 'loading'}
    assert code(client.post('/v1/route-preview', json=body())) == 'data_unavailable'

    def broken():
        raise SnapshotError('integrity_failed', 'Release 1 does not match the tables: feature counts.')
    svc._load = broken
    svc.refresh()
    assert client.get('/v1/health').json()['error']['details'] == {'reason': 'integrity_failed'}


def test_busy():
    client = TestClient(create_app(service(), max_concurrency=1))
    sem = client.app.state.route_slots
    sem.acquire()                      # hold the only route search slot
    try:
        r = client.post('/v1/route-preview', json=body())
        assert r.status_code == 503 and code(r) == 'busy' and r.headers['retry-after'] == '2'
    finally:
        sem.release()
    assert client.post('/v1/route-preview', json=body()).status_code == 200


def test_logs_hold_no_coordinates(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.post('/v1/route-preview', json=body())
    client.post('/v1/route-preview', json=body(destination=(500, 0)))
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'route-preview' in text and 'outside_coverage' in text
    origin = ll(130, 6)
    for value in (origin['lat'], origin['lng']):
        assert f'{value:.4f}'[:7] not in text
    assert '151.2' not in text and '-33.9' not in text


def test_unknown_paths_never_enter_application_logs(client, caplog):
    caplog.set_level(logging.INFO, logger='atlas.api')
    assert client.get('/unknown/-33.8731,151.2069%0Aprivacy-probe').status_code == 404
    lines = [r.getMessage() for r in caplog.records if r.name == 'atlas.api']
    assert any('unknown_endpoint' in line for line in lines)
    assert all('-33.8731' not in line and 'privacy-probe' not in line and '\n' not in line for line in lines)
