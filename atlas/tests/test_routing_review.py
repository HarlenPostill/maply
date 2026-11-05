"""Reproductions for the independent review findings (designation ties, gap contact,
effective graph version, reload detection, date and designation rollover, disconnects)."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from atlas.routing import designation
from atlas.routing.api import create_app
from atlas.routing.geo import SegmentGrid, gap_blocked
from atlas.routing.graph import Graph
from atlas.routing.policy import Policy
from atlas.routing.router import Cancelled, Router, RouteError
from atlas.routing.service import Service
from routing_town import AS_OF, NODES, Town, designations, ll, lnglat

MALL_WAY = 106


def mall_without_footpath() -> Town:
    town = Town()
    for along in range(5, 100, 10):
        town.observe('mall', 'right', along, footpath='absent')
    return town


def build(town=None, doc='default', policy=Policy(), as_of=AS_OF) -> Graph:
    return Graph((town or Town()).snapshot(), policy, designations() if doc == 'default' else doc, as_of)


# --- P1: conflicting same-name ways never grant the allowance ---------------------

@pytest.mark.parametrize('copy_first', [False, True])
def test_conflicting_way_classes_on_the_same_geometry_are_ambiguous(copy_first):
    doc = designations()
    mall = next(w for w in doc['ways'] if w['name'] == 'Mall')
    copy = {**mall, 'way_id': 999, 'highway': 'residential'}
    doc['ways'] = [copy, *doc['ways']] if copy_first else [*doc['ways'], copy]
    g = build(mall_without_footpath(), doc)
    piece = g.pieces[7]
    assert (piece['designation'], piece['designation_basis']) == ('ambiguous', 'conflicting_ways')
    assert piece['osm_way_ids'] == [MALL_WAY, 999]
    with pytest.raises(RouteError) as info:
        Router(g).route(lnglat(206, 150), lnglat(206, 190))
    assert info.value.code == 'no_eligible_footpath_nearby'
    assert info.value.details['excluded_reasons'] == ['footpath_absent']


def test_designated_legs_carry_their_way_ids_and_basis():
    route = Router(build(mall_without_footpath())).route(lnglat(206, 150), lnglat(206, 190))
    leg = route['legs'][1]
    assert (leg['designation'], leg['designation_basis'], leg['osm_way_ids']) == ('designated_pedestrian', 'per_way_match', [MALL_WAY])


def test_a_same_name_way_that_only_starts_at_the_junction_does_not_count():
    doc = designations()
    # A residential "Mall" way leaving N southwards: it touches the Mall piece only at N.
    doc['ways'].append({'way_id': 998, 'name': 'Mall', 'highway': 'residential',
                        'coords': [list(lnglat(200, 100)), list(lnglat(200, 40))]})
    piece = build(mall_without_footpath(), doc).pieces[7]
    assert (piece['designation'], piece['osm_way_ids']) == ('designated_pedestrian', [MALL_WAY])


def test_duplicate_ways_of_the_same_class_agree():
    doc = designations()
    mall = next(w for w in doc['ways'] if w['name'] == 'Mall')
    doc['ways'].append({**mall, 'way_id': 997})
    piece = build(mall_without_footpath(), doc).pieces[7]
    assert (piece['designation'], piece['osm_way_ids']) == ('designated_pedestrian', [MALL_WAY, 997])


# --- P1: an endpoint gap may not pass through a street's interior vertex ---------

def test_gap_through_an_interior_street_vertex_is_blocked():
    grid = SegmentGrid()
    grid.add_line([(-1.0, 0.0), (0.0, 0.0), (1.0, 0.0)], 1)
    stub = SimpleNamespace(g=SimpleNamespace(piece_grid=grid))
    assert Router._gap_crosses_street(stub, (0.0, 1.0), (0.0, -1.0), 99) is True       # the reviewer's case
    assert Router._gap_crosses_street(stub, (0.5, 1.0), (0.5, -1.0), 99) is True       # ordinary crossing
    assert Router._gap_crosses_street(stub, (0.0, 1.0), (0.0, -1.0), 1) is False       # own street is skipped
    assert Router._gap_crosses_street(stub, (0.0, 1.0), (0.0, 0.0), 99) is False       # terminal contact at the match
    assert Router._gap_crosses_street(stub, (0.0, 0.0), (0.0, 1.0), 99) is False       # starts on a street, leaves it
    assert Router._gap_crosses_street(stub, (0.0, 1.0), (0.0, 0.01), 99) is False      # ends within the terminal tolerance
    assert Router._gap_crosses_street(stub, (0.0, 1.0), (0.0, -0.2), 99) is True       # passes the vertex, then stops
    assert gap_blocked((-2.0, 0.0), (2.0, 0.0), [((-1.0, 0.0), (1.0, 0.0))]) is True     # runs along a street


def test_matching_never_jumps_through_a_street_vertex():
    town = Town()
    town.vertices['main2'] = [(150.0, 0.0)]          # Main Street has a vertex right under the gap
    route = Router(build(town)).route(lnglat(150, 8), lnglat(150, -26))
    origin = route['endpoints']['origin']
    assert (origin['street_name'], origin['side']) == ('Main Street', 'left')    # not Lower Lane across Main Street


# --- P2: graph_version covers every effective input -------------------------------

def test_graph_version_changes_with_every_input():
    base = build().version
    assert build().version == base
    doc = designations()
    doc['ways'][6] = {**doc['ways'][6], 'highway': 'living_street'}
    moved = Town()
    moved.moved['NE'] = (300.0, 101.0)
    attrs = Town().observe('main2', 'left', 55, features=[('footpath_section', {'presence': 'present', 'surface': 'asphalt'})])
    position = Town().observe('main2', 'left', 55.5)
    flagged = Town().observe('main2', 'left', 55, flagged=True)
    variants = {
        'designations': build(doc=doc).version,
        'no designations': build(doc=None).version,
        'policy': build(policy=Policy(crossing_penalty_m=31)).version,
        'as-of date': build(as_of=date(2026, 11, 6)).version,
        'node geometry': build(moved).version,
        'feature attrs': build(attrs).version,
        'observation position': build(position).version,
        'needs_verification': build(flagged).version,
    }
    assert all(v != base for v in variants.values()), variants
    assert len(set(variants.values())) == len(variants)


# --- P2: service rebuilds on date rollover, designation change and data change ----

def test_service_rebuilds_on_date_rollover_and_designation_change_and_reloads_on_marker():
    today = [AS_OF]
    docs = [designations()]
    marker = [('m1',)]
    town = Town()
    loads = []

    def load():
        snap = town.snapshot()
        snap.marker = marker[0]
        loads.append(snap)
        return snap
    svc = Service(load, lambda: docs[0], today=lambda: today[0], marker=lambda: marker[0])
    assert svc.check() == 'loaded'
    v0 = svc.state.graph.version
    assert svc.check() == 'unchanged' and svc.state.graph.version == v0 and len(loads) == 1

    today[0] = date(2026, 11, 6)
    assert svc.check() == 'rebuilt' and len(loads) == 1          # no database reload for a date change
    v1 = svc.state.graph.version
    assert v1 != v0 and svc.state.graph.as_of == date(2026, 11, 6)

    doc = designations()
    doc['ways'] = [w for w in doc['ways'] if w['name'] != 'Mall']
    docs[0] = doc
    assert svc.check() == 'rebuilt' and len(loads) == 1
    v2 = svc.state.graph.version
    assert v2 not in (v0, v1) and svc.state.graph.pieces[7]['designation'] == 'ambiguous'

    town.observe('main2', 'left', 55, footpath='absent')
    marker[0] = ('m2',)
    assert svc.check() == 'reloaded' and len(loads) == 2
    assert svc.state.graph.version not in (v0, v1, v2)
    assert svc.state.graph.pieces[2]['elements']['left'].excluded == ['footpath_absent']


def test_unreadable_designations_keep_the_current_graph():
    calls = [designations()]

    def source():
        if calls[0] is None:
            raise ValueError('half-written file')
        return calls[0]
    svc = Service(lambda: Town().snapshot(), source, today=lambda: AS_OF)
    svc.check()
    version = svc.state.graph.version
    calls[0] = None
    assert svc.check() == 'unchanged' and svc.state.graph.version == version


def test_file_source_rereads_a_changed_file(tmp_path):
    path = tmp_path / 'designations.json'
    src = designation.FileSource(path)
    assert src.current() is None
    path.write_text(json.dumps(designations()))
    first = src.current()
    assert first is not None and src.current() is first           # cached while unchanged
    doc = designations()
    doc['ways'] = doc['ways'][:3]
    path.write_text(json.dumps(doc))
    import os
    os.utime(path, ns=(1, 1))                                      # make sure the stamp changes
    assert len(src.current()['ways']) == 3
    assert designation.content_digest(first) != designation.content_digest(src.current())


@pytest.mark.parametrize('initially_ready', [False, True])
def test_background_refresh_recovers_from_invalid_designations(initially_ready):
    attempted = threading.Event()
    recovered = threading.Event()
    docs = [designations()]

    def source():
        if docs[0] is None:
            attempted.set()
            raise ValueError('unreadable designation file')
        return docs[0]

    svc = Service(lambda: Town().snapshot(), source, today=lambda: AS_OF)
    if initially_ready:
        svc.refresh()
    original = svc.state
    docs[0] = {'ways': [{}]} if initially_ready else None
    check = svc.check

    def observed_check():
        try:
            result = check()
        except Exception:
            attempted.set()
            raise
        if svc.state.graph is not None and svc.state is not original:
            recovered.set()
        return result

    svc.check = observed_check
    thread = svc.run_background(0.01)
    try:
        assert attempted.wait(2)
        assert thread.is_alive() and svc.state is original
        doc = designations()
        doc['ways'] = doc['ways'][:3]
        docs[0] = doc
        assert recovered.wait(2)
        assert svc.state.graph is not None and thread.is_alive()
    finally:
        svc.stop()
        thread.join(2)
    assert not thread.is_alive()


# --- optional: real deadline expiry and client disconnect -------------------------

def test_deadline_expires_mid_search():
    g = build()
    ticks = iter([0.0] + [10.0] * 1000)
    import atlas.routing.router as router_mod
    real = router_mod.time.monotonic
    router_mod.time.monotonic = lambda: next(ticks)
    try:
        with pytest.raises(RouteError) as info:
            Router(g, timeout_s=3.0).route(lnglat(50, 6), lnglat(250, 6))
    finally:
        router_mod.time.monotonic = real
    assert info.value.code == 'route_timeout'


def test_client_disconnect_cancels_the_search():
    svc = Service(lambda: Town().snapshot(), designations(), today=lambda: AS_OF)
    svc.refresh()
    seen = {}

    def slow_route(origin, destination, cancel):
        seen['cancel'] = cancel
        if not cancel.wait(5):
            raise AssertionError('never cancelled')
        raise Cancelled()
    svc.state.router.route = slow_route
    app = create_app(svc)
    body = json.dumps({'origin': ll(130, 6), 'destination': ll(170, 6)}).encode()
    messages = [{'type': 'http.request', 'body': body, 'more_body': False}]
    sent = []

    async def receive():
        # Like uvicorn: once the client has gone, a pending disconnect is delivered immediately.
        if messages:
            return messages.pop(0)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
             'path': '/v1/route-preview', 'raw_path': b'/v1/route-preview', 'query_string': b'', 'root_path': '',
             'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())],
             'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 8765)}
    asyncio.run(asyncio.wait_for(app(scope, receive, send), 5))
    assert seen['cancel'].is_set()
    start = next(m for m in sent if m['type'] == 'http.response.start')
    assert start['status'] == 499
    assert app.state.route_slots.acquire(blocking=False)              # the slot was released
    app.state.route_slots.release()
