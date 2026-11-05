"""Side-aware graph, policy and endpoint matching on a synthetic town (no database needed)."""
from __future__ import annotations

import threading
from datetime import date

import pytest

from atlas.routing.evidence import classify
from atlas.routing.graph import Graph
from atlas.routing.policy import Policy
from atlas.routing.router import Cancelled, Router, RouteError
from routing_town import AS_OF, NODES, Town, designations, lnglat

ABSENT = 'absent'


def router(town: Town | None = None, policy: Policy = Policy(), doc='default', **kw) -> Router:
    town = town or Town()
    g = Graph(town.snapshot(), policy, designations() if doc == 'default' else doc, AS_OF)
    return Router(g, **kw)


def go(r: Router, a, b) -> dict:
    return r.route(lnglat(*a), lnglat(*b))


def kinds(route) -> list[str]:
    return [leg['kind'] for leg in route['legs']]


def footpaths(route) -> list[tuple]:
    return [(leg['street_name'], leg['side']) for leg in route['legs'] if leg['kind'] == 'footpath_side']


def crossings(route) -> list[dict]:
    return [leg for leg in route['legs'] if leg['kind'] == 'crossing']


def fail(r: Router, a, b) -> RouteError:
    with pytest.raises(RouteError) as info:
        go(r, a, b)
    return info.value


# --- every route is an unverified preview -------------------------------------

def test_every_route_is_an_unverified_preview_without_verified_fields():
    route = go(router(), (130, 6), (170, 6))
    assert route['status'] == 'unverified_preview' and route['navigation_allowed'] is False

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                assert not k.startswith('verified'), k
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        else:
            assert x != 'verified'
    walk(route)


# --- same edge, both directions -----------------------------------------------

def test_same_footpath_side_both_directions():
    r = router()
    there, back = go(r, (130, 6), (170, 6)), go(r, (170, 6), (130, 6))
    assert kinds(there) == ['endpoint_gap', 'footpath_side', 'endpoint_gap']
    assert footpaths(there) == [('Main Street', 'left')] == footpaths(back)
    assert there['summary']['street_m'] == pytest.approx(40, abs=0.5) == back['summary']['street_m']
    a, b = there['legs'][1]['geometry']['coordinates'], back['legs'][1]['geometry']['coordinates']
    assert a[0][0] < a[-1][0] and b[0][0] > b[-1][0]       # drawn in the direction walked
    assert a[0][1] > lnglat(130, 0)[1] and b[0][1] > lnglat(130, 0)[1]   # both on the north side
    assert there['summary']['crossings']['count'] == 0


# --- side continuity: no mid-block crossings ----------------------------------

def test_reaching_the_other_side_needs_a_crossing_at_a_junction_or_street_end():
    route = go(router(), (50, 6), (50, -6))
    assert footpaths(route) == [('Main Street', 'left'), ('Main Street', 'right')]
    (crossing,) = crossings(route)
    assert crossing['crossed_street'] == 'Main Street'
    assert crossing['at'] == pytest.approx(list(lnglat(*NODES['W'])), abs=1e-6)   # turns at the street end
    assert crossing['evidence'] == 'unverified' and crossing['length_m'] is None
    assert any(w['code'] == 'crossing_unverified' and w['severity'] == 'prominent' for w in route['warnings'])


def test_continuing_past_a_degree_two_node_keeps_the_side():
    route = go(router(), (50, 6), (150, 6))     # W-M and M-J are separate atlas segments
    assert footpaths(route) == [('Main Street', 'left')]
    assert route['summary']['crossings']['count'] == 0


# --- corners ------------------------------------------------------------------

def test_turning_a_corner_on_the_same_side_is_free():
    route = go(router(), (150, 6), (194, 50))
    assert footpaths(route) == [('Main Street', 'left'), ('Side Street', 'left')]
    assert route['summary']['crossings']['count'] == 0


def test_going_straight_past_a_side_street_crosses_its_mouth():
    route = go(router(), (150, 6), (250, 6))
    assert [c['crossed_street'] for c in crossings(route)] == ['Side Street']


def test_reaching_the_far_side_of_a_side_street_crosses_it():
    route = go(router(), (150, 6), (206, 50))
    assert [c['crossed_street'] for c in crossings(route)] == ['Side Street']
    assert footpaths(route)[-1] == ('Side Street', 'right')


# --- disconnected -------------------------------------------------------------

def test_disconnected_component_is_no_route():
    err = fail(router(), (50, 306), (50, 6))
    assert (err.status, err.code, err.details) == (404, 'no_route', {
        'reason': 'disconnected', 'origin_in_main_network': False, 'destination_in_main_network': True})


# --- exclusions -----------------------------------------------------------------

