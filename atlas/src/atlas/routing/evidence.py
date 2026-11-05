"""What one camera observation says, from its raw feature tags.

Every fact is tri-state by construction: a tag that is missing stays unknown. This
replaces atlas.observation_signals, whose OR chains turn "observed, no concern"
into NULL whenever any one tag is missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SLOPE_CATEGORIES = ('apparently_level', 'gentle', 'moderate', 'steep')
SURFACE_CONCERN = {'loose_gravel', 'earth', 'sand', 'grass', 'rounded_cobblestones'}


@dataclass
class Finding:
    code: str
    severity: str          # 'prominent' or 'info'
    message: str


@dataclass
class ObservationFacts:
    footpath: str | None                       # present, absent or None (unknown)
    flagged: bool                              # needs_verification: uncertain
    footpath_ends: bool = False                # continuity ends / interruption ends_in_road
    fixed_barriers: list[str] = field(default_factory=list)        # excluded even when flagged
    blocking_permanence_unknown: list[str] = field(default_factory=list)  # eligible, prominent warning
    steps_at_crossing: bool = False                                  # crossing steps or a stepped ramp bottom
    findings: list[Finding] = field(default_factory=list)
    # kind -> axis -> category -> count of features
    slopes: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    kerb: dict[str, int] = field(default_factory=dict)  # context for nearby crossings

    @property
    def has_slope(self) -> bool:
        return any(c for axes in self.slopes.values() for cats in axes.values() for c in cats.values())


def _on_route(attrs: dict) -> bool:
    return attrs.get('route_relation') != 'beside_route' and attrs.get('affected_width') != 'outside_route'


def _slope(facts: ObservationFacts, kind: str, axis: str, value: str | None):
    if value in SLOPE_CATEGORIES:
        cats = facts.slopes.setdefault(kind, {}).setdefault(axis, {})
        cats[value] = cats.get(value, 0) + 1
        if value == 'steep':
            facts.findings.append(Finding('steep_slope_observed', 'prominent',
                                          f'A camera observation tagged a steep {axis.replace("_", " ")} on a {kind.replace("_", " ")}. '
                                          'The gradient was not measured.'))


def _kerb(facts: ObservationFacts, key: str):
    facts.kerb[key] = facts.kerb.get(key, 0) + 1


def classify(footpath: str | None, flagged: bool, features: list[tuple[str, dict]]) -> ObservationFacts:
    facts = ObservationFacts(footpath=footpath if footpath in ('present', 'absent') else None, flagged=flagged)
    add = facts.findings.append
    for kind, attrs in features:
        on_route = _on_route(attrs)
        if kind == 'footpath_section':
            if not on_route:
                continue
            _slope(facts, 'footpath', 'longitudinal', attrs.get('longitudinal_slope'))
            _slope(facts, 'footpath', 'cross', attrs.get('cross_slope'))
            interruption = attrs.get('interruption')
            if attrs.get('continuity') == 'ends' or interruption == 'ends_in_road':
                facts.footpath_ends = True
            if interruption == 'steps':
                facts.fixed_barriers.append('steps')
            elif interruption in ('barrier', 'construction'):
                facts.blocking_permanence_unknown.append(f'footpath_{interruption}')
            elif interruption == 'missing_surface':
                add(Finding('missing_surface_observed', 'prominent', 'Part of the footpath surface looked missing.'))
            elif attrs.get('continuity') == 'interrupted':
                add(Finding('footpath_interrupted', 'prominent', 'The footpath looked interrupted here.'))
            if attrs.get('clear_passage') == 'blocked':
                facts.blocking_permanence_unknown.append('passage_blocked')
            elif attrs.get('clear_passage') == 'narrowed':
                add(Finding('narrowed_passage', 'prominent', 'The footpath looked narrowed. Width was not measured.'))
            if (attrs.get('condition') == 'visible_defect' or attrs.get('texture') in ('rough', 'very_rough')
                    or attrs.get('surface') in SURFACE_CONCERN):
                add(Finding('surface_concern', 'info', 'Surface defect, rough texture or loose surface observed.'))
        elif kind == 'driveway':
            if not on_route:
                continue
            _slope(facts, 'driveway', 'longitudinal', attrs.get('longitudinal_slope'))
            _slope(facts, 'driveway', 'cross', attrs.get('cross_slope'))
            joins = {attrs.get('entry_join'), attrs.get('exit_join')}
            if 'step' in joins:
                facts.fixed_barriers.append('driveway_step')
            if 'open_gap' in joins:
                facts.fixed_barriers.append('driveway_open_gap')
            if joins & {'raised_lip', 'channel'}:
                add(Finding('driveway_lip_or_channel', 'prominent', 'A raised lip or channel at a driveway edge.'))
            treatment = attrs.get('footpath_treatment')
            if treatment in ('dips', 'tilts_sideways', 'interrupted'):
                add(Finding('driveway_' + treatment, 'info', f'The footpath {treatment.replace("_", " ")} at a driveway.'))
            if attrs.get('clear_passage') == 'blocked':
                facts.blocking_permanence_unknown.append('passage_blocked')
        elif kind == 'hazard':
            relation = attrs.get('route_relation')
            if relation not in ('on_route', 'partially_encroaching') or attrs.get('affected_width') == 'outside_route':
                continue
            blocking = attrs.get('clear_passage') == 'blocked' or (
                attrs.get('affected_width') == 'full_width' and attrs.get('bypass') == 'not_visible')
            persistence = attrs.get('persistence')
            label = attrs.get('type', 'unspecified')
            if blocking and (persistence == 'fixed' or label == 'step'):
                # A blocking step is built form: a fixed barrier whatever its persistence tag says.
                facts.fixed_barriers.append(f'hazard_{label}')
            elif blocking and persistence == 'temporary':
                add(Finding('temporary_obstruction_observed', 'prominent',
                            f'A temporary {label.replace("_", " ")} blocked the path when imaged.'))
            elif blocking:
                facts.blocking_permanence_unknown.append(f'hazard_{label}')
            else:
                when = {'fixed': 'fixed ', 'temporary': 'temporary '}.get(persistence, '')
                add(Finding('hazard_observed', 'info', f'A {when}{label.replace("_", " ")} on part of the path.'))
        elif kind == 'kerb_ramp':
            _slope(facts, 'kerb_ramp', 'slope', attrs.get('slope'))
            _slope(facts, 'kerb_ramp', 'longitudinal', attrs.get('longitudinal_slope'))
            _slope(facts, 'kerb_ramp', 'cross', attrs.get('cross_slope'))
            presence = attrs.get('presence')
            if presence == 'present':
                _kerb(facts, 'kerb_ramp_present')
            elif presence == 'absent':
                _kerb(facts, 'kerb_ramp_absent')
            if attrs.get('bottom_join') in ('raised_lip', 'step', 'open_gap', 'channel'):
                _kerb(facts, 'kerb_ramp_raised_lip_or_step')
            if attrs.get('bottom_join') == 'step':
                facts.steps_at_crossing = True
        elif kind == 'crossing':
            _slope(facts, 'crossing', 'longitudinal', attrs.get('longitudinal_slope'))
            _slope(facts, 'crossing', 'cross', attrs.get('cross_slope'))
            transition = attrs.get('transition_type')
            if transition == 'unmodified_kerb':
                _kerb(facts, 'crossing_unmodified_kerb')
            elif transition == 'steps':
                _kerb(facts, 'crossing_steps')
                facts.steps_at_crossing = True
            elif transition in ('kerb_ramp', 'level_or_raised_crossing', 'continuous_footpath'):
                _kerb(facts, 'crossing_level_or_ramp')
    return facts


KERB_KEYS = ('kerb_ramp_present', 'kerb_ramp_absent', 'kerb_ramp_raised_lip_or_step',
             'crossing_unmodified_kerb', 'crossing_steps', 'crossing_level_or_ramp')
