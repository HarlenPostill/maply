"""Endpoint matching, bounded shortest-path search and the route preview response."""
from __future__ import annotations

import heapq
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

from atlas.routing.evidence import SLOPE_CATEGORIES
from atlas.routing.geo import XY, dist, gap_blocked, locate, offset, substring
from atlas.routing.graph import Element, Graph
from atlas.routing.policy import API_VERSION, LIMITS

SLOPE_SCHEMA = {'footpath': ('longitudinal', 'cross'), 'driveway': ('longitudinal', 'cross'),
                'kerb_ramp': ('slope', 'longitudinal', 'cross'), 'crossing': ('longitudinal', 'cross')}
MAX_WARNINGS = 300


class RouteError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


class Cancelled(Exception):
    pass


@dataclass
class Candidate:
    which: str
    element: Element | None          # footpath side element, or None for a corner
    corner: int | None
    along: float                     # metres along the element
    gap_m: float
    matched: XY
    requested: XY
    side_ambiguous: bool
    street_name: str | None = None
    side: str | None = None
    facing: str | None = None
    centre_m: float = 0.0            # distance from the point to the street centreline (or node)


@dataclass
class Step:
    element: Element | None
    a: float
    b: float
    reverse: bool = False             # for flat elements: traversed v -> u


def _r(x):
    return None if x is None else round(x, 1)


