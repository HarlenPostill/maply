"""Route preview policy. Settled rules are fixed; pending choices are explicit settings.

Nothing here can make a route verified or enable navigation: every route is an
unverified planning preview.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

POLICY_VERSION = 'manual_chair_v1'
API_VERSION = 'maply-route-preview/1'


@dataclass(frozen=True)
class Policy:
    version: str = POLICY_VERSION
    # Engineering limits and reporting allocations. None of these is an accessibility claim.
    snap_max_m: float = 40.0
    side_ambiguous_m: float = 0.5
    excluded_bypass_m: float = 5.0   # an excluded footpath beside the point blocks matches farther than this beyond it
    evidence_reach_m: float = 10.0
    nearby_m: float = 25.0
    display_offset_m: float = 4.0
    imagery_age_years: int = 3

    # Route preferences. Slope and imagery age never change cost.
    unknown_multiplier: float = 1.5
    uncertain_multiplier: float = 1.3
    connector_multiplier: float = 1.5
    crossing_penalty_m: float = 30.0
    shared_transition_penalty_m: float = 10.0
    gap_weight: float = 3.0

    def describe(self) -> dict:
        return {
            'version': self.version,
            'all_routes': 'unverified_preview',
            'navigation_allowed': False,
            'settled': {
                'missing_footpath_ordinary_street': 'excluded',
                'fixed_barrier': 'excluded_even_if_flagged',
                'steps': 'excluded_even_if_flagged',
                'steps_reported_at_crossing': 'every_crossing_at_that_junction_excluded',
                'blocking_permanence_unknown': 'eligible_with_prominent_warning',
                'temporary_obstruction': 'eligible_with_warning',
                'flagged_observation': 'uncertain; excludes only for fixed barriers and steps',
                'missing_fact': 'unknown',
                'steep_slope': 'eligible_with_prominent_warning',
                'slope_cost': 'none',
                'imagery_age': 'reported_separately',
                'crossings_and_connectors': 'always_unverified',
                'designated_pedestrian_or_shared_street': 'missing_footpath_allowed_when_per_way_designation_covers_piece',
            },
            'pending_decisions': [],
            'parameters': {k: v for k, v in asdict(self).items() if k != 'version'},
        }


DEFAULT_POLICY = Policy()

LIMITS: dict[str, str] = {
    'preview_only': 'No route is verified or fit to navigate by. It is a planning preview.',
    'crossings_inferred_at_junctions': 'Crossings are placed at street junctions from centreline geometry. Crossing '
                                       'locations, signals, kerb heights and crossing widths are not known.',
    'camera_position_not_feature_position': 'Observations are located at the Street View camera, not at the footpath, '
                                            'ramp or kerb they describe.',
    'no_footway_network': 'Only seven road classes are mapped: no park, plaza or laneway paths, service roads or steps.',
    'no_grade_separation': 'Bridges and tunnels are unknown; streets that cross on the map are treated as meeting.',
    'width_not_measured': 'Footpath width is not in the data.',
    'slope_categorical_only': 'Slopes are categories from imagery, on few observations. No gradients exist.',
    'imagery_age': 'Imagery ranges from 2007 to 2026 and much of it is undated.',
    'approximate_boundary': 'The survey boundary is approximate. Endpoints outside it are refused.',
    'endpoint_gap_not_assessed': 'The stretch between the requested point and the footpath is not assessed. '
                                 'Entrances are not in the data.',
    'osm_tags_merged_by_name': 'OSM sidewalk and width tags were merged per street name and are not used.',
    'no_community_observations': 'There are no player reports yet.',
    'designation_unavailable': 'Per-way street designations are not loaded; no street gets the pedestrian or '
                               'shared-zone allowance.',
    'connectors_inferred': 'Some links join streets that stop short of each other. They are inferred.',
}