def test_known_missing_footpath_is_excluded_even_in_previews():
    town = Town().observe('main2', 'left', 55, footpath=ABSENT)
    r = router(town)
    route = go(r, (50, 6), (250, 6))
    assert ('Main Street', 'left') in footpaths(route)
    lefts = [leg for leg in route['legs'] if leg['kind'] == 'footpath_side' and leg['side'] == 'left']
    assert all(leg['length_m'] < 100 for leg in lefts)       # never walks the north side of M-J
    assert route['summary']['crossings']['count'] == 2
    err = fail(r, (130, 6), (170, 6))
    assert err.code == 'no_eligible_footpath_nearby' and err.details['excluded_nearby'] is True


def test_blocked_by_exclusions_when_no_way_around():
    town = Town().observe('main2', 'left', 55, footpath=ABSENT).observe('main2', 'right', 55, footpath=ABSENT)
    err = fail(router(town), (50, 6), (250, 6))
    assert (err.code, err.details['reason']) == ('no_route', 'blocked_by_exclusions')


def test_flagged_absence_is_uncertain_not_excluded():
    town = Town().observe('main2', 'left', 55, footpath=ABSENT, flagged=True)
    route = go(router(town), (130, 6), (170, 6))
    assert route['summary']['evidence_m']['uncertain_m'] > 0
    assert route['summary']['observations']['flagged_uncertain'] == 1
    w = next(w for w in route['warnings'] if w['code'] == 'flagged_observation')
    assert w['severity'] == 'prominent' and 'footpath absent' in w['message']


def hazard(persistence=None, blocking=True, **extra):
    attrs = {'type': 'bollard', 'route_relation': 'on_route', 'affected_width': 'full_width' if blocking else 'part_of_width',
             'bypass': 'not_visible' if blocking else 'visible', **extra}
    if persistence:
        attrs['persistence'] = persistence
    return [('hazard', attrs)] + [('footpath_section', {'presence': 'present', 'surface': 'concrete'})]


def test_fixed_barrier_excluded():
    town = Town().observe('main2', 'left', 55, features=hazard('fixed'))
    assert fail(router(town), (130, 6), (170, 6)).code == 'no_eligible_footpath_nearby'


def test_steps_are_a_fixed_barrier_but_a_partial_fixed_hazard_is_only_a_warning():
    steps = Town().observe('main2', 'left', 55, features=[('footpath_section', {'presence': 'present', 'interruption': 'steps'})])
    assert fail(router(steps), (130, 6), (170, 6)).code == 'no_eligible_footpath_nearby'
    partial = Town().observe('main2', 'left', 55, features=hazard('fixed', blocking=False))
    route = go(router(partial), (130, 6), (170, 6))
    assert any(w['code'] == 'hazard_observed' for w in route['warnings'])


def test_temporary_obstruction_is_eligible_with_a_prominent_warning():
    town = Town().observe('main2', 'left', 55, features=hazard('temporary', type='construction'))
    route = go(router(town), (130, 6), (170, 6))
    w = next(w for w in route['warnings'] if w['code'] == 'temporary_obstruction_observed')
    assert w['severity'] == 'prominent'


def test_blocking_with_unknown_permanence_is_allowed_with_a_prominent_warning():
    facts = classify('present', False, hazard(None))
    assert facts.fixed_barriers == [] and facts.blocking_permanence_unknown == ['hazard_bollard']
    for flagged in (False, True):
        town = Town().observe('main2', 'left', 55, features=hazard(None), flagged=flagged)
        route = go(router(town), (130, 6), (170, 6))
        w = next(w for w in route['warnings'] if w['code'] == 'blocking_permanence_unknown')
        assert w['severity'] == 'prominent'
    # A footpath "barrier" interruption has no permanence tag at all: never called fixed, never excluded.
    barrier = classify('present', False, [('footpath_section', {'presence': 'present', 'interruption': 'barrier'})])
    assert barrier.fixed_barriers == [] and barrier.blocking_permanence_unknown == ['footpath_barrier']
    town = Town().observe('main2', 'left', 55, features=[('footpath_section', {'presence': 'present', 'interruption': 'construction'})])
    assert any(w['code'] == 'blocking_permanence_unknown' for w in go(router(town), (130, 6), (170, 6))['warnings'])


def test_flagged_fixed_barriers_and_steps_still_exclude_and_keep_the_recheck_reason():
    for features in (hazard('fixed'), [('footpath_section', {'presence': 'present', 'interruption': 'steps'})],
                     [('driveway', {'entry_join': 'step', 'surface': 'concrete'})]):
        town = Town().observe('main2', 'left', 55, features=features, flagged=True)
        r = router(town)
        err = fail(r, (130, 6), (170, 6))
        assert err.code == 'no_eligible_footpath_nearby' and err.details['excluded_reasons'] == ['fixed_barrier_flagged']
        assert r.g.pieces[2]['elements']['left'].excluded == ['fixed_barrier_flagged']
        assert r.g.stats['excluded_footpath_side_m'] == {'fixed_barrier_flagged': 100.0}
    unflagged = router(Town().observe('main2', 'left', 55, features=hazard('fixed')))
    assert fail(unflagged, (130, 6), (170, 6)).details['excluded_reasons'] == ['fixed_barrier']