class Router:
    def __init__(self, graph: Graph, timeout_s: float = 3.0, max_settled: int = 2_000_000):
        self.g, self.policy = graph, graph.policy
        self.timeout_s, self.max_settled = timeout_s, max_settled

    # -------------------------------------------------------------- matching

    def candidates(self, which: str, lng: float, lat: float) -> list[Candidate]:
        g, policy = self.g, self.policy
        if not g.in_coverage(lng, lat):
            raise RouteError(422, 'outside_coverage', f'The {which} is outside the surveyed area.', {'which': which})
        p = g.proj.xy(lng, lat)
        seen, any_near, excluded_near, found = set(), False, False, []
        nearest_excluded = math.inf
        excluded_reasons: set[str] = set()
        for _, _, edge_id in g.piece_grid.near(p, policy.snap_max_m):
            if edge_id in seen:
                continue
            seen.add(edge_id)
            piece = g.pieces[edge_id]
            loc = locate(piece['line'], piece['cum'], p)
            if loc.distance > policy.snap_max_m:
                continue
            any_near = True
            if self._gap_crosses_street(p, loc.point, edge_id):
                excluded_near = True
                continue
            if loc.clamped:
                found.extend(self._corner_candidates(which, p, piece, loc))
                continue
            sides = ['left', 'right'] if loc.distance < policy.side_ambiguous_m else ['left' if loc.cross > 0 else 'right']
            for side in sides:
                el = piece['elements'][side]
                if el.excluded:
                    excluded_near = True
                    nearest_excluded = min(nearest_excluded, loc.distance)
                    excluded_reasons.update(el.excluded)
                    continue
                shown = offset(substring(el.line, el.cum, max(loc.along - 0.5, 0), min(loc.along + 0.5, el.cum[-1])),
                               policy.display_offset_m if side == 'left' else -policy.display_offset_m)
                matched = ((shown[0][0] + shown[-1][0]) / 2, (shown[0][1] + shown[-1][1]) / 2)
                found.append(Candidate(which, el, None, loc.along * el.scale, dist(p, matched), matched, p,
                                       len(sides) == 2, el.street_name, side, el.facing, loc.distance))
        # Never bypass an excluded footpath beside the point by matching to one farther away.
        found = [c for c in found if c.centre_m <= nearest_excluded + policy.excluded_bypass_m]
        if not found:
            if not any_near:
                raise RouteError(422, 'too_far_from_network', f'No surveyed street within {policy.snap_max_m:g} m of the {which}.',
                                 {'which': which, 'max_m': policy.snap_max_m})
            raise RouteError(422, 'no_eligible_footpath_nearby',
                             f'Footpath near the {which} is excluded, or reachable only across a street.',
                             {'which': which, 'max_m': policy.snap_max_m, 'excluded_nearby': excluded_near,
                              'excluded_reasons': sorted(excluded_reasons)})
        found.sort(key=lambda c: (c.gap_m, c.element.index if c.element else -1, c.corner or -1))
        return found[:8]

    def _corner_candidates(self, which, p, piece, loc) -> list[Candidate]:
        """The point lies beyond a street end: match it to the corner whose angle contains it."""
        g = self.g
        e = piece['edge']
        node = e.source if loc.along <= 1e-6 else e.target
        ends = g.node_street_ends[node]
        corners = g.node_corners[node]
        q = g.node_xy[node]
        if len(ends) == 1:
            chosen = list(corners)          # beyond a dead end: both sides, flagged ambiguous
        else:
            angle = math.atan2(p[1] - q[1], p[0] - q[0])
            chosen = [corners[g._sector(ends, angle)]]
        out = []
        for c in chosen:
            if not any(not g.elements[i].excluded for i in g.adj.get(c, [])):
                continue
            out.append(Candidate(which, None, c, 0.0, dist(p, q), q, p, len(chosen) > 1,
                                 piece['street_name'], None, None, dist(p, q)))
        return out

    def _gap_crosses_street(self, p: XY, q: XY, own_edge: int) -> bool:
        return gap_blocked(p, q, ((a, b) for a, b, edge_id in self.g.piece_grid.along(p, q) if edge_id != own_edge))

    # ---------------------------------------------------------------- search

    def route(self, origin: tuple[float, float], destination: tuple[float, float], cancel=None) -> dict:
        g, policy = self.g, self.policy
        start = time.monotonic()
        po, pd = g.proj.xy(*origin), g.proj.xy(*destination)
        if dist(po, pd) < 5.0:
            raise RouteError(422, 'same_location', 'The origin and destination are the same place.',
                             {'distance_m': round(dist(po, pd), 1)})
        origins = self.candidates('origin', *origin)
        destinations = self.candidates('destination', *destination)

        S, T = -1, -2
        out_links: dict[int, list] = defaultdict(list)   # node -> [(target, cost, step)]
        start_links = []
        for c in origins:
            w = policy.gap_weight * c.gap_m
            if c.element is None:
                start_links.append((c.corner, w, None, c))
            else:
                el = c.element
                start_links.append((el.u, w + el.cost_between(policy, c.along, 0), Step(el, c.along, 0), c))
                start_links.append((el.v, w + el.cost_between(policy, c.along, el.length_m), Step(el, c.along, el.length_m), c))
        for c in destinations:
            w = policy.gap_weight * c.gap_m
            if c.element is None:
                out_links[c.corner].append((T, w, None, c))
            else:
                el = c.element
                out_links[el.u].append((T, w + el.cost_between(policy, 0, c.along), Step(el, 0, c.along), c))
                out_links[el.v].append((T, w + el.cost_between(policy, el.length_m, c.along), Step(el, el.length_m, c.along), c))
        for co in origins:
            for cd in destinations:
                if co.element is not None and co.element is cd.element:
                    el = co.element
                    cost = policy.gap_weight * (co.gap_m + cd.gap_m) + el.cost_between(policy, co.along, cd.along)
                    start_links.append((T, cost, Step(el, co.along, cd.along), co, cd))

        best = {S: 0.0}
        prev: dict[int, tuple] = {}
        heap = [(0.0, 0, S)]
        counter, settled = 1, 0
        done = set()
        while heap:
            cost, _, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node == T:
                break
            settled += 1
            if settled % 512 == 1:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() - start > self.timeout_s or settled > self.max_settled:
                    raise RouteError(504, 'route_timeout', 'The route search took too long.', {'timeout_s': self.timeout_s})
            if node == S:
                links = [(link[0], link[1], ('start',) + tuple(link[2:])) for link in start_links]
            else:
                links = []
                for idx in g.adj.get(node, ()):
                    el = g.elements[idx]
                    nxt = el.v if el.u == node else el.u
                    if el.kind == 'footpath_side':
                        step = Step(el, 0.0, el.length_m) if el.u == node else Step(el, el.length_m, 0.0)
                    else:
                        step = Step(el, 0.0, el.length_m or 0.0, reverse=el.u != node)
                    links.append((nxt, el.full_cost(policy), ('edge', step)))
                for target, w, step, c in out_links.get(node, ()):
                    links.append((target, w, ('end', step, c)))
            for nxt, w, how in links:
                nc = cost + w
                if nc < best.get(nxt, math.inf) - 1e-9:
                    best[nxt] = nc
                    prev[nxt] = (node, how)
                    heapq.heappush(heap, (nc, counter, nxt))
                    counter += 1
        if T not in done:
            raise self._no_route(origins, destinations)

        # Walk back.
        chain = []
        node = T
        while node != S:
            parent, how = prev[node]
            chain.append(how)
            node = parent
        chain.reverse()
        steps, origin_c, dest_c = [], None, None
        for how in chain:
            if how[0] == 'start':
                if len(how) == 4:              # S -> T on one element
                    steps.append(how[1])
                    origin_c, dest_c = how[2], how[3]
                else:
                    origin_c = how[2]
                    if how[1] is not None:
                        steps.append(how[1])
            elif how[0] == 'edge':
                steps.append(how[1])
            else:
                dest_c = how[2]
                if how[1] is not None:
                    steps.append(how[1])
        return self._response(steps, origin_c, dest_c)

    def _no_route(self, origins, destinations) -> RouteError:
        def nodes(cs):
            out = set()
            for c in cs:
                out.update([c.corner] if c.element is None else [c.element.u, c.element.v])
            return out
        o, d = nodes(origins), nodes(destinations)
        main = self.g.main_component
        details = {'origin_in_main_network': any(self.g.component_eligible[x] == main for x in o),
                   'destination_in_main_network': any(self.g.component_eligible[x] == main for x in d)}
        comp = self.g.component_all
        joined = {comp[x] for x in o} & {comp[x] for x in d}
        if joined:
            return RouteError(404, 'no_route', 'No route avoids the excluded footpath.', {'reason': 'blocked_by_exclusions', **details})
        return RouteError(404, 'no_route', 'The origin and destination are not connected in the graph.', {'reason': 'disconnected', **details})

    # -------------------------------------------------------------- response

    def _response(self, steps: list[Step], oc: Candidate, dc: Candidate) -> dict:
        g, policy = self.g, self.policy
        lnglat = lambda p: g.proj.lnglat(*p)
        legs, warnings, route_coords = [], [], []
        evidence = dict.fromkeys(('observed_m', 'uncertain_m', 'unknown_m'), 0.0)
        designation = dict.fromkeys(('ordinary_m', 'designated_pedestrian_m', 'designated_shared_m', 'ambiguous_m'), 0.0)
        ages = dict.fromkeys(('within_3y_m', 'older_than_3y_m', 'undated_m', 'no_observation_m'), 0.0)
        slopes = {k: {axis: dict.fromkeys(SLOPE_CATEGORIES, 0) for axis in axes} for k, axes in SLOPE_SCHEMA.items()}
        seen_obs, flagged, no_slope = set(), 0, 0
        months = []
        street_m = connector_m = 0.0
        crossings = shared = connectors = 0
        warned = set()

        def warn(code, severity, leg, at, observed_on, message, key=None):
            k = key or (code, leg, tuple(at) if at else None)
            if k in warned:
                return
            warned.add(k)
            warnings.append({'code': code, 'severity': severity, 'leg': leg, 'at': at,
                             'observed_on': observed_on.strftime('%Y-%m') if observed_on else None, 'message': message})

        def add_gap(c: Candidate):
            legs.append({'index': len(legs), 'kind': 'endpoint_gap', 'which': c.which, 'length_m': _r(c.gap_m),
                         'evidence': 'not_assessed',
                         'geometry': {'type': 'LineString', 'coordinates': [lnglat(c.requested), lnglat(c.matched)]}})
            warn('endpoint_gap_not_assessed', 'info', legs[-1]['index'], lnglat(c.requested), None,
                 f'The {c.gap_m:.0f} m between the {c.which} and the footpath is not assessed.')
            if c.side_ambiguous:
                warn('endpoint_side_ambiguous', 'prominent', legs[-1]['index'], lnglat(c.requested), None,
                     f'The {c.which} is on the street centreline; which side of the street was assumed.')

        add_gap(oc)
        current = None
        for st in steps:
            el = st.element
            if el.kind == 'footpath_side':
                lo, hi = sorted((st.a, st.b))
                if hi - lo < 0.05:
                    continue
                length = hi - lo
                key = (el.street_name, el.side, el.designation)
                if current is None or current['_key'] != key:
                    current = {'index': len(legs), 'kind': 'footpath_side', 'street_name': el.street_name, 'side': el.side,
                               'facing': el.facing, 'designation': el.designation, 'designation_basis': el.designation_basis,
                               'osm_way_ids': [], 'length_m': 0.0,
                               'evidence_m': dict.fromkeys(evidence, 0.0), 'imagery_age_m': dict.fromkeys(ages, 0.0),
                               'observations': 0, 'flagged_uncertain': 0, '_months': [], '_coords': [], '_key': key}
                    legs.append(current)
                current['length_m'] += length
                current['osm_way_ids'] = sorted(set(current['osm_way_ids']) | set(el.osm_way_ids))
                if current['designation_basis'] != el.designation_basis:
                    current['designation_basis'] = 'several'
                street_m += length
                designation[el.designation + '_m'] += length
                for iv in el.intervals:
                    overlap = min(hi, iv.b) - max(lo, iv.a)
                    if overlap > 0:
                        current['evidence_m'][iv.evidence + '_m'] += overlap
                        evidence[iv.evidence + '_m'] += overlap
                        current['imagery_age_m'][iv.age + '_m'] += overlap
                        ages[iv.age + '_m'] += overlap
                for ro in el.observations:
                    if lo - 1e-6 <= ro.along <= hi + 1e-6 and ro.row.id not in seen_obs:
                        seen_obs.add(ro.row.id)
                        current['observations'] += 1
                        if ro.facts.flagged:
                            current['flagged_uncertain'] += 1
                            flagged += 1
                        if ro.row.observed_on:
                            current['_months'].append(ro.row.observed_on)
                            months.append(ro.row.observed_on)
                        if ro.facts.has_slope:
                            for kind, axes in ro.facts.slopes.items():
                                for axis, cats in axes.items():
                                    for cat, n in cats.items():
                                        slopes[kind][axis][cat] += n
                        else:
                            no_slope += 1
                for ro, finding in el.findings:
                    if lo - 1e-6 <= ro.along <= hi + 1e-6:
                        warn(finding.code, finding.severity, current['index'], [round(ro.row.lng, 7), round(ro.row.lat, 7)],
                             ro.row.observed_on, finding.message, key=(finding.code, ro.row.id))
                part = substring(el.line, el.cum, st.a / el.scale, st.b / el.scale)
                # The offset is relative to the traversal direction; flip it when walking against the line.
                sign = (1 if el.side == 'left' else -1) * (1 if st.a <= st.b else -1)
                shown = [lnglat(p) for p in offset(part, sign * policy.display_offset_m)]
                current['_coords'].extend(shown if not current['_coords'] else shown[1:])
                route_coords.extend(shown)
            elif el.kind in ('crossing', 'shared_surface_transition'):
                current = None
                at = lnglat(g.node_xy[el.node_id])
                legs.append({'index': len(legs), 'kind': el.kind, 'crossed_street': el.street_name, 'at': at,
                             'length_m': None, 'evidence': 'unverified', 'nearby_observations': dict(el.nearby),
                             'geometry': {'type': 'Point', 'coordinates': at}})
                if el.kind == 'crossing':
                    crossings += 1
                    warn('crossing_unverified', 'prominent', legs[-1]['index'], at, None,
                         f'Crossing {el.street_name or "an unnamed street"}: the crossing point, kerb ramps and kerb heights are not known.')
                else:
                    shared += 1
                    warn('shared_surface_transition_unverified', 'prominent', legs[-1]['index'], at, None,
                         f'Moving across {el.street_name or "a designated street"}: the surface and kerbs here are not known.')
            else:  # connector
                current = None
                coords = el.line if not st.reverse else el.line[::-1]
                shown = [lnglat(p) for p in coords]
                connectors += 1
                connector_m += el.length_m
                legs.append({'index': len(legs), 'kind': 'connector', 'length_m': _r(el.length_m), 'evidence': 'inferred',
                             'geometry': {'type': 'LineString', 'coordinates': shown}})
                warn('connector_inferred', 'prominent', legs[-1]['index'], shown[0], None,
                     'This link was inferred where mapped streets stop short of each other; it was never observed.')
                route_coords.extend(shown)
        add_gap(dc)

        for leg in legs:
            if leg['kind'] != 'footpath_side':
                continue
            ms = leg.pop('_months')
            leg['imagery_months'] = {'from': min(ms).strftime('%Y-%m'), 'to': max(ms).strftime('%Y-%m')} if ms else None
            leg['geometry'] = {'type': 'LineString', 'coordinates': leg.pop('_coords')}
            leg.pop('_key')
            leg['length_m'] = _r(leg['length_m'])
            leg['evidence_m'] = {k: _r(v) for k, v in leg['evidence_m'].items()}
            leg['imagery_age_m'] = {k: _r(v) for k, v in leg['imagery_age_m'].items()}
            if leg['evidence_m']['unknown_m'] > 0:
                warn('footpath_unknown', 'prominent', leg['index'], leg['geometry']['coordinates'][0], None,
                     f'{leg["evidence_m"]["unknown_m"]:.0f} m of this footpath has no observation.')

        severity_order = {'prominent': 0, 'info': 1}
        warnings.sort(key=lambda w: (w['leg'], severity_order[w['severity']]))
        truncated = len(warnings) > MAX_WARNINGS
        dedup_coords = []
        for c in route_coords:
            if not dedup_coords or dedup_coords[-1] != c:
                dedup_coords.append(c)
        limits = ['preview_only', 'crossings_inferred_at_junctions', 'camera_position_not_feature_position',
                  'width_not_measured', 'slope_categorical_only', 'imagery_age', 'endpoint_gap_not_assessed']
        if connectors:
            limits.append('connectors_inferred')
        if not g.designator.available:
            limits.append('designation_unavailable')
        s = g.snapshot
        return {
            'api_version': API_VERSION,
            'status': 'unverified_preview',
            'navigation_allowed': False,
            'release': s.release.public(),
            'graph_version': g.version,
            'policy_version': policy.version,
            'as_of_date': g.as_of.isoformat(),
            'endpoints': {c.which: {'requested': lnglat(c.requested), 'matched': lnglat(c.matched), 'gap_m': _r(c.gap_m),
                                    'street_name': c.street_name, 'side': c.side, 'facing': c.facing,
                                    'side_ambiguous': c.side_ambiguous} for c in (oc, dc)},
            'summary': {
                'route_m': _r(street_m + connector_m), 'street_m': _r(street_m), 'connector_m': _r(connector_m),
                'endpoint_gap_m': _r(oc.gap_m + dc.gap_m),
                'evidence_m': {k: _r(v) for k, v in evidence.items()},
                'designation_m': {k: _r(v) for k, v in designation.items()},
                'imagery_age_m': {k: _r(v) for k, v in ages.items()},
                'imagery_months': {'from': min(months).strftime('%Y-%m'), 'to': max(months).strftime('%Y-%m')} if months else None,
                'crossings': {'count': crossings, 'shared_surface_transitions': shared},
                'connectors': {'count': connectors},
                'observations': {'count': len(seen_obs), 'flagged_uncertain': flagged},
            },
            'slope_observations': {'unit': 'feature_observations', **slopes, 'observations_without_slope': no_slope},
            'width': {'status': 'not_measured'},
            'warnings': warnings[:MAX_WARNINGS],
            'warnings_truncated': truncated,
            'legs': legs,
            'geometry': {'type': 'LineString', 'coordinates': dedup_coords},
            'display_offset_m': policy.display_offset_m,
            'limits': limits,
        }


