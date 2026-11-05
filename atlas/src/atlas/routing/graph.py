"""Side-aware routing graph built from one atlas snapshot.

Each street piece between junctions gives a left and a right footpath element. At
each junction the street arms are ordered by angle, and each gap between
neighbouring arms is a corner. Following a side round a corner is free. Reaching
another corner means crossing one arm, which is an unverified crossing element.
There are no mid-block crossings. A street end that meets nothing has separate
left and right corners joined by one crossing. Connectors (inferred links) join
the corners on each side of where they attach. Nothing here is verified.
"""
from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date

from atlas.routing.designation import Designator
from atlas.routing.evidence import KERB_KEYS, Finding, ObservationFacts, classify
from atlas.routing.geo import (XY, Projector, SegmentGrid, cumulative, dist, heading_from_end)
from atlas.routing.policy import Policy
from atlas.routing.snapshot import EdgeRow, ObservationRow, Snapshot

def effective_version(snapshot: Snapshot, designations_digest: str, policy: Policy, as_of: date) -> str:
    """Version of everything that can change a response: data, designations, policy and the as-of date."""
    payload = json.dumps([snapshot.content_hash, designations_digest, asdict(policy), as_of.isoformat()],
                         sort_keys=True, default=str)
    return f'r{snapshot.release.id}-{hashlib.sha256(payload.encode()).hexdigest()[:12]}'


COMPASS = ('east', 'north-east', 'north', 'north-west', 'west', 'south-west', 'south', 'south-east')


def compass(angle: float) -> str:
    return COMPASS[round(math.degrees(angle) % 360 / 45) % 8]


def years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


@dataclass
class Interval:
    a: float
    b: float
    evidence: str        # observed, uncertain, unknown
    age: str             # within_3y, older_than_3y, undated, no_observation


@dataclass
class RouteObservation:
    along: float                      # metres from the piece start
    row: ObservationRow
    facts: ObservationFacts


@dataclass
class Element:
    index: int
    kind: str                         # footpath_side, crossing, shared_surface_transition, connector
    u: int
    v: int
    length_m: float | None
    excluded: list[str] = field(default_factory=list)
    # footpath_side
    edge: EdgeRow | None = None
    side: str | None = None
    street_name: str | None = None
    facing: str | None = None
    designation: str | None = None
    designation_basis: str | None = None
    osm_way_ids: list[int] = field(default_factory=list)
    line: list[XY] | None = None      # projected, from u to v (segment direction for footpaths)
    cum: list[float] | None = None
    scale: float = 1.0                # metres per projected unit
    intervals: list[Interval] = field(default_factory=list)
    observations: list[RouteObservation] = field(default_factory=list)
    findings: list[tuple[RouteObservation, Finding]] = field(default_factory=list)
    # crossing
    node_id: int | None = None
    nearby: dict | None = None
    cost_flat: float = 0.0

    def cost_between(self, policy: Policy, a: float, b: float) -> float:
        if self.kind != 'footpath_side':
            return self.cost_flat
        lo, hi = sorted((a, b))
        total = 0.0
        for iv in self.intervals:
            overlap = min(hi, iv.b) - max(lo, iv.a)
            if overlap > 0:
                total += overlap * {'observed': 1.0, 'uncertain': policy.uncertain_multiplier,
                                    'unknown': policy.unknown_multiplier}[iv.evidence]
        return total

    def full_cost(self, policy: Policy) -> float:
        return self.cost_between(policy, 0.0, self.length_m or 0.0) if self.kind == 'footpath_side' else self.cost_flat


@dataclass
class Corner:
    node_id: int
    sector: int
    xy: XY


@dataclass
class StreetEnd:
    angle: float
    edge: EdgeRow
    at_start: bool


