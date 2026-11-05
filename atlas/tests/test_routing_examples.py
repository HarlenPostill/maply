"""docs/routing-examples/*.json are produced by the real API from the synthetic town.

UPDATE_ROUTING_EXAMPLES=1 uv run pytest tests/test_routing_examples.py  rewrites them;
otherwise the test fails when the files no longer match what the API returns.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from atlas.routing.api import create_app
from atlas.routing.service import Service
from routing_town import AS_OF, Town, designations, ll

EXAMPLES = Path(__file__).resolve().parents[1] / 'docs' / 'routing-examples'


def example_town() -> Town:
    town = Town()
    town.observe_every = 15.0
    steep = [('footpath_section', {'presence': 'present', 'surface': 'concrete', 'longitudinal_slope': 'steep',
                                   'cross_slope': 'apparently_level'})]
    town.observe('main2', 'left', 50, features=steep, on=date(2021, 3, 1))
    town.observe('main2', 'left', 65, on=date(2019, 6, 1))
    town.observe('side', 'right', 35, flagged=True, on=None)
    town.observe('side', 'right', 20, features=[('hazard', {'type': 'construction', 'route_relation': 'partially_encroaching',
                                                            'affected_width': 'full_width', 'bypass': 'not_visible',
                                                            'persistence': 'temporary'}),
                                                ('footpath_section', {'presence': 'present', 'surface': 'concrete'})])
    town.observe('main3', 'left', 5, features=[('kerb_ramp', {'presence': 'present', 'bottom_join': 'apparent_flush',
                                                              'slope': 'gentle', 'crossing_context': 'crossing_approach'}),
                                               ('footpath_section', {'presence': 'present', 'surface': 'concrete'})])
    return town


def generated() -> dict[str, object]:
    svc = Service(lambda: example_town().snapshot(), designations(), today=lambda: AS_OF)
    svc.refresh()
    client = TestClient(create_app(svc))
    version = client.get('/v1/health').json()['graph_version']
    request = {'origin': ll(130, 20), 'destination': ll(206, 60), 'expected_graph_version': version}
    post = lambda b: client.post('/v1/route-preview', json=b).json()
    errors = {
        'outside_coverage': post({'origin': ll(130, 20), 'destination': ll(500, 0)}),
        'too_far_from_network': post({'origin': ll(130, 50), 'destination': ll(206, 60)}),
        'no_route_disconnected': post({'origin': ll(50, 306), 'destination': ll(206, 60)}),
        'same_location': post({'origin': ll(130, 20), 'destination': ll(131, 20)}),
        'snapshot_changed': post({**request, 'expected_graph_version': 'r1-000000000000'}),
        'invalid_request': post({'origin': ll(130, 20)}),
    }
    loading = TestClient(create_app(Service(lambda: example_town().snapshot()))).get('/v1/coverage').json()
    errors['data_unavailable'] = loading
    return {'coverage.json': client.get('/v1/coverage').json(), 'route-preview-request.json': request,
            'route-preview.json': post(request), 'errors.json': errors}


def test_examples_match_the_api():
    files = generated()
    if os.environ.get('UPDATE_ROUTING_EXAMPLES') == '1':
        EXAMPLES.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (EXAMPLES / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    for name, data in files.items():
        stored = json.loads((EXAMPLES / name).read_text())
        if name == 'coverage.json':          # loaded_at is the only field that changes between runs
            stored['snapshot'].pop('loaded_at', None)
            data = json.loads(json.dumps(data))
            data['snapshot'].pop('loaded_at', None)
        assert stored == json.loads(json.dumps(data)), f'{name} is out of date: UPDATE_ROUTING_EXAMPLES=1 uv run pytest {__file__}'