def coverage(graph: Graph) -> dict:
    s = graph.snapshot
    obs = s.observations
    dated = sorted(o.observed_on for o in obs if o.observed_on)
    lngs = [c[0] for _, g in s.coverage for poly in g['coordinates'] for ring in poly for c in ring]
    lats = [c[1] for _, g in s.coverage for poly in g['coordinates'] for ring in poly for c in ring]
    if len(s.coverage) == 1:
        geometry = s.coverage[0][1]
    else:
        geometry = {'type': 'MultiPolygon', 'coordinates': [p for _, g in s.coverage for p in g['coordinates']]}
    limits = [k for k in LIMITS if k != 'designation_unavailable' or not graph.designator.available]
    if any(o.source == 'community' for o in obs):
        limits.remove('no_community_observations')
    return {
        'api_version': API_VERSION,
        'release': s.release.public(),
        'snapshot': {'graph_version': graph.version, 'content_sha256': s.content_hash[:12],
                     'designations_sha256': graph.designator.digest[:12],
                     'loaded_at': s.loaded_at.isoformat().replace('+00:00', 'Z'),
                     'as_of_date': graph.as_of.isoformat(),
                     'community_observations': sum(o.source == 'community' for o in obs)},
        'area': {'name': ' + '.join(n for n, _ in s.coverage), 'approximate': True, 'enforcement': 'strict',
                 'bbox': [round(min(lngs), 6), round(min(lats), 6), round(max(lngs), 6), round(max(lats), 6)],
                 'geometry': geometry},
        'survey': {'imagery_from': dated[0].strftime('%Y-%m') if dated else None,
                   'imagery_to': dated[-1].strftime('%Y-%m') if dated else None,
                   'imagery_median': dated[len(dated) // 2].strftime('%Y-%m') if dated else None,
                   'observations': len(obs),
                   'undated_observations': sum(o.observed_on is None for o in obs),
                   'older_than_3y_observations': sum(1 for d in dated if d < graph.stale_before),
                   'flagged_uncertain_observations': sum(o.needs_verification for o in obs)},
        'graph': graph.stats,
        'policy': graph.policy.describe(),
        'limits': [{'code': k, 'message': LIMITS[k]} for k in limits],
        'snap_max_m': graph.policy.snap_max_m,
        'navigation_allowed': False,
    }