class Graph:
    def __init__(self, snapshot: Snapshot, policy: Policy, designator_doc: dict | None, as_of: date):
        self.snapshot, self.policy, self.as_of = snapshot, policy, as_of
        lngs = [p[0] for p in snapshot.nodes.values()]
        lats = [p[1] for p in snapshot.nodes.values()]
        self.proj = Projector((min(lngs) + max(lngs)) / 2, (min(lats) + max(lats)) / 2)
        self.designator = Designator(designator_doc, self.proj)
        self.version = effective_version(snapshot, self.designator.digest, policy, as_of)
        self.stale_before = years_before(as_of, policy.imagery_age_years)
        self.corners: list[Corner] = []
        self.elements: list[Element] = []
        self.adj: dict[int, list[int]] = defaultdict(list)          # eligible elements only
        self.node_corners: dict[int, list[int]] = {}
        self.node_street_ends: dict[int, list[StreetEnd]] = {}
        self.piece_grid = SegmentGrid(cell=50.0)
        self.pieces: dict[int, dict] = {}                           # street edge id -> geometry and elements
        self.coverage_polygons = [[self.proj.line(ring) for ring in poly]
                                  for _, geom in snapshot.coverage for poly in geom['coordinates']]
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self):
        s = self.snapshot
        node_xy = {n: self.proj.xy(*p) for n, p in s.nodes.items()}
        self.node_xy = node_xy
        street_edges = [e for e in s.edges if e.kind == 'street']
        for e in street_edges:
            line = self.proj.line(e.coords)
            cum = cumulative(line)
            seg = s.segments[e.segment_id]
            designation, matched, way_ids, basis = self.designator.classify(seg.street_name, line, cum)
            self.pieces[e.id] = {'edge': e, 'line': line, 'cum': cum, 'designation': designation,
                                 'matched_share': matched, 'osm_way_ids': way_ids, 'designation_basis': basis,
                                 'street_name': seg.street_name,
                                 'scale': (e.length_m / cum[-1]) if cum[-1] > 0 else 1.0}
            self.piece_grid.add_line(line, e.id)

        # Observations onto pieces.
        by_segment = defaultdict(list)
        for e in street_edges:
            by_segment[e.segment_id].append(e)
        for edges in by_segment.values():
            edges.sort(key=lambda e: e.start_m)
        observations_by_piece = defaultdict(list)
        for o in s.observations:
            edges = by_segment.get(o.segment_id)
            if not edges:
                continue
            i = max(0, bisect_right([e.start_m for e in edges], o.position_m) - 1)
            e = edges[i]
            along = min(max(o.position_m - e.start_m, 0.0), e.length_m)
            facts = classify(o.footpath, o.needs_verification, o.features)
            observations_by_piece[(e.id, o.side)].append(RouteObservation(along, o, facts))
        self.observation_index = SegmentGrid(cell=50.0)
        for obs_list in observations_by_piece.values():
            for ro in obs_list:
                if ro.facts.kerb:
                    p = self.proj.xy(ro.row.lng, ro.row.lat)
                    self.observation_index.add_line([p, p], ro)

        # Street ends at each node, ordered by angle.
        for e in street_edges:
            piece = self.pieces[e.id]
            for at_start, node in ((True, e.source), (False, e.target)):
                angle = heading_from_end(piece['line'], piece['cum'], at_start)
                self.node_street_ends.setdefault(node, []).append(StreetEnd(angle, e, at_start))
        for node in s.nodes:
            ends = sorted(self.node_street_ends.get(node, []), key=lambda x: x.angle)
            self.node_street_ends[node] = ends
            count = 2 if len(ends) == 1 else max(1, len(ends))
            ids = []
            for sector in range(count):
                ids.append(len(self.corners))
                self.corners.append(Corner(node, sector, node_xy[node]))
            self.node_corners[node] = ids

        # Footpath side elements.
        end_index = {}
        for node, ends in self.node_street_ends.items():
            for i, end in enumerate(ends):
                end_index[(end.edge.id, end.at_start)] = i
        for e in street_edges:
            piece = self.pieces[e.id]
            i, j = end_index[(e.id, True)], end_index[(e.id, False)]
            sides = {'left': (self._ccw(e.source, i), self._cw(e.target, j)),
                     'right': (self._cw(e.source, i), self._ccw(e.target, j))}
            heading = math.atan2(piece['line'][-1][1] - piece['line'][0][1], piece['line'][-1][0] - piece['line'][0][0])
            piece['elements'] = {}
            for side, (u, v) in sides.items():
                el = Element(index=len(self.elements), kind='footpath_side', u=u, v=v, length_m=e.length_m,
                             edge=e, side=side, street_name=piece['street_name'],
                             facing=compass(heading + (math.pi / 2 if side == 'left' else -math.pi / 2)),
                             designation=piece['designation'], line=piece['line'], cum=piece['cum'], scale=piece['scale'],
                             osm_way_ids=piece['osm_way_ids'], designation_basis=piece['designation_basis'])
                obs = sorted(observations_by_piece.get((e.id, side), []), key=lambda r: (r.along, r.row.id))
                el.observations = obs
                self._assess(el)
                self.elements.append(el)
                piece['elements'][side] = el

        # Crossings at junctions (3+ arms) and street ends that meet nothing. A camera position
        # cannot tie reported crossing steps to one arm, so they exclude every crossing there.
        for node, ends in self.node_street_ends.items():
            if len(ends) == 2:
                continue
            nearby, steps = self._nearby(node)
            for i, end in enumerate(ends):
                designated = self.pieces[end.edge.id]['designation'] in ('designated_pedestrian', 'designated_shared')
                el = Element(index=len(self.elements), kind='shared_surface_transition' if designated else 'crossing',
                             u=self._cw(node, i), v=self._ccw(node, i), length_m=None, node_id=node,
                             street_name=self.pieces[end.edge.id]['street_name'], nearby=nearby, excluded=list(steps))
                el.cost_flat = self.policy.shared_transition_penalty_m if designated else self.policy.crossing_penalty_m
                self.elements.append(el)

        # Connectors: inferred links, joined to the corners either side of where they attach.
        for e in s.edges:
            if e.kind != 'connector':
                continue
            line = self.proj.line(e.coords)
            cum = cumulative(line)
            us = self._corners_for_angle(e.source, heading_from_end(line, cum, True))
            vs = self._corners_for_angle(e.target, heading_from_end(line, cum, False))
            for u in us:
                for v in vs:
                    el = Element(index=len(self.elements), kind='connector', u=u, v=v, length_m=e.length_m, edge=e,
                                 line=line, cum=cum, scale=(e.length_m / cum[-1]) if cum[-1] > 0 else 1.0)
                    el.cost_flat = e.length_m * self.policy.connector_multiplier
                    self.elements.append(el)

        for el in self.elements:
            if not el.excluded:
                self.adj[el.u].append(el.index)
                self.adj[el.v].append(el.index)
        self.component_eligible = self._components(lambda el: not el.excluded)
        self.component_all = self._components(lambda el: True)
        self.stats = self._stats()

    def _ccw(self, node: int, i: int) -> int:
        corners = self.node_corners[node]
        return corners[0] if len(self.node_street_ends[node]) == 1 else corners[i]

    def _cw(self, node: int, i: int) -> int:
        corners = self.node_corners[node]
        k = len(self.node_street_ends[node])
        return corners[1] if k == 1 else corners[(i - 1) % k]

    def _corners_for_angle(self, node: int, angle: float) -> list[int]:
        ends = self.node_street_ends.get(node, [])
        corners = self.node_corners[node]
        if len(ends) <= 1:
            return list(corners)
        return [corners[self._sector(ends, angle)]]

    @staticmethod
    def _sector(ends: list[StreetEnd], angle: float) -> int:
        """Index i of the gap between arm i and arm i+1 (counter-clockwise) that contains angle."""
        k = len(ends)
        for i in range(k):
            a, b = ends[i].angle, ends[(i + 1) % k].angle
            span = (b - a) % (2 * math.pi) or 2 * math.pi
            if (angle - a) % (2 * math.pi) < span:
                return i
        return 0

    def _nearby(self, node: int) -> tuple[dict, list[str]]:
        """Kerb and crossing observation counts within nearby_m, and step exclusions for its crossings."""
        p = self.node_xy[node]
        counts = dict.fromkeys(KERB_KEYS, 0)
        steps, seen = set(), set()
        for a, _, ro in self.observation_index.near(p, self.policy.nearby_m):
            if ro.row.id in seen or dist(a, p) > self.policy.nearby_m:
                continue
            seen.add(ro.row.id)
            for k, v in ro.facts.kerb.items():
                counts[k] += v
            if ro.facts.steps_at_crossing:
                steps.add('steps_reported_nearby_flagged' if ro.facts.flagged else 'steps_reported_nearby')
        return counts, sorted(steps)

    def _assess(self, el: Element):
        """Exclusions, findings and evidence intervals for one footpath side piece."""
        designated = el.designation in ('designated_pedestrian', 'designated_shared')
        reasons = []
        for ro in el.observations:
            f = ro.facts
            missing = f.footpath == 'absent' or f.footpath_ends
            # Reported fixed barriers and steps exclude even when the observation is flagged;
            # the re-check uncertainty is kept as a separate exclusion reason for diagnostics.
            if f.fixed_barriers:
                reasons.append('fixed_barrier_flagged' if f.flagged else 'fixed_barrier')
            # Blockages of unrecorded permanence never exclude; they always warn prominently.
            if f.blocking_permanence_unknown:
                el.findings.append((ro, Finding('blocking_permanence_unknown', 'prominent',
                                                'Something blocked the path when imaged; whether it is permanent is not recorded.')))
            if f.flagged:
                shown = (['footpath absent'] if f.footpath == 'absent' else []) + \
                        (['footpath ends'] if f.footpath_ends else []) + f.blocking_permanence_unknown
                message = 'Observation flagged for re-checking; treat as uncertain.'
                if shown:
                    message = 'Observation flagged for re-checking showed: ' + ', '.join(x.replace('_', ' ') for x in shown) + '.'
                el.findings.append((ro, Finding('flagged_observation', 'prominent' if shown else 'info', message)))
            else:
                if missing and not designated:
                    reasons.append('footpath_absent' if f.footpath == 'absent' else 'footpath_ends')
                if missing and designated:
                    el.findings.append((ro, Finding('no_separate_footpath', 'info',
                                                    'No separate footpath here; the street is designated for pedestrians or shared use.')))
            for finding in f.findings:
                el.findings.append((ro, finding))
        el.excluded = sorted(set(reasons))
        el.intervals = self._intervals(el)

    def _intervals(self, el: Element) -> list[Interval]:
        """Partition [0, length] by nearest observation within reach; the rest is unknown."""
        length = el.length_m
        reach = self.policy.evidence_reach_m
        groups: list[tuple[float, list[RouteObservation]]] = []
        for ro in el.observations:
            if groups and abs(groups[-1][0] - ro.along) < 1e-6:
                groups[-1][1].append(ro)
            else:
                groups.append((ro.along, [ro]))
        cuts = []
        for i, (pos, members) in enumerate(groups):
            lo = max(0.0, pos - reach, (groups[i - 1][0] + pos) / 2 if i else 0.0)
            hi = min(length, pos + reach, (pos + groups[i + 1][0]) / 2 if i + 1 < len(groups) else length)
            evidence = 'uncertain' if any(m.facts.flagged for m in members) else 'observed'
            dated = [m.row.observed_on for m in members if m.row.observed_on]
            age = 'undated' if not dated else ('older_than_3y' if max(dated) < self.stale_before else 'within_3y')
            if hi > lo:
                cuts.append(Interval(lo, hi, evidence, age))
        out, cursor = [], 0.0
        for iv in cuts:
            if iv.a > cursor + 1e-9:
                out.append(Interval(cursor, iv.a, 'unknown', 'no_observation'))
            out.append(iv)
            cursor = iv.b
        if cursor < length - 1e-9:
            out.append(Interval(cursor, length, 'unknown', 'no_observation'))
        return out

    def _components(self, include) -> dict[int, int]:
        parent = list(range(len(self.corners)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for el in self.elements:
            if include(el):
                a, b = find(el.u), find(el.v)
                if a != b:
                    parent[a] = b
        return {c: find(c) for c in range(len(self.corners))}

    def _stats(self) -> dict:
        footpaths = [el for el in self.elements if el.kind == 'footpath_side']
        street_m = sum(p['edge'].length_m for p in self.pieces.values())
        designation = defaultdict(float)
        basis = defaultdict(float)
        matched = 0.0
        for p in self.pieces.values():
            designation[p['designation']] += p['edge'].length_m
            basis[p['designation_basis']] += p['edge'].length_m
            matched += p['matched_share'] * p['edge'].length_m
        excluded = defaultdict(float)
        for el in footpaths:
            for r in el.excluded:
                excluded[r] += el.length_m
        excluded_crossings = defaultdict(int)
        for el in self.elements:
            if el.kind in ('crossing', 'shared_surface_transition'):
                for r in el.excluded:
                    excluded_crossings[r] += 1
        comp_len = defaultdict(float)
        eligible_m = 0.0
        for el in footpaths:
            if not el.excluded:
                comp_len[self.component_eligible[el.u]] += el.length_m
                eligible_m += el.length_m
        evidence = defaultdict(float)
        for el in footpaths:
            for iv in el.intervals:
                evidence[iv.evidence + '_m'] += iv.b - iv.a
        self.main_component = max(comp_len, key=comp_len.get) if comp_len else None
        return {
            'street_m': round(street_m, 1),
            'footpath_side_pieces': len(footpaths),
            'footpath_side_m': round(sum(el.length_m for el in footpaths), 1),
            'footpath_side_evidence_m': {k: round(evidence.get(k, 0.0), 1) for k in ('observed_m', 'uncertain_m', 'unknown_m')},
            'excluded_footpath_side_m': {k: round(v, 1) for k, v in sorted(excluded.items())},
            'excluded_crossings': dict(sorted(excluded_crossings.items())),
            'eligible_footpath_side_m': round(eligible_m, 1),
            'corners': len(self.corners),
            'crossings': sum(el.kind == 'crossing' for el in self.elements),
            'shared_surface_transitions': sum(el.kind == 'shared_surface_transition' for el in self.elements),
            'connectors': sum(e.kind == 'connector' for e in self.snapshot.edges),
            'connector_m': round(sum(e.length_m for e in self.snapshot.edges if e.kind == 'connector'), 1),
            'eligible_components': len(comp_len),
            'largest_eligible_component_share': round(max(comp_len.values()) / eligible_m, 4) if eligible_m else 0.0,
            'designation': {
                'available': self.designator.available,
                'source': self.designator.source,
                'matched_share': round(matched / street_m, 4) if street_m else 0.0,
                **{k + '_m': round(designation.get(k, 0.0), 1)
                   for k in ('ordinary', 'designated_pedestrian', 'designated_shared', 'ambiguous')},
                'basis_m': {k: round(v, 1) for k, v in sorted(basis.items())},
            },
        }

    # ---------------------------------------------------------------- helpers

    def in_coverage(self, lng: float, lat: float) -> bool:
        from atlas.routing.geo import in_multipolygon
        return in_multipolygon(self.proj.xy(lng, lat), self.coverage_polygons)