def test_a_blocking_step_is_excluded_whatever_its_persistence_tag():
    for persistence in (None, 'temporary'):
        facts = classify('present', False, hazard(persistence, type='step'))
        assert facts.fixed_barriers == ['hazard_step'] and facts.blocking_permanence_unknown == []
        town = Town().observe('main2', 'left', 55, features=hazard(persistence, type='step'))
        assert fail(router(town), (130, 6), (170, 6)).code == 'no_eligible_footpath_nearby'


def test_steps_reported_at_a_crossing_exclude_every_crossing_at_that_junction():
    plain = go(router(), (150, 6), (206, 50))
    assert [c['at'] for c in crossings(plain)] == [pytest.approx(list(lnglat(*NODES['J'])), abs=1e-6)]
    for feature in (('crossing', {'crossing_context': 'crossing_approach', 'transition_type': 'steps'}),
                    ('kerb_ramp', {'presence': 'present', 'bottom_join': 'step', 'crossing_context': 'crossing_approach'})):
        for flagged in (False, True):
            town = Town().observe('main3', 'left', 5, features=[feature, ('footpath_section', {'presence': 'present'})],
                                  flagged=flagged)
            r = router(town)
            at_j = [el for el in r.g.elements if el.kind == 'crossing' and el.node_id == 3]
            reason = 'steps_reported_nearby_flagged' if flagged else 'steps_reported_nearby'
            assert at_j and all(el.excluded == [reason] for el in at_j)
            assert r.g.stats['excluded_crossings'] == {reason: len(at_j)}
            route = go(r, (150, 6), (206, 50))       # now crosses Side Street at N instead of J
            assert [c['crossed_street'] for c in crossings(route)] == ['Side Street']
            assert crossings(route)[0]['at'] == pytest.approx(list(lnglat(*NODES['N'])), abs=1e-6)


def test_missing_tags_stay_unknown_rather_than_level_or_fine():
    facts = classify('present', False, [('footpath_section', {'presence': 'present', 'surface': 'concrete'})])
    assert facts.slopes == {} and not facts.has_slope and facts.findings == []
    assert facts.fixed_barriers == [] and facts.blocking_permanence_unknown == [] and not facts.footpath_ends


# --- designated pedestrian / shared streets -----------------------------------

def mall_without_footpath() -> Town:
    town = Town()
    for along in (5, 15, 25, 35, 45, 55, 65, 75, 85, 95):
        town.observe('mall', 'right', along, footpath=ABSENT)
    return town


def test_per_way_pedestrian_designation_allows_missing_footpath():
    route = go(router(mall_without_footpath()), (206, 150), (206, 190))
    assert route['legs'][1]['designation'] == 'designated_pedestrian'
    assert route['summary']['designation_m']['designated_pedestrian_m'] == pytest.approx(40, abs=0.5)
    assert any(w['code'] == 'no_separate_footpath' for w in route['warnings'])


def test_without_trustworthy_designation_the_merged_highway_grants_nothing():
    for doc in (None, {**designations(), 'ways': [w for w in designations()['ways'] if w['name'] != 'Mall']}):
        r = router(mall_without_footpath(), doc=doc)
        assert r.g.pieces[7]['designation'] == 'ambiguous'
        assert fail(r, (206, 150), (206, 190)).code == 'no_eligible_footpath_nearby'


def test_partial_designation_coverage_is_ambiguous():
    doc = designations()
    for w in doc['ways']:
        if w['name'] == 'Mall':
            w['coords'] = [list(lnglat(200, 100)), list(lnglat(200, 150))]      # covers half the piece
    assert router(mall_without_footpath(), doc=doc).g.pieces[7]['designation'] == 'ambiguous'


# --- endpoints and gaps ---------------------------------------------------------

def test_endpoint_gap_is_reported_and_never_part_of_the_route():
    route = go(router(), (130, 30), (170, 6))
    gap = route['legs'][0]
    assert gap['kind'] == 'endpoint_gap' and gap['evidence'] == 'not_assessed'
    assert gap['length_m'] == pytest.approx(26, abs=0.5)
    assert route['summary']['endpoint_gap_m'] == pytest.approx(26 + 2, abs=1)
    assert route['summary']['route_m'] == pytest.approx(40, abs=0.5)


def test_too_far_and_outside_coverage():
    err = fail(router(), (130, 50), (170, 6))
    assert (err.code, err.details['which'], err.details['max_m']) == ('too_far_from_network', 'origin', 40.0)
    err = fail(router(), (130, 6), (500, 0))
    assert (err.code, err.details) == ('outside_coverage', {'which': 'destination'})


def test_matching_never_jumps_across_a_street():
    # The north side of M-J is excluded; Lower Lane is 38 m away but across Main Street.
    town = Town().observe('main2', 'left', 50, footpath=ABSENT)
    err = fail(router(town), (150, 8), (170, 6))
    assert err.code == 'no_eligible_footpath_nearby'
    # From the south side, Main Street's south footpath is nearer than Lower Lane.
    route = go(router(), (150, -10), (170, -6))
    assert route['endpoints']['origin']['street_name'] == 'Main Street' and route['endpoints']['origin']['side'] == 'right'


def test_point_on_the_centreline_is_flagged_side_ambiguous():
    route = go(router(), (130, 0.1), (170, 6))
    assert route['endpoints']['origin']['side_ambiguous'] is True
    assert any(w['code'] == 'endpoint_side_ambiguous' for w in route['warnings'])


def test_same_location():
    assert fail(router(), (130, 6), (131, 6)).code == 'same_location'


# --- connectors -----------------------------------------------------------------

def test_connectors_are_inferred_and_warned():
    town = Town()
    town.connectors.append(('I2', 'P'))
    route = go(router(town), (50, 306), (206, 150))
    (connector,) = [leg for leg in route['legs'] if leg['kind'] == 'connector']
    assert connector['evidence'] == 'inferred'
    assert route['summary']['connector_m'] == pytest.approx(141.4, abs=0.5)
    assert 'connectors_inferred' in route['limits']
    assert any(w['code'] == 'connector_inferred' for w in route['warnings'])


# --- reporting: partitions, imagery age, slopes ---------------------------------

def test_lengths_partition_the_street_length():
    town = Town()
    town.observe_every = 30.0
    town.observe('main3', 'left', 35, flagged=True).observe('main3', 'left', 65, on=date(2019, 5, 1))
    town.observe('main3', 'left', 95, on=None)
    route = go(router(town), (110, 6), (290, 6))
    s = route['summary']
    for part in ('evidence_m', 'designation_m', 'imagery_age_m'):
        assert sum(s[part].values()) == pytest.approx(s['street_m'], abs=0.4), part
    assert s['evidence_m']['unknown_m'] > 0 and s['evidence_m']['uncertain_m'] > 0
    assert s['imagery_age_m']['older_than_3y_m'] > 0
    for leg in route['legs']:
        if leg['kind'] == 'footpath_side':
            assert sum(leg['evidence_m'].values()) == pytest.approx(leg['length_m'], abs=0.3)


def test_undated_imagery_is_reported_separately():
    town = Town()
    town.observed_on = None
    route = go(router(town), (130, 6), (170, 6))
    assert route['summary']['imagery_age_m']['undated_m'] == pytest.approx(40, abs=0.5)
    assert route['summary']['imagery_months'] is None


def test_steep_slope_warns_prominently_and_never_changes_the_route():
    feats = [('footpath_section', {'presence': 'present', 'surface': 'concrete', 'longitudinal_slope': 'steep',
                                   'cross_slope': 'apparently_level'})]
    plain = go(router(), (150, 6), (250, 6))
    steep_town = Town().observe('main2', 'left', 75, features=feats)
    steep = go(router(steep_town), (150, 6), (250, 6))
    assert steep['geometry'] == plain['geometry'] and steep['summary']['route_m'] == plain['summary']['route_m']
    w = next(w for w in steep['warnings'] if w['code'] == 'steep_slope_observed')
    assert w['severity'] == 'prominent' and 'not measured' in w['message']
    slopes = steep['slope_observations']
    assert slopes['unit'] == 'feature_observations'
    assert slopes['footpath']['longitudinal']['steep'] == 1 and slopes['footpath']['cross']['apparently_level'] == 1
    assert slopes['observations_without_slope'] == steep['summary']['observations']['count'] - 1


# --- limits ---------------------------------------------------------------------

def test_timeout_and_cancellation():
    with pytest.raises(RouteError) as info:
        go(router(timeout_s=0.0), (50, 6), (250, 6))
    assert info.value.code == 'route_timeout' and info.value.status == 504
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        router().route(lnglat(50, 6), lnglat(250, 6), cancel)


def test_graph_stats_describe_the_model():
    g = router().g
    assert g.stats['crossings'] > 0 and g.stats['designation']['available'] is True
    assert g.stats['designation']['matched_share'] == pytest.approx(1.0, abs=0.02)
    assert g.stats['designation']['designated_pedestrian_m'] == pytest.approx(100, abs=0.5)
